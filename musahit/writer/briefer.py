"""Writer orchestrator: build payload → call LLM → validate → retry / fallback → persist.

Per ADR-012 § Stage 6 Writer the writer always produces *something*.
The LLM is called up to ``max_retries + 1`` times; the first call uses
the bare prompt, each retry appends the validator's specific errors so
the model can correct itself. After the final failure we fall through
to :func:`musahit.writer.fallback.render_fallback_briefing` — the
operator gets a structurally-valid briefing in any case.

The briefing markdown is written to
``briefings/YYYY/MM/DD/briefing.md`` and a ``briefings`` row is
inserted (or updated) with the day's aggregate counts. Multiple runs
on the same date overwrite the file and update the row — the briefing
is per-date, not per-run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from musahit.common.logging import get_logger
from musahit.common.time import utcnow
from musahit.score.llm_client import LlmClient
from musahit.writer.fallback import render_fallback_briefing
from musahit.writer.payload import BriefingPayload, build_payload
from musahit.writer.prompt import build_writer_prompt
from musahit.writer.validator import validate_briefing_markdown

_log = get_logger("musahit.writer")

DEFAULT_MAX_RETRIES: int = 3
DEFAULT_BRIEFINGS_ROOT: Path = Path("briefings")
DEFAULT_WRITER_TEMPERATURE: float = 0.3
DEFAULT_WRITER_MAX_TOKENS: int = 4096


class Briefer:
    """Compose, validate, and persist the nightly Markdown briefing."""

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        llm: LlmClient,
        briefings_root: Path = DEFAULT_BRIEFINGS_ROOT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        writer_model: str | None = None,
    ) -> None:
        self._conn = conn
        self._llm = llm
        self._root = Path(briefings_root)
        self._max_retries = max_retries
        self._writer_model = writer_model

    async def run(self, run_id: str) -> dict[str, Any]:
        log = _log.bind(run_id=run_id)
        payload = build_payload(self._conn, run_id)

        markdown, used_fallback = await self._compose(payload, log)
        path = self._write_markdown(payload, markdown)
        self._upsert_briefings_row(payload, path)
        self._mark_stage_done(run_id, used_fallback=used_fallback)

        log.info(
            "writer_done",
            used_fallback=used_fallback,
            cluster_count=payload.cluster_count,
            arc_count=payload.arc_count,
            open_arc_count=payload.open_arc_count,
            peak_defcon=payload.peak_defcon,
            path=str(path),
        )
        return {
            "path": str(path),
            "used_fallback": used_fallback,
            "cluster_count": payload.cluster_count,
            "arc_count": payload.arc_count,
            "open_arc_count": payload.open_arc_count,
            "peak_defcon": payload.peak_defcon,
        }

    # ── Compose loop ────────────────────────────────────────────────────

    async def _compose(self, payload: BriefingPayload, log: Any) -> tuple[str, bool]:
        """Try the LLM up to ``max_retries+1`` times; fall through to fallback."""
        base_prompt = build_writer_prompt(payload)
        prompt = base_prompt
        last_errors: list[str] = []
        for attempt in range(self._max_retries + 1):
            kwargs = self._llm_kwargs()
            try:
                raw = await self._llm.generate(prompt, **kwargs)
            except Exception as exc:
                log.warning(
                    "writer_llm_error",
                    attempt=attempt,
                    error=f"{type(exc).__name__}: {exc}",
                )
                last_errors = [f"LLM error: {type(exc).__name__}: {exc}"]
                prompt = self._retry_prompt(base_prompt, last_errors)
                continue
            errors = validate_briefing_markdown(raw)
            if not errors:
                return raw, False
            last_errors = errors
            log.warning(
                "writer_validator_failed",
                attempt=attempt,
                errors=errors[:3],
            )
            prompt = self._retry_prompt(base_prompt, errors)
        # All retries exhausted — fallback.
        log.warning("writer_fallback", last_errors=last_errors[:3])
        return render_fallback_briefing(payload), True

    def _llm_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "temperature": DEFAULT_WRITER_TEMPERATURE,
            "max_tokens": DEFAULT_WRITER_MAX_TOKENS,
        }
        if self._writer_model is not None:
            kwargs["model"] = self._writer_model
        return kwargs

    def _retry_prompt(self, base_prompt: str, errors: list[str]) -> str:
        if not errors:
            return base_prompt
        complaint = "\n".join(f"- {e}" for e in errors[:5])
        return (
            base_prompt
            + "\n\nÖNCEKİ DENEMEDE ŞABLON DOĞRULAYICI HATA VERDİ:\n"
            + complaint
            + "\nLütfen şablon yapısını korumayı tekrar dene. Aynı bölüm "
            "başlıklarını harfi harfine kullan."
        )

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

    def _mark_stage_done(self, run_id: str, *, used_fallback: bool) -> None:
        row = self._conn.execute(
            "SELECT stages_done, counts FROM pipeline_runs WHERE run_id = ?",
            [run_id],
        ).fetchone()
        stages = json.loads(row[0]) if row and row[0] else []
        counts: dict[str, Any] = json.loads(row[1]) if row and row[1] else {}
        if "write" not in stages:
            stages.append("write")
        counts["writer_used_fallback"] = used_fallback
        self._conn.execute(
            "UPDATE pipeline_runs SET stages_done = ?, counts = ? WHERE run_id = ?",
            [json.dumps(stages), json.dumps(counts), run_id],
        )


__all__ = [
    "DEFAULT_BRIEFINGS_ROOT",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_WRITER_MAX_TOKENS",
    "DEFAULT_WRITER_TEMPERATURE",
    "Briefer",
]
