"""Writer orchestrator: per-section LLM generation with stub fallback.

Per the 2026-05-27 per-section refactor (ADR-012 amendment) the writer
issues 8 calls · one per template section · rather than one big prompt.
Six LLM-driven sections use ``generate_with_prefill`` with the section
marker as the assistant prefill so the model cannot emit the wrong
header. Section 7 (SİSTEM LOG) is rendered deterministically. Empty
sections (no payload data) emit a canonical "bugün öğe yok" note
WITHOUT calling the LLM · the 2026-05-27 hallucinated specimen showed
the model invents content for empty sections every time.

Failure isolation:

* LLM raises / returns text that fails ``validate_section`` →
  ``render_section_stub(idx)`` substitutes a stub. Other sections are
  unaffected.
* ``validate_section`` rejects prompt echo (DISCIPLINE_RULES markers,
  BÖLÜM VERİSİ, ÇIKTI trailers) and chain-of-thought scaffolding
  ("Adım N:" / "Gerekçe:") so hallucinated specimens never ship.
* If all 8 sections fail OR the final assembled markdown fails
  ``validate_briefing_markdown``, fall through to
  ``render_fallback_briefing`` · ``used_fallback=True``. Partial-stub
  runs are NOT full fallback per ADR-012 amendment.

The briefing markdown is written to ``briefings/YYYY/MM/DD/briefing.md``
and a ``briefings`` row is inserted (or updated) with the day's
aggregate counts. Multiple runs on the same date overwrite the file
and update the row · the briefing is per-date, not per-run.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from musahit.common.logging import get_logger
from musahit.common.time import utcnow
from musahit.score.llm_client import LlmClient
from musahit.writer.fallback import render_fallback_briefing, render_section_stub
from musahit.writer.payload import (
    BUCKET_AMBIENT,
    BUCKET_MATERIAL,
    BUCKET_PRIORITY,
    BUCKET_ROUTINE,
    BriefingPayload,
    build_payload,
)
from musahit.writer.prompt import (
    build_section_user,
    build_system_log_section,
    build_writer_system,
)
from musahit.writer.template import DOCUMENT_TITLE, TEMPLATE_SECTIONS
from musahit.writer.validator import validate_briefing_markdown, validate_section

_log = get_logger("musahit.writer")

DEFAULT_BRIEFINGS_ROOT: Path = Path("briefings")
DEFAULT_WRITER_TEMPERATURE: float = 0.3
DEFAULT_WRITER_MAX_TOKENS: int = 4096

# Sentinel emitted for empty sections instead of calling the LLM. The
# 2026-05-27 hallucinated specimen showed the model invents COVID
# headlines and CHP intrigue when given an empty section payload · the
# only safe answer is "don't call the LLM."
EMPTY_SECTION_NOTE_TR: str = "Bugün bu bölümde öğe yok."

# Indices into TEMPLATE_SECTIONS. 7 is the deterministic SİSTEM LOG.
SYSTEM_LOG_SECTION_IDX: int = 7
LLM_SECTION_INDICES: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)


class Briefer:
    """Compose, validate, and persist the nightly Markdown briefing.

    ``target_date`` (when provided) is the TR-local briefing date that
    drives :attr:`BriefingPayload.date` and the markdown path
    (``briefings/YYYY/MM/DD/briefing.md``). When omitted, the writer
    falls back to deriving the date from ``pipeline_runs.started_at``
    (UTC) · this is the legacy path and is wrong when the run crosses
    midnight TR-local. Production callers (orchestrator + CLI) always
    pass ``target_date``; the fallback exists for backward compat with
    legacy test fixtures.
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        llm: LlmClient,
        briefings_root: Path = DEFAULT_BRIEFINGS_ROOT,
        writer_model: str | None = None,
        *,
        target_date: date | None = None,
    ) -> None:
        self._conn = conn
        self._llm = llm
        self._root = Path(briefings_root)
        self._writer_model = writer_model
        self._target_date = target_date

    async def run(self, run_id: str) -> dict[str, Any]:
        log = _log.bind(run_id=run_id)
        payload = build_payload(
            self._conn, run_id, target_date=self._target_date
        )

        markdown, used_fallback, sections_failed = await self._compose(payload, log)
        path = self._write_markdown(payload, markdown)
        self._upsert_briefings_row(payload, path)
        self._mark_stage_done(
            run_id,
            used_fallback=used_fallback,
            sections_failed=sections_failed,
        )

        log.info(
            "writer_done",
            used_fallback=used_fallback,
            sections_failed=sections_failed,
            cluster_count=payload.cluster_count,
            arc_count=payload.arc_count,
            open_arc_count=payload.open_arc_count,
            peak_defcon=payload.peak_defcon,
            path=str(path),
        )
        return {
            "path": str(path),
            "used_fallback": used_fallback,
            "sections_failed": sections_failed,
            "cluster_count": payload.cluster_count,
            "arc_count": payload.arc_count,
            "open_arc_count": payload.open_arc_count,
            "peak_defcon": payload.peak_defcon,
        }

    # ── Compose loop ────────────────────────────────────────────────────

    async def _compose(
        self, payload: BriefingPayload, log: Any
    ) -> tuple[str, bool, list[int]]:
        """Generate each section · assemble · validate.

        Returns ``(markdown, used_fallback, sections_failed)``.

        * ``sections_failed`` lists LLM-section indices (0-6) that
          could not be generated cleanly and were replaced with a stub.
          Empty sections that emitted the canonical "öğe yok" note are
          NOT counted as failures · empties are correct behavior.
        * ``used_fallback`` is True only when all 8 sections failed OR
          when the final assembled markdown still fails
          ``validate_briefing_markdown`` (last-resort safety net).
        """
        sections: list[str | None] = [None] * len(TEMPLATE_SECTIONS)
        failed_indices: list[int] = []

        for idx in LLM_SECTION_INDICES:
            section = await self._compose_section(payload, idx, log)
            if section.failed:
                failed_indices.append(idx)
                sections[idx] = section.text
            else:
                sections[idx] = section.text

        # SİSTEM LOG is always deterministic · also reports the failed
        # indices so the operator sees what fell back to stubs.
        sections[SYSTEM_LOG_SECTION_IDX] = build_system_log_section(
            payload, failed_indices
        )

        markdown = f"{DOCUMENT_TITLE}\n\n" + "\n\n".join(
            s for s in sections if s is not None
        )

        # Final safety net · should pass by construction.
        final_errors = validate_briefing_markdown(markdown)
        if final_errors:
            log.warning(
                "writer_final_validation_failed",
                errors=final_errors[:3],
                sections_failed=failed_indices,
            )
            return (
                render_fallback_briefing(payload),
                True,
                list(range(len(TEMPLATE_SECTIONS))),
            )

        # Partial-stub runs are NOT full fallback per ADR-012 amendment.
        # SİSTEM LOG (idx 7) is always deterministic so "all 8 failed"
        # cannot happen via the LLM path; used_fallback only flips True
        # via the final-validator safety net above.
        used_fallback = False
        if failed_indices:
            log.warning(
                "writer_sections_fallback",
                sections_failed=failed_indices,
            )
        return markdown, used_fallback, failed_indices

    async def _compose_section(
        self, payload: BriefingPayload, idx: int, log: Any
    ) -> _SectionResult:
        marker = TEMPLATE_SECTIONS[idx].marker
        prefill = f"{marker}\n\n"

        # Empty-section short-circuit · never call the LLM for sections
        # whose payload has no data. The 2026-05-27 specimen showed
        # 100% hallucination on empty sections; this path eliminates
        # the failure mode by deciding deterministically.
        if _is_section_empty(payload, idx):
            log.info("writer_section_empty_short_circuit", section_idx=idx)
            return _SectionResult(
                text=f"{prefill}{EMPTY_SECTION_NOTE_TR}\n",
                failed=False,
            )

        try:
            body = await self._llm.generate_with_prefill(
                system=build_writer_system(),
                user=build_section_user(payload, idx),
                prefill=prefill,
                **self._llm_kwargs(),
            )
        except Exception as exc:
            log.warning(
                "writer_section_llm_error",
                section_idx=idx,
                error=f"{type(exc).__name__}: {exc}",
            )
            return _SectionResult(text=render_section_stub(idx), failed=True)

        full = prefill + body
        if not validate_section(full, idx):
            log.warning(
                "writer_section_validation_failed",
                section_idx=idx,
                preview=body[:120],
            )
            return _SectionResult(text=render_section_stub(idx), failed=True)
        return _SectionResult(text=full, failed=False)

    def _llm_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "temperature": DEFAULT_WRITER_TEMPERATURE,
            "max_tokens": DEFAULT_WRITER_MAX_TOKENS,
        }
        if self._writer_model is not None:
            kwargs["model"] = self._writer_model
        return kwargs

    # ── Persistence ─────────────────────────────────────────────────────

    def _write_markdown(self, payload: BriefingPayload, markdown: str) -> Path:
        date = payload.date
        directory = self._root / f"{date.year:04d}" / f"{date.month:02d}" / f"{date.day:02d}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "briefing.md"
        path.write_text(markdown, encoding="utf-8")
        return path

    def _upsert_briefings_row(self, payload: BriefingPayload, path: Path) -> None:
        # html_path is required by the schema but the renderer that
        # produces it lives in step 19 (dashboard). For now we use the
        # same directory + briefing.html as the *planned* location;
        # the dashboard creates the file later. NOT NULL constraint is
        # satisfied by the path string.
        html_path = str(path.with_name("briefing.html"))
        self._conn.execute(
            """
            INSERT INTO briefings (
                date, generated_at, markdown_path, html_path, audio_path,
                peak_defcon, cluster_count, arc_count, open_arc_count
            )
            VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)
            ON CONFLICT (date) DO UPDATE SET
                generated_at   = excluded.generated_at,
                markdown_path  = excluded.markdown_path,
                html_path      = excluded.html_path,
                peak_defcon    = excluded.peak_defcon,
                cluster_count  = excluded.cluster_count,
                arc_count      = excluded.arc_count,
                open_arc_count = excluded.open_arc_count
            """,
            [
                payload.date,
                utcnow(),
                str(path),
                html_path,
                payload.peak_defcon,
                payload.cluster_count,
                payload.arc_count,
                payload.open_arc_count,
            ],
        )

    # ── stages_done ─────────────────────────────────────────────────────

    def _mark_stage_done(
        self,
        run_id: str,
        *,
        used_fallback: bool,
        sections_failed: list[int],
    ) -> None:
        row = self._conn.execute(
            "SELECT stages_done, counts FROM pipeline_runs WHERE run_id = ?",
            [run_id],
        ).fetchone()
        stages = json.loads(row[0]) if row and row[0] else []
        counts: dict[str, Any] = json.loads(row[1]) if row and row[1] else {}
        if "write" not in stages:
            stages.append("write")
        counts["writer_used_fallback"] = used_fallback
        counts["writer_sections_fallback"] = list(sections_failed)
        self._conn.execute(
            "UPDATE pipeline_runs SET stages_done = ?, counts = ? WHERE run_id = ?",
            [json.dumps(stages), json.dumps(counts), run_id],
        )


# ── Internal helpers ─────────────────────────────────────────────────────


class _SectionResult:
    """One section's outcome · (text, failed).

    ``text`` is what gets concatenated into the briefing markdown ·
    either the LLM body with prefilled marker, or the canonical empty
    note, or ``render_section_stub`` output.

    ``failed`` is True ONLY when the LLM was called and produced
    unusable output (validation failed or raised). Empty short-circuits
    are not failures · they are the correct deterministic answer.
    """

    __slots__ = ("text", "failed")

    def __init__(self, text: str, failed: bool) -> None:
        self.text = text
        self.failed = failed


def _is_section_empty(payload: BriefingPayload, idx: int) -> bool:
    """True when section idx has no payload data.

    Empty sections must not call the LLM · the 2026-05-27 hallucinated
    specimen confirmed Trendyol-LLM 7B invents content rather than
    emitting the empty-state phrase reliably.
    """
    if idx == 0:  # DEFCON 1-2 ÖNCELİKLİ
        return not _has_clusters_in(payload, BUCKET_PRIORITY)
    if idx == 1:  # DEFCON 3 MATERYAL
        return not _has_clusters_in(payload, BUCKET_MATERIAL)
    if idx == 2:  # AÇIK GELİŞMELER · DEVAM EDEN TAKİP
        return not payload.open_arc_updates
    if idx == 3:  # DEFCON 4 GÜNDEM
        return not _has_clusters_in(payload, BUCKET_ROUTINE)
    if idx == 4:  # DİKKAT · YALNIZCA SOSYALDE
        for levels in (BUCKET_PRIORITY, BUCKET_MATERIAL, BUCKET_ROUTINE):
            for level in levels:
                for cluster in payload.clusters_by_defcon.get(level, []):
                    if cluster.is_social_only:
                        return False
        return True
    if idx == 5:  # AMBİYANS · DEFCON 5
        return not _has_clusters_in(payload, BUCKET_AMBIENT)
    if idx == 6:  # KAPATILAN HİKAYELER
        return not payload.resolved_arcs
    return False


def _has_clusters_in(
    payload: BriefingPayload, levels: tuple[int, ...]
) -> bool:
    return any(payload.clusters_by_defcon.get(level) for level in levels)


__all__ = [
    "DEFAULT_BRIEFINGS_ROOT",
    "DEFAULT_WRITER_MAX_TOKENS",
    "DEFAULT_WRITER_TEMPERATURE",
    "EMPTY_SECTION_NOTE_TR",
    "Briefer",
]
