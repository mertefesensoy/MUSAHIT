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


# ── TestHappyPath (rewritten for per-section) ────────────────────────────


class TestHappyPath:
    async def test_per_section_compose(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_one_cluster(db)
        llm = FakeLlmClient(default=VALID_SECTION_CONTENT)
        briefer = Briefer(db, llm, briefings_root=tmp_path / "briefings")

        result = await briefer.run(RUN_ID)

        assert llm.call_count == 7
        for i in range(7):
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
        _seed_one_cluster(db)
        call_counter = {"n": 0}

        def responder(_prompt: str, _attempt: int) -> str:
            n = call_counter["n"]
            call_counter["n"] += 1
            if n == 3:
                return "## ❯ WRONG HEADER\n\nBad content"
            return VALID_SECTION_CONTENT

        llm = FakeLlmClient(responder=responder)
        result = await Briefer(
            db, llm, briefings_root=tmp_path / "briefings"
        ).run(RUN_ID)

        assert result["sections_failed"] == [3]
        assert result["used_fallback"] is False
        on_disk = Path(result["path"]).read_text("utf-8")
        assert "Bu bölüm üretilemedi" in on_disk
        other_sections = [i for i in range(7) if i != 3]
        for i in other_sections:
            assert TEMPLATE_SECTIONS[i].marker in on_disk
        assert "Başarısız bölüm üretimi" in on_disk
        section_3_title = TEMPLATE_SECTIONS[3].marker.removeprefix("## ❯ ")
        assert section_3_title in on_disk


# ── TestAllLlmSectionsFail ───────────────────────────────────────────────


class TestAllLlmSectionsFail:
    async def test_all_llm_sections_fail_marks_not_full_fallback(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        """All 7 LLM sections fail → 7 stubs + 1 real SİSTEM LOG.
        Per Decision 1 this is NOT full fallback since SİSTEM LOG
        (idx 7) is deterministic and always succeeds."""
        _seed_one_cluster(db)

        def responder(_prompt: str, _attempt: int) -> str:
            return "## ❯ WRONG\nBad"

        llm = FakeLlmClient(responder=responder)
        result = await Briefer(
            db, llm, briefings_root=tmp_path / "briefings"
        ).run(RUN_ID)

        assert result["sections_failed"] == [0, 1, 2, 3, 4, 5, 6]
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
        _seed_one_cluster(db)
        llm = FakeLlmClient(default=VALID_SECTION_CONTENT)
        await Briefer(db, llm, briefings_root=tmp_path / "briefings").run(RUN_ID)
        assert len(llm.prefill_calls) == 7
        for i in range(7):
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
        _seed_one_cluster(db)

        def responder(_prompt: str, _attempt: int) -> str:
            raise RuntimeError("simulated llm outage")

        llm = FakeLlmClient(responder=responder)
        result = await Briefer(
            db, llm, briefings_root=tmp_path / "briefings"
        ).run(RUN_ID)

        assert result["sections_failed"] == [0, 1, 2, 3, 4, 5, 6]
        assert result["used_fallback"] is False
        assert validate_briefing_markdown(
            Path(result["path"]).read_text("utf-8")
        ) == []


def _payload_unused_just_a_check(_: BriefingPayload) -> None:  # pragma: no cover
    """Keep BriefingPayload import live for future test extensions."""
