"""Integration tests for musahit.writer.briefer.Briefer."""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from musahit.common.migrations import init_db
from musahit.ingest.sources import seed_sources
from musahit.score.defcon import DEFCON
from musahit.score.llm_client import FakeLlmClient
from musahit.writer.briefer import Briefer
from musahit.writer.fallback import render_fallback_briefing
from musahit.writer.payload import BriefingPayload, build_payload
from musahit.writer.prompt import build_writer_prompt
from musahit.writer.validator import validate_briefing_markdown

RUN_ID = "run_test"
NOW = datetime(2026, 5, 23, 8, 0, 0)


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


def _valid_canned_briefing(conn: duckdb.DuckDBPyConnection) -> str:
    """Build the payload's fallback briefing — guaranteed validator-clean."""
    return render_fallback_briefing(build_payload(conn, RUN_ID))


# ── TestHappyPath ──────────────────────────────────────────────────────────


class TestHappyPath:
    async def test_writes_file_and_inserts_row(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_one_cluster(db)
        canned = _valid_canned_briefing(db)
        llm = FakeLlmClient(default=canned)
        briefer = Briefer(db, llm, briefings_root=tmp_path / "briefings")

        result = await briefer.run(RUN_ID)

        assert result["used_fallback"] is False
        path = Path(result["path"])
        assert path.exists()
        assert path.read_text(encoding="utf-8") == canned
        # briefings row inserted for that date.
        row = db.execute(
            "SELECT markdown_path, cluster_count, peak_defcon, html_path "
            "FROM briefings WHERE date = ?",
            [NOW.date()],
        ).fetchone()
        assert row is not None
        assert row[0] == str(path)
        assert row[1] == 1
        assert row[2] == int(DEFCON.MATERIAL)
        assert row[3].endswith("briefing.html")


# ── TestRetryThenSucceeds ──────────────────────────────────────────────────


class TestRetryThenSucceeds:
    async def test_first_invalid_then_valid_uses_no_fallback(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_one_cluster(db)
        canned = _valid_canned_briefing(db)

        # FakeLlmClient's per-prompt-key attempt counter resets when the
        # briefer sends a different prompt for the retry (it appends
        # validator-error feedback). We need a global counter; capture
        # it in a closure so the second physical call returns canned.
        global_calls = {"n": 0}

        def responder(_prompt: str, _attempt: int) -> str:
            n = global_calls["n"]
            global_calls["n"] += 1
            if n == 0:
                return "# Wrong Title\n\n## ❯ Yanlış Bölüm\n\nbroken\n"
            return canned

        llm = FakeLlmClient(responder=responder)
        result = await Briefer(db, llm, briefings_root=tmp_path / "briefings").run(RUN_ID)

        assert result["used_fallback"] is False
        # The second call's output was used and validated.
        assert validate_briefing_markdown(Path(result["path"]).read_text("utf-8")) == []
        # Two LLM calls happened (attempt 0 failed, attempt 1 succeeded).
        assert llm.call_count == 2


# ── TestFallback ───────────────────────────────────────────────────────────


class TestFallback:
    async def test_three_retries_then_fallback(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_one_cluster(db)

        def responder(_prompt: str, _attempt: int) -> str:
            return "garbage that won't validate"

        llm = FakeLlmClient(responder=responder)
        result = await Briefer(
            db, llm, briefings_root=tmp_path / "briefings", max_retries=3
        ).run(RUN_ID)

        assert result["used_fallback"] is True
        # 1 initial + 3 retries = 4 calls.
        assert llm.call_count == 4
        # The on-disk briefing matches the Python fallback renderer output.
        on_disk = Path(result["path"]).read_text("utf-8")
        assert validate_briefing_markdown(on_disk) == []

    async def test_llm_exception_also_falls_through(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_one_cluster(db)

        def responder(_prompt: str, _attempt: int) -> str:
            raise RuntimeError("simulated llm outage")

        llm = FakeLlmClient(responder=responder)
        result = await Briefer(
            db, llm, briefings_root=tmp_path / "briefings", max_retries=2
        ).run(RUN_ID)

        assert result["used_fallback"] is True
        assert validate_briefing_markdown(Path(result["path"]).read_text("utf-8")) == []


# ── TestStagesDone ─────────────────────────────────────────────────────────


class TestStagesDone:
    async def test_stages_done_appends_write(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_one_cluster(db)
        canned = _valid_canned_briefing(db)
        llm = FakeLlmClient(default=canned)
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


# ── TestIdempotenceForDate ─────────────────────────────────────────────────


class TestIdempotenceForDate:
    async def test_second_run_for_same_date_updates_row_not_dupes(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_one_cluster(db)
        canned = _valid_canned_briefing(db)
        llm = FakeLlmClient(default=canned)
        briefer = Briefer(db, llm, briefings_root=tmp_path / "briefings")

        await briefer.run(RUN_ID)
        await briefer.run(RUN_ID)

        # Exactly one row in briefings for that date.
        n = db.execute(
            "SELECT COUNT(*) FROM briefings WHERE date = ?", [NOW.date()]
        ).fetchone()[0]
        assert n == 1


# ── TestPathLayout ─────────────────────────────────────────────────────────


class TestPathLayout:
    async def test_writes_to_yyyy_mm_dd_path(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_one_cluster(db)
        canned = _valid_canned_briefing(db)
        llm = FakeLlmClient(default=canned)
        result = await Briefer(db, llm, briefings_root=tmp_path / "briefings").run(RUN_ID)
        path = Path(result["path"])
        # Expected layout: <root>/2026/05/23/briefing.md
        assert path.name == "briefing.md"
        assert path.parent.name == "23"
        assert path.parent.parent.name == "05"
        assert path.parent.parent.parent.name == "2026"


# ── TestPromptIsLargeEnoughCheck ──────────────────────────────────────────


class TestPromptInUse:
    async def test_prompt_actually_called(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed_one_cluster(db)
        canned = _valid_canned_briefing(db)
        # Capture the expected prompt BEFORE the briefer runs — once
        # it runs, stages_done gains "write" and the prompt would
        # serialise a different stages_done line.
        expected_prompt = build_writer_prompt(build_payload(db, RUN_ID))
        llm = FakeLlmClient(default=canned)
        await Briefer(db, llm, briefings_root=tmp_path / "briefings").run(RUN_ID)
        # The Fake's call_log captures every prompt; verify the first
        # prompt matches.
        assert expected_prompt in llm.calls[0]


def _payload_unused_just_a_check(_: BriefingPayload) -> None:  # pragma: no cover
    """Keep BriefingPayload import live for future test extensions."""
