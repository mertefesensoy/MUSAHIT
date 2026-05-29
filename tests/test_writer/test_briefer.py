"""Integration tests for musahit.writer.briefer.Briefer."""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from musahit.common.migrations import init_db
from musahit.ingest.sources import seed_sources
from musahit.score.llm_client import FakeLlmClient
from musahit.writer.briefer import Briefer
from musahit.writer.payload import BriefingPayload
from musahit.writer.template import DOCUMENT_TITLE, TEMPLATE_SECTIONS
from musahit.writer.validator import validate_briefing_markdown

RUN_ID = "run_test"
NOW = datetime(2026, 5, 23, 8, 0, 0)

VALID_SECTION_CONTENT = "İçerik burada.\n"


@pytest.fixture()
def db(tmp_path: Path) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    db_path = tmp_path / "x.duckdb"
    init_db(db_path, load_vss=False)
    conn = duckdb.connect(str(db_path))
    seed_sources(conn)
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, started_at, status, stages_done, counts) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            RUN_ID,
            NOW,
            "RUNNING",
            json.dumps(["ingest", "normalize", "cluster", "score", "arc-link"]),
            json.dumps({}),
        ],
    )
    yield conn
    conn.close()


def _seed_one_cluster(conn: duckdb.DuckDBPyConnection) -> None:
    """Minimum payload: one MATERIAL cluster so the run isn't trivially empty."""
    conn.execute(
        "INSERT INTO clusters (id, created_at, headline, summary, category, raw_defcon, "
        "ceiling_defcon, final_defcon, confidence, bands_present, arc_id, operator_override) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
        [
            "cl_test",
            NOW,
            "Test başlığı",
            "Tek cümlelik özet.",
            "POLİTİKA",
            3,
            4,
            3,
            "ORTA",
            json.dumps(["centrist"]),
        ],
    )
    conn.execute(
        "INSERT OR IGNORE INTO ingest_log (run_id, source_id, started_at, completed_at, "
        "status, articles_fetched) VALUES (?, ?, ?, ?, ?, ?)",
        [RUN_ID, "bianet", NOW, NOW, "OK", 1],
    )
    conn.execute(
        "INSERT INTO articles (id, source_id, url, fetched_at, published_at, "
        "title, lead, body, language, entities, word_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ["a1", "bianet", "u/a1", NOW, NOW, "t", "l", "b", "tr", "[]", 40],
    )
    conn.execute(
        "INSERT INTO cluster_articles (cluster_id, article_id) VALUES (?, ?)",
        ["cl_test", "a1"],
    )


def _seed_all_section_buckets(conn: duckdb.DuckDBPyConnection) -> None:
    """Populate every LLM-driven section (idx 0-6) with at least one row.

    Empty sections short-circuit the LLM (no call, canonical "öğe yok"
    note). Tests that want to verify the seven-calls-per-section
    contract must seed each bucket explicitly so the empty-section
    short-circuit does not collapse the call count.

    Seeded:
      * idx 0 · DEFCON 1-2 priority · final_defcon=2 cluster.
      * idx 1 · DEFCON 3 material · final_defcon=3 cluster.
      * idx 2 · open arc · OPEN arc joined by a cluster this run.
      * idx 3 · DEFCON 4 routine · final_defcon=4 cluster.
      * idx 4 · social-only · DEFCON 3 cluster with bands=[social_x].
      * idx 5 · DEFCON 5 ambient · final_defcon=5 cluster.
      * idx 6 · resolved arc · arc with state=RESOLVED, last_update_at
        inside the run window.
    """
    # Ingest log row · one per source we cite. Sources picked from
    # seed_sources() so the FK on ingest_log.source_id holds.
    sources = ["bianet", "cumhuriyet", "sabah", "diken", "hurriyet"]
    for src in sources:
        conn.execute(
            "INSERT OR IGNORE INTO ingest_log (run_id, source_id, started_at, "
            "completed_at, status, articles_fetched) VALUES (?, ?, ?, ?, ?, ?)",
            [RUN_ID, src, NOW, NOW, "OK", 1],
        )

    # Helper: insert one cluster with one article linked.
    def _add(
        cluster_id: str,
        article_id: str,
        source_id: str,
        defcon: int,
        bands: list[str],
        *,
        arc_id: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO clusters (id, created_at, headline, summary, category, "
            "raw_defcon, ceiling_defcon, final_defcon, confidence, bands_present, "
            "arc_id, operator_override) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            [
                cluster_id, NOW, f"Başlık {cluster_id}", "Özet.",
                "POLİTİKA", defcon, defcon, defcon, "ORTA",
                json.dumps(bands), arc_id,
            ],
        )
        conn.execute(
            "INSERT INTO articles (id, source_id, url, fetched_at, published_at, "
            "title, lead, body, language, entities, word_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                article_id, source_id, f"u/{article_id}", NOW, NOW,
                "t", "l", "b", "tr", "[]", 40,
            ],
        )
        conn.execute(
            "INSERT INTO cluster_articles (cluster_id, article_id) VALUES (?, ?)",
            [cluster_id, article_id],
        )

    _add("cl_priority", "a_priority", "bianet", 2, ["centrist"])
    _add("cl_material", "a_material", "cumhuriyet", 3, ["opposition"])
    _add("cl_routine", "a_routine", "sabah", 4, ["gov_aligned"])
    _add("cl_social", "a_social", "diken", 3, ["social_x"])  # social-only
    _add("cl_ambient", "a_ambient", "hurriyet", 5, ["centrist"])

    # Open arc · joined by a cluster from this run so the open_arc
    # query picks it up. last_update_at >= run.started_at marks it
    # is_active_today.
    conn.execute(
        "INSERT INTO arcs (id, headline, summary, state, peak_defcon, category, "
        "created_at, last_update_at, last_update_summary, last_update_headline, "
        "last_update_cluster_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "arc_open_001", "Açık hikaye", "Devam ediyor.", "OPEN",
            3, "POLİTİKA", NOW, NOW, "Yeni gelişme.", "Yeni gelişme",
            "cl_arc_join",
        ],
    )
    _add(
        "cl_arc_join", "a_arc_join", "bianet", 3, ["centrist"],
        arc_id="arc_open_001",
    )

    # Resolved arc · closed today (last_update_at in the run window).
    conn.execute(
        "INSERT INTO arcs (id, headline, summary, state, peak_defcon, category, "
        "created_at, last_update_at, last_update_summary, last_update_headline, "
        "last_update_cluster_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "arc_resolved_001", "Kapatılan hikaye", "Çözüldü.", "RESOLVED",
            3, "POLİTİKA", NOW, NOW, "", "", None,
        ],
    )


# ── TestHappyPath (rewritten for per-section) ────────────────────────────


class TestHappyPath:
    async def test_per_section_compose(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # Seed data in every LLM section so the empty-section
        # short-circuit does not collapse the call count.
        _seed_all_section_buckets(db)
        llm = FakeLlmClient(default=VALID_SECTION_CONTENT)
        briefer = Briefer(db, llm, briefings_root=tmp_path / "briefings")

        result = await briefer.run(RUN_ID)

        # 2026-05-29 Group-A: only DEFCON 1-2 (0) and DEFCON 3 (1) call the
        # LLM; sections 2-6 are rendered deterministically (no LLM call).
        assert llm.call_count == 2
        for i in (0, 1):
            expected_prefill = f"{TEMPLATE_SECTIONS[i].marker}\n\n"
            assert llm.prefill_calls[i] == expected_prefill
        assert result["used_fallback"] is False
        assert result["sections_failed"] == []
        path = Path(result["path"])
        assert path.exists()
        on_disk = path.read_text(encoding="utf-8")
        assert on_disk.startswith(DOCUMENT_TITLE)
        assert validate_briefing_markdown(on_disk) == []
        row = db.execute(
            "SELECT markdown_path, cluster_count, peak_defcon, html_path "
            "FROM briefings WHERE date = ?",
            [NOW.date()],
        ).fetchone()
        assert row is not None
        assert row[0] == str(path)


# ── TestPerSectionFailure ────────────────────────────────────────────────


class TestPerSectionFailure:
    async def test_per_section_failure_produces_stub(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_all_section_buckets(db)
        call_counter = {"n": 0}

        def responder(_prompt: str, _attempt: int) -> str:
            n = call_counter["n"]
            call_counter["n"] += 1
            # LLM sections are invoked in idx order (0 then 1); the 2nd
            # LLM call is idx=1 (DEFCON 3). Make that one return a wrong
            # header so the per-section validator rejects it.
            if n == 1:
                return "## ❯ WRONG HEADER\n\nBad content"
            return VALID_SECTION_CONTENT

        llm = FakeLlmClient(responder=responder)
        result = await Briefer(
            db, llm, briefings_root=tmp_path / "briefings"
        ).run(RUN_ID)

        assert result["sections_failed"] == [1]
        assert result["used_fallback"] is False
        on_disk = Path(result["path"]).read_text("utf-8")
        assert "Bu bölüm üretilemedi" in on_disk
        other_sections = [i for i in range(7) if i != 1]
        for i in other_sections:
            assert TEMPLATE_SECTIONS[i].marker in on_disk
        assert "Başarısız bölüm üretimi" in on_disk
        section_1_title = TEMPLATE_SECTIONS[1].marker.removeprefix("## ❯ ")
        assert section_1_title in on_disk


# ── TestAllLlmSectionsFail ───────────────────────────────────────────────


class TestAllLlmSectionsFail:
    async def test_all_llm_sections_fail_marks_not_full_fallback(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        """Both LLM sections (0,1) fail → 2 stubs; the deterministic
        sections (2-6) and SİSTEM LOG (7) still render, so this is NOT
        full fallback and the assembled markdown still validates."""
        _seed_all_section_buckets(db)

        def responder(_prompt: str, _attempt: int) -> str:
            return "## ❯ WRONG\nBad"

        llm = FakeLlmClient(responder=responder)
        result = await Briefer(
            db, llm, briefings_root=tmp_path / "briefings"
        ).run(RUN_ID)

        assert result["sections_failed"] == [0, 1]
        assert result["used_fallback"] is False
        on_disk = Path(result["path"]).read_text("utf-8")
        assert validate_briefing_markdown(on_disk) == []
        assert TEMPLATE_SECTIONS[7].marker in on_disk


# ── TestFinalValidationFailure ───────────────────────────────────────────


class TestFinalValidationFailure:
    async def test_final_validation_failure_triggers_full_fallback(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        """When per-section validator passes but the final assembled
        markdown fails validate_briefing_markdown, the full fallback
        fires as a last-resort safety net."""
        _seed_one_cluster(db)
        llm = FakeLlmClient(default=VALID_SECTION_CONTENT)
        briefer = Briefer(db, llm, briefings_root=tmp_path / "briefings")

        with (
            patch(
                "musahit.writer.briefer.validate_section",
                return_value=True,
            ),
            patch(
                "musahit.writer.briefer.validate_briefing_markdown",
                return_value=["forced error"],
            ),
        ):
            result = await briefer.run(RUN_ID)

        assert result["used_fallback"] is True
        assert result["sections_failed"] == list(range(8))
        on_disk = Path(result["path"]).read_text("utf-8")
        assert validate_briefing_markdown(on_disk) == []


# ── TestStagesDone ────────────────────────────────────────────────────────


class TestStagesDone:
    async def test_stages_done_appends_write(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_one_cluster(db)
        llm = FakeLlmClient(default=VALID_SECTION_CONTENT)
        await Briefer(db, llm, briefings_root=tmp_path / "briefings").run(RUN_ID)

        row = db.execute(
            "SELECT stages_done, counts FROM pipeline_runs WHERE run_id = ?",
            [RUN_ID],
        ).fetchone()
        stages = json.loads(row[0])
        counts = json.loads(row[1])
        assert "write" in stages
        assert stages[-1] == "write"
        assert counts.get("writer_used_fallback") is False
        assert counts.get("writer_sections_fallback") == []


# ── TestIdempotenceForDate ───────────────────────────────────────────────


class TestIdempotenceForDate:
    async def test_second_run_for_same_date_updates_row_not_dupes(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_one_cluster(db)
        llm = FakeLlmClient(default=VALID_SECTION_CONTENT)
        briefer = Briefer(db, llm, briefings_root=tmp_path / "briefings")

        await briefer.run(RUN_ID)
        await briefer.run(RUN_ID)

        n = db.execute(
            "SELECT COUNT(*) FROM briefings WHERE date = ?", [NOW.date()]
        ).fetchone()[0]
        assert n == 1


# ── TestPathLayout ────────────────────────────────────────────────────────


class TestPathLayout:
    async def test_writes_to_yyyy_mm_dd_path(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_one_cluster(db)
        llm = FakeLlmClient(default=VALID_SECTION_CONTENT)
        result = await Briefer(
            db, llm, briefings_root=tmp_path / "briefings"
        ).run(RUN_ID)
        path = Path(result["path"])
        assert path.name == "briefing.md"
        assert path.parent.name == "23"
        assert path.parent.parent.name == "05"
        assert path.parent.parent.parent.name == "2026"


# ── TestTargetDateInBriefer ──────────────────────────────────────────────


class TestTargetDateInBriefer:
    async def test_target_date_drives_markdown_directory(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_one_cluster(db)
        llm = FakeLlmClient(default=VALID_SECTION_CONTENT)
        tr_today = date(2026, 5, 24)
        result = await Briefer(
            db,
            llm,
            briefings_root=tmp_path / "briefings",
            target_date=tr_today,
        ).run(RUN_ID)
        path = Path(result["path"])
        assert path.parent.name == "24"
        assert path.parent.parent.name == "05"
        assert path.parent.parent.parent.name == "2026"

    async def test_target_date_drives_briefings_row_date(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_one_cluster(db)
        tr_today = date(2026, 5, 24)
        await Briefer(
            db,
            FakeLlmClient(default=VALID_SECTION_CONTENT),
            briefings_root=tmp_path / "briefings",
            target_date=tr_today,
        ).run(RUN_ID)
        row = db.execute(
            "SELECT date FROM briefings WHERE date = ?", [tr_today]
        ).fetchone()
        assert row is not None
        assert row[0] == tr_today

    async def test_omitting_target_date_falls_back_to_started_at(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_one_cluster(db)
        result = await Briefer(
            db,
            FakeLlmClient(default=VALID_SECTION_CONTENT),
            briefings_root=tmp_path / "briefings",
        ).run(RUN_ID)
        path = Path(result["path"])
        assert path.parent.name == "23"


# ── TestPrefillWiring ────────────────────────────────────────────────────


class TestPrefillWiring:
    async def test_briefer_uses_per_section_prefill(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_all_section_buckets(db)
        llm = FakeLlmClient(default=VALID_SECTION_CONTENT)
        await Briefer(db, llm, briefings_root=tmp_path / "briefings").run(RUN_ID)
        assert len(llm.prefill_calls) == 2
        for i in (0, 1):
            assert llm.prefill_calls[i].startswith(TEMPLATE_SECTIONS[i].marker)

    async def test_written_markdown_starts_with_document_title(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_one_cluster(db)
        llm = FakeLlmClient(default=VALID_SECTION_CONTENT)
        result = await Briefer(
            db, llm, briefings_root=tmp_path / "briefings"
        ).run(RUN_ID)
        on_disk = Path(result["path"]).read_text("utf-8")
        assert on_disk.startswith(DOCUMENT_TITLE)


# ── TestLlmException ─────────────────────────────────────────────────────


class TestLlmException:
    async def test_llm_exception_produces_stub(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_all_section_buckets(db)

        def responder(_prompt: str, _attempt: int) -> str:
            raise RuntimeError("simulated llm outage")

        llm = FakeLlmClient(responder=responder)
        result = await Briefer(
            db, llm, briefings_root=tmp_path / "briefings"
        ).run(RUN_ID)

        assert result["sections_failed"] == [0, 1]
        assert result["used_fallback"] is False
        assert validate_briefing_markdown(
            Path(result["path"]).read_text("utf-8")
        ) == []


# ── Issue 3b · Empty-section short-circuit & anti-hallucination ─────────


class TestEmptySectionShortCircuit:
    async def test_empty_section_skips_llm_emits_note(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        """Single MATERIAL cluster only · 6 sections are empty.
        Empty sections must NOT call the LLM and must emit the canonical
        'Bugün bu bölümde öğe yok.' note. The 2026-05-27 hallucinated
        specimen showed Trendyol fabricates COVID/crime headlines when
        given an empty section · this path eliminates the failure mode."""
        _seed_one_cluster(db)
        llm = FakeLlmClient(default=VALID_SECTION_CONTENT)
        result = await Briefer(
            db, llm, briefings_root=tmp_path / "briefings"
        ).run(RUN_ID)

        # Only idx=1 (MATERIAL) has data · 1 LLM call, 6 short-circuits.
        assert llm.call_count == 1
        assert llm.prefill_calls[0].startswith(TEMPLATE_SECTIONS[1].marker)
        on_disk = Path(result["path"]).read_text("utf-8")
        # Each empty section's marker plus the canonical empty-note must
        # be present in the markdown.
        empty_indices = [0, 2, 3, 4, 5, 6]
        for idx in empty_indices:
            assert TEMPLATE_SECTIONS[idx].marker in on_disk
        assert "Bugün bu bölümde öğe yok." in on_disk

    async def test_empty_sections_do_not_count_as_fallback(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        """Empty sections are deterministic correctness, not failures.
        writer_sections_fallback must NOT include them."""
        _seed_one_cluster(db)
        llm = FakeLlmClient(default=VALID_SECTION_CONTENT)
        result = await Briefer(
            db, llm, briefings_root=tmp_path / "briefings"
        ).run(RUN_ID)

        assert result["used_fallback"] is False
        # Only 1 cluster (MATERIAL · idx=1) · other 6 LLM sections
        # short-circuited. None should be in sections_failed.
        assert result["sections_failed"] == []
        row = db.execute(
            "SELECT counts FROM pipeline_runs WHERE run_id = ?", [RUN_ID]
        ).fetchone()
        counts = json.loads(row[0])
        assert counts.get("writer_used_fallback") is False
        assert counts.get("writer_sections_fallback") == []


class TestValidatorRejectsPromptEcho:
    async def test_section_with_prompt_echo_becomes_stub(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        """The 2026-05-27 DİKKAT section echoed DISCIPLINE_RULES verbatim.
        Such a section must be rejected and replaced with a stub."""
        _seed_one_cluster(db)
        # idx=1 (MATERIAL) is the only non-empty LLM section.
        echo_text = (
            "KURALLAR (ADR-009):\n"
            "- Yorum yapma · sadece raporla.\n\n"
            "BÖLÜM VERİSİ:\n(bugün öğe yok)\n"
        )

        def responder(_prompt: str, _attempt: int) -> str:
            return echo_text

        llm = FakeLlmClient(responder=responder)
        result = await Briefer(
            db, llm, briefings_root=tmp_path / "briefings"
        ).run(RUN_ID)

        # idx=1 rejected · listed in sections_failed.
        assert 1 in result["sections_failed"]
        on_disk = Path(result["path"]).read_text("utf-8")
        # No DISCIPLINE_RULES echo present anywhere in the briefing.
        assert "KURALLAR (ADR-009)" not in on_disk
        assert "BÖLÜM VERİSİ:" not in on_disk
        assert "Bu bölüm üretilemedi" in on_disk

    async def test_section_with_cot_scaffolding_becomes_stub(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        """The 2026-05-27 AMBİYANS section emitted 'Adım 1:' / 'Gerekçe:'
        reasoning scaffolding. Such a section must become a stub."""
        _seed_one_cluster(db)
        cot_text = (
            "Adım 1: Türkiye'nin son gündemini özetleyin.\n"
            "Gerekçe: Tarafsız bir bakış açısı sağlamak için.\n\n"
            "Bazı içerik buradadır.\n"
        )

        def responder(_prompt: str, _attempt: int) -> str:
            return cot_text

        llm = FakeLlmClient(responder=responder)
        result = await Briefer(
            db, llm, briefings_root=tmp_path / "briefings"
        ).run(RUN_ID)

        assert 1 in result["sections_failed"]
        on_disk = Path(result["path"]).read_text("utf-8")
        # No CoT scaffolding leaked into the briefing.
        assert "Adım 1:" not in on_disk
        assert "Gerekçe:" not in on_disk


class TestDeterministicSectionsNoLlmCall:
    """2026-05-29 Group-A · the itemized sections (2-6) must render from
    payload data with NO LLM call, so Mode-4 fabrication is impossible.

    The FakeLlmClient is wired to emit a recognizable garbage marker on
    every call; we assert (a) it was called exactly twice (idx 0,1) and
    (b) the garbage never lands in the deterministic AÇIK GELİŞMELER /
    AMBİYANS / DEFCON 4 sections, which instead carry itemized data with
    recency suffixes.
    """

    async def test_itemized_sections_are_data_not_llm(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_all_section_buckets(db)
        garbage = "İş Bankası kredi paketi açıkladı · Guantanamo"

        def responder(_prompt: str, _attempt: int) -> str:
            # Valid-shaped LLM output (won't be rejected) but containing a
            # fabrication marker. If any deterministic section were routed
            # through the LLM, this marker would leak into it.
            return f"{garbage}\n"

        llm = FakeLlmClient(responder=responder)
        result = await Briefer(
            db, llm, briefings_root=tmp_path / "briefings"
        ).run(RUN_ID)

        # Only DEFCON 1-2 and DEFCON 3 call the LLM.
        assert llm.call_count == 2

        on_disk = Path(result["path"]).read_text("utf-8")
        sections = _split_sections(on_disk)
        for marker in (
            "## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP",
            "## ❯ DEFCON 4 · GÜNDEM",
            "## ❯ AMBİYANS · DEFCON 5",
        ):
            assert garbage not in sections[marker], (
                f"fabrication leaked into deterministic section {marker}"
            )
        # The deterministic open-arc / DEFCON-4 sections carry recency data.
        açık = sections["## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP"]
        assert "arc_open_001" in açık
        assert "bugün" in açık


def _split_sections(markdown: str) -> dict[str, str]:
    """Bucket markdown body lines under their ``## ❯`` marker for assertions."""
    out: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("## ❯"):
            if current is not None:
                out[current] = "\n".join(buf)
            current = line.strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf)
    return out


def _payload_unused_just_a_check(_: BriefingPayload) -> None:  # pragma: no cover
    """Keep BriefingPayload import live for future test extensions."""
