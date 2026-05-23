"""Tests for musahit.ingest.poller — the ingest orchestrator.

Every test injects fake ingesters through ``IngestPoller``'s
``ingester_factory`` kwarg so no real HTTP, PRAW, or pdfplumber call
happens. The DB schema is the standard in-memory fixture used by every
other ingester test.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest

from musahit.common.migrations import init_db
from musahit.common.types import IngestStatus, PipelineStatus, SourceKind
from musahit.ingest import IngestResult
from musahit.ingest.html import HtmlIngester
from musahit.ingest.poller import (
    DEFAULT_MAX_CONCURRENT,
    IngestPoller,
    get_ingester,
)
from musahit.ingest.reddit import RedditIngester
from musahit.ingest.resmi_gazete import ResmiGazeteIngester
from musahit.ingest.rss import RssIngester
from musahit.ingest.sources import SOURCES, seed_sources

# ── Fakes ──────────────────────────────────────────────────────────────────


class FakeIngester:
    """Stand-in for any real ingester. Returns whatever the test scripted."""

    def __init__(
        self,
        result: IngestResult | None = None,
        *,
        raises: type[BaseException] | None = None,
        sleep_seconds: float = 0.0,
        tracker: ConcurrencyTracker | None = None,
    ) -> None:
        self._result = result or IngestResult(status=IngestStatus.OK, count=1)
        self._raises = raises
        self._sleep = sleep_seconds
        self._tracker = tracker
        self.calls: list[str] = []

    async def fetch(self, source: Any) -> IngestResult:
        self.calls.append(source.id)
        if self._tracker is not None:
            self._tracker.enter()
        try:
            if self._sleep:
                await asyncio.sleep(self._sleep)
            if self._raises is not None:
                raise self._raises("simulated")
            return self._result
        finally:
            if self._tracker is not None:
                self._tracker.exit()


class ConcurrencyTracker:
    """Records the peak number of simultaneous fetches."""

    def __init__(self) -> None:
        self.current = 0
        self.max_concurrent = 0

    def enter(self) -> None:
        self.current += 1
        if self.current > self.max_concurrent:
            self.max_concurrent = self.current

    def exit(self) -> None:
        self.current -= 1


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def ingest_db(tmp_path: Path) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    db_path = tmp_path / "test.duckdb"
    init_db(db_path, load_vss=False)
    conn = duckdb.connect(str(db_path))
    seed_sources(conn)
    try:
        yield conn
    finally:
        conn.close()


# A small, deterministic subset of the canonical registry — all four kinds
# are represented so dispatch tests still exercise SourceKind diversity.
def _test_sources() -> tuple[Any, ...]:
    by_kind = {k: [] for k in (SourceKind.RSS, SourceKind.HTML, SourceKind.PDF, SourceKind.API)}
    for source in SOURCES:
        if source.kind in by_kind and len(by_kind[source.kind]) < 1:
            by_kind[source.kind].append(source)
    # Add a few more RSS sources so we have >8 for the concurrency test.
    rss_sources = [s for s in SOURCES if s.kind is SourceKind.RSS][:10]
    flat: list[Any] = []
    for v in by_kind.values():
        flat.extend(v)
    extras = [s for s in rss_sources if s not in flat]
    return tuple(flat + extras[: 11 - len(flat)])


# ── TestSuccessfulRun ──────────────────────────────────────────────────────


class TestSuccessfulRun:
    async def test_pipeline_runs_and_ingest_log_populated(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        sources = _test_sources()

        def factory(_source: Any) -> FakeIngester:
            return FakeIngester(IngestResult(status=IngestStatus.OK, count=3))

        poller = IngestPoller(
            conn=ingest_db,
            sources=sources,
            ingester_factory=factory,
        )
        summary = await poller.run(run_id="run_test_success")

        assert summary["run_id"] == "run_test_success"
        assert summary["total"] == 3 * len(sources)
        # pipeline_runs row
        row = ingest_db.execute(
            """
            SELECT run_id, status, stages_done, counts, started_at
            FROM pipeline_runs WHERE run_id = 'run_test_success'
            """
        ).fetchone()
        assert row is not None
        run_id, status, stages_done, counts, started_at = row
        assert status == PipelineStatus.RUNNING.value
        assert json.loads(stages_done) == ["ingest"]
        assert json.loads(counts) == {"articles": 3 * len(sources)}
        assert started_at is not None
        # ingest_log rows: one per source
        log_rows = ingest_db.execute(
            "SELECT source_id, status, articles_fetched FROM ingest_log WHERE run_id = ?",
            ["run_test_success"],
        ).fetchall()
        assert len(log_rows) == len(sources)
        for _src_id, st, count in log_rows:
            assert st == IngestStatus.OK.value
            assert count == 3

    async def test_default_run_id_format(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        from musahit.common.time import tr_local_date

        def factory(_source: Any) -> FakeIngester:
            return FakeIngester()

        poller = IngestPoller(
            conn=ingest_db,
            sources=(SOURCES[0],),
            ingester_factory=factory,
        )
        summary = await poller.run()
        expected = "run_" + tr_local_date().isoformat().replace("-", "")
        assert summary["run_id"] == expected


# ── TestPerSourceFailureIsolation ──────────────────────────────────────────


class TestPerSourceFailureIsolation:
    async def test_exception_in_one_source_does_not_abort_run(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        sources = _test_sources()
        bad = sources[0]
        good = sources[1]

        def factory(source: Any) -> FakeIngester:
            if source.id == bad.id:
                return FakeIngester(raises=RuntimeError)
            return FakeIngester(IngestResult(status=IngestStatus.OK, count=2))

        poller = IngestPoller(
            conn=ingest_db,
            sources=(bad, good),
            ingester_factory=factory,
        )
        summary = await poller.run(run_id="run_failure")

        assert summary["total"] == 2  # only the good source contributed.

        rows = {
            r[0]: r
            for r in ingest_db.execute(
                """
                SELECT source_id, status, error_detail
                FROM ingest_log WHERE run_id = 'run_failure'
                """
            ).fetchall()
        }
        assert rows[bad.id][1] == IngestStatus.PARSE_ERROR.value
        assert "RuntimeError" in rows[bad.id][2]
        assert rows[good.id][1] == IngestStatus.OK.value
        # The run finished: stages_done populated.
        stages = json.loads(
            ingest_db.execute(
                "SELECT stages_done FROM pipeline_runs WHERE run_id = 'run_failure'"
            ).fetchone()[0]
        )
        assert stages == ["ingest"]

    async def test_source_timeout_is_caught_and_translated(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        sources = _test_sources()
        slow = sources[0]
        fast = sources[1]

        def factory(source: Any) -> FakeIngester:
            if source.id == slow.id:
                # Sleep longer than the poller's per-source timeout.
                return FakeIngester(sleep_seconds=0.5)
            return FakeIngester(IngestResult(status=IngestStatus.OK, count=1))

        # Use SimpleNamespace surrogates so we can force rate_limit_seconds=0
        # — the timeout floor is max(default, rate_limit*12); setting both
        # small keeps the test fast (real Source instances have rate_limit*12
        # floors of 60s+ which would make this test 60s long).
        surrogate_slow = SimpleNamespace(id=slow.id, kind=slow.kind, rate_limit_seconds=0)
        surrogate_fast = SimpleNamespace(id=fast.id, kind=fast.kind, rate_limit_seconds=0)
        poller_fast = IngestPoller(
            conn=ingest_db,
            sources=(surrogate_slow, surrogate_fast),  # type: ignore[arg-type]
            ingester_factory=factory,
            default_timeout_seconds=0.05,
        )
        summary = await poller_fast.run(run_id="run_timeout")

        rows = {
            r[0]: r[1]
            for r in ingest_db.execute(
                "SELECT source_id, status FROM ingest_log WHERE run_id = 'run_timeout'"
            ).fetchall()
        }
        assert rows[slow.id] == IngestStatus.TIMEOUT.value
        assert rows[fast.id] == IngestStatus.OK.value
        assert summary["total"] == 1  # only the fast source counted.


# ── TestRerunSameRunId ─────────────────────────────────────────────────────


class TestRerunSameRunId:
    async def test_rerun_updates_existing_row(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        sources = _test_sources()[:2]

        def factory(_s: Any) -> FakeIngester:
            return FakeIngester(IngestResult(status=IngestStatus.OK, count=1))

        poller = IngestPoller(
            conn=ingest_db, sources=sources, ingester_factory=factory
        )
        await poller.run(run_id="run_rerun")
        await poller.run(run_id="run_rerun")  # second pass: same run_id

        pipeline_rows = ingest_db.execute(
            "SELECT COUNT(*) FROM pipeline_runs WHERE run_id = 'run_rerun'"
        ).fetchone()
        assert pipeline_rows[0] == 1

        log_rows = ingest_db.execute(
            "SELECT COUNT(*) FROM ingest_log WHERE run_id = 'run_rerun'"
        ).fetchone()
        assert log_rows[0] == len(sources)  # one per source, not duplicated.


# ── TestConcurrencyCap ─────────────────────────────────────────────────────


class TestConcurrencyCap:
    async def test_no_more_than_max_concurrent_active_at_once(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        # Force 11 sources so that with cap=3, we observe overlap but not all at once.
        sources = _test_sources()
        assert len(sources) >= 11
        tracker = ConcurrencyTracker()

        def factory(_source: Any) -> FakeIngester:
            return FakeIngester(sleep_seconds=0.05, tracker=tracker)

        poller = IngestPoller(
            conn=ingest_db,
            sources=sources,
            ingester_factory=factory,
            max_concurrent=3,
        )
        await poller.run(run_id="run_concurrency")

        assert tracker.max_concurrent <= 3
        assert tracker.max_concurrent >= 2  # overlap actually happened

    def test_default_cap_is_eight(self) -> None:
        assert DEFAULT_MAX_CONCURRENT == 8


# ── TestGetIngesterDispatch ────────────────────────────────────────────────


class TestGetIngesterDispatch:
    @pytest.mark.parametrize(
        ("kind", "expected_cls"),
        [
            (SourceKind.RSS, RssIngester),
            (SourceKind.HTML, HtmlIngester),
            (SourceKind.PDF, ResmiGazeteIngester),
            (SourceKind.API, RedditIngester),
        ],
    )
    def test_dispatches_correctly(
        self,
        ingest_db: duckdb.DuckDBPyConnection,
        kind: SourceKind,
        expected_cls: type,
    ) -> None:
        # Find the first source of that kind in the canonical registry.
        source = next(s for s in SOURCES if s.kind is kind)
        ingester = get_ingester(source, conn=ingest_db)
        assert isinstance(ingester, expected_cls)

    def test_unknown_kind_returns_none(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        # DEFERRED is documented in SourceKind but never present in SOURCES;
        # the dispatcher must still return None safely.
        fake_source = SimpleNamespace(
            id="fake_deferred",
            kind=SourceKind.DEFERRED,
            url="https://example.com",
        )
        # No raise; just None.
        assert get_ingester(fake_source, conn=ingest_db) is None  # type: ignore[arg-type]


# ── TestFactoryOverride ────────────────────────────────────────────────────


class TestFactoryOverride:
    async def test_constructor_factory_takes_precedence(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        sources = _test_sources()[:3]
        # Per-source dict of pre-built FakeIngester instances so we can inspect
        # which sources were actually called.
        ingesters: dict[str, FakeIngester] = {
            s.id: FakeIngester(IngestResult(status=IngestStatus.OK, count=7))
            for s in sources
        }

        def factory(source: Any) -> FakeIngester:
            return ingesters[source.id]

        poller = IngestPoller(
            conn=ingest_db,
            sources=sources,
            ingester_factory=factory,
        )
        await poller.run(run_id="run_factory")

        for source in sources:
            assert ingesters[source.id].calls == [source.id]

    async def test_factory_returning_none_logs_skipped(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        sources = _test_sources()[:2]

        def factory(_source: Any) -> Any:
            return None

        poller = IngestPoller(
            conn=ingest_db, sources=sources, ingester_factory=factory
        )
        summary = await poller.run(run_id="run_none_factory")

        assert summary["total"] == 0
        rows = ingest_db.execute(
            "SELECT status FROM ingest_log WHERE run_id = 'run_none_factory'"
        ).fetchall()
        assert all(r[0] == IngestStatus.SKIPPED.value for r in rows)
