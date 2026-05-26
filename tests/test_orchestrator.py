"""Tests for musahit.orchestrator.Orchestrator.

Every test injects a fake :class:`Stage` via ``stage_factory`` and a
fake Ollama manager. The real Ollama / Piper / per-stage modules are
never instantiated; the production stages are exercised by their own
test suites.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable, Generator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from musahit.common.config import Settings
from musahit.common.migrations import init_db
from musahit.common.types import PipelineStatus
from musahit.orchestrator import (
    DefaultStageFactory,
    DiskPressureError,
    Orchestrator,
    PipelineResult,
    StageFactory,
    _NoOpStage,  # noqa: PLC2701  (test relies on the dry-run stub class)
)
from musahit.stages import (
    STAGE_ARC_LINK,
    STAGE_CLUSTER,
    STAGE_INGEST,
    STAGE_NORMALIZE,
    STAGE_ORDER,
    STAGE_SCORE,
    STAGE_TTS,
    STAGE_WRITE,
    StageTimingBudget,
)

RUN_ID = "run_test_pipeline"
NOW = datetime(2026, 5, 23, 1, 0, 0)


# ── Fakes ──────────────────────────────────────────────────────────────────


class _SuccessStage:
    """Records that run() was called, returns immediately."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

    async def run(self, run_id: str) -> dict[str, Any]:
        self.calls.append(run_id)
        return {"stage": self.name, "ok": True}


class _RaisingStage:
    """Raises a chosen exception when run() is called."""

    def __init__(self, name: str, exc: BaseException) -> None:
        self.name = name
        self._exc = exc
        self.calls: list[str] = []

    async def run(self, run_id: str) -> Any:
        self.calls.append(run_id)
        raise self._exc


class _SlowStage:
    """Awaits longer than its timeout budget."""

    def __init__(self, name: str, sleep_seconds: float = 5.0) -> None:
        self.name = name
        self._sleep = sleep_seconds
        self.calls: list[str] = []

    async def run(self, run_id: str) -> Any:
        self.calls.append(run_id)
        await asyncio.sleep(self._sleep)


class _RecordingOllama:
    """Captures load / unload calls for assertions."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []  # ("load"|"unload", model)

    async def load(self, model: str) -> None:
        self.events.append(("load", model))

    async def unload(self, model: str) -> None:
        self.events.append(("unload", model))


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path: Path) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    db_path = tmp_path / "x.duckdb"
    init_db(db_path, load_vss=False)
    conn = duckdb.connect(str(db_path))
    yield conn
    conn.close()


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "x.duckdb",
        briefings_dir=tmp_path / "briefings",
        logs_dir=tmp_path / "logs",
        data_dir=tmp_path / "data",
        piper_voice_path=tmp_path / "voices" / "voice.onnx",
        min_free_disk_gb=1,
    )


def _make_factory(stages: dict[str, Any]) -> StageFactory:
    """Build a StageFactory mapping name → preconstructed fake."""

    def _factory(name: str) -> Any:
        if name not in stages:
            raise ValueError(f"no fake stage for {name!r}")
        return stages[name]

    return _factory


def _all_success_stages() -> dict[str, _SuccessStage]:
    return {name: _SuccessStage(name) for name in STAGE_ORDER}


# Generous timing budget for non-timeout tests; 10 seconds soft, 20s hard.
_FAST_BUDGETS = {name: StageTimingBudget(soft_minutes=10.0 / 60.0) for name in STAGE_ORDER}


# ── Happy path ─────────────────────────────────────────────────────────────


class TestHappyPath:
    async def test_all_stages_run_and_status_completed(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        stages = _all_success_stages()
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(stages),
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )
        result = await orchestrator.run(run_id=RUN_ID)

        assert result.status == PipelineStatus.COMPLETED.value
        assert result.stages_completed == list(STAGE_ORDER)
        assert result.stages_failed == []
        # Each stage's run was called exactly once with the run_id.
        for stage in stages.values():
            assert stage.calls == [RUN_ID]
        # pipeline_runs row updated.
        row = db.execute(
            "SELECT status, stages_done, completed_at FROM pipeline_runs "
            "WHERE run_id = ?",
            [RUN_ID],
        ).fetchone()
        assert row[0] == PipelineStatus.COMPLETED.value
        assert json.loads(row[1]) == list(STAGE_ORDER)
        assert row[2] is not None

    async def test_total_seconds_populated(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(_all_success_stages()),
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )
        result = await orchestrator.run(run_id=RUN_ID)
        assert result.total_seconds >= 0.0


# ── Soft failure ───────────────────────────────────────────────────────────


class TestSoftFailure:
    async def test_one_stage_raises_others_continue(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        stages: dict[str, Any] = _all_success_stages()
        # The score stage raises; the rest succeed.
        stages[STAGE_SCORE] = _RaisingStage(
            STAGE_SCORE, RuntimeError("simulated score crash")
        )
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(stages),
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )
        result = await orchestrator.run(run_id=RUN_ID)

        # Pipeline still reaches COMPLETED · soft failures don't abort.
        assert result.status == PipelineStatus.COMPLETED.value
        # Score is missing from completed; everything else is present.
        assert STAGE_SCORE not in result.stages_completed
        assert STAGE_INGEST in result.stages_completed
        assert STAGE_WRITE in result.stages_completed
        assert STAGE_TTS in result.stages_completed
        # Failure recorded.
        assert len(result.stages_failed) == 1
        assert result.stages_failed[0].name == STAGE_SCORE
        assert "simulated score crash" in result.stages_failed[0].reason
        # Persisted to DB.
        row = db.execute(
            "SELECT failed_stages FROM pipeline_runs WHERE run_id = ?",
            [RUN_ID],
        ).fetchone()
        failed = json.loads(row[0])
        assert failed[0]["name"] == STAGE_SCORE
        assert "RuntimeError" in failed[0]["reason"]


# ── Timeout ────────────────────────────────────────────────────────────────


class TestTimeout:
    async def test_slow_stage_times_out_and_records_failure(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        # Score budget tightened to 0.0005 min = 30 ms → timeout at 60 ms.
        # The fake stage sleeps for 2 s, well past the hard timeout.
        tight_budgets = dict(_FAST_BUDGETS)
        tight_budgets[STAGE_SCORE] = StageTimingBudget(soft_minutes=0.0005)
        stages: dict[str, Any] = _all_success_stages()
        stages[STAGE_SCORE] = _SlowStage(STAGE_SCORE, sleep_seconds=2.0)
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(stages),
            ollama=_RecordingOllama(),
            timing_budgets=tight_budgets,
        )
        result = await orchestrator.run(run_id=RUN_ID)

        # Pipeline still reaches COMPLETED; timeout was per-stage.
        assert result.status == PipelineStatus.COMPLETED.value
        # Score is in failed.
        names = [f.name for f in result.stages_failed]
        assert STAGE_SCORE in names
        score_failure = next(f for f in result.stages_failed if f.name == STAGE_SCORE)
        assert "TimeoutError" in score_failure.reason
        assert "exceeded" in score_failure.reason
        # Other stages still ran.
        assert STAGE_INGEST in result.stages_completed
        assert STAGE_WRITE in result.stages_completed


# ── Resume ─────────────────────────────────────────────────────────────────


class TestResume:
    async def test_resume_skips_completed_stages(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        # Seed pipeline_runs with stages_done already covering the
        # first three stages. A run with default args should skip them.
        db.execute(
            "INSERT INTO pipeline_runs (run_id, started_at, status, "
            "stages_done, counts, failed_stages) VALUES (?, ?, ?, ?, ?, ?)",
            [
                RUN_ID,
                NOW,
                PipelineStatus.RUNNING.value,
                json.dumps([STAGE_INGEST, STAGE_NORMALIZE, STAGE_CLUSTER]),
                json.dumps({}),
                json.dumps([]),
            ],
        )
        stages = _all_success_stages()
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(stages),
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )
        result = await orchestrator.run(run_id=RUN_ID)

        # Skipped stages did NOT run.
        assert stages[STAGE_INGEST].calls == []
        assert stages[STAGE_NORMALIZE].calls == []
        assert stages[STAGE_CLUSTER].calls == []
        # Remaining stages DID run.
        assert stages[STAGE_SCORE].calls == [RUN_ID]
        assert stages[STAGE_ARC_LINK].calls == [RUN_ID]
        assert stages[STAGE_WRITE].calls == [RUN_ID]
        assert stages[STAGE_TTS].calls == [RUN_ID]
        # Result reports only the newly-completed stages.
        assert STAGE_INGEST not in result.stages_completed
        assert STAGE_SCORE in result.stages_completed

    async def test_force_reruns_all_stages(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        db.execute(
            "INSERT INTO pipeline_runs (run_id, started_at, status, "
            "stages_done, counts, failed_stages) VALUES (?, ?, ?, ?, ?, ?)",
            [
                RUN_ID,
                NOW,
                PipelineStatus.RUNNING.value,
                json.dumps([STAGE_INGEST, STAGE_NORMALIZE]),
                json.dumps({}),
                json.dumps([]),
            ],
        )
        stages = _all_success_stages()
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(stages),
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )
        result = await orchestrator.run(run_id=RUN_ID, force=True)

        # With --force, every stage runs even though some were in stages_done.
        for stage in stages.values():
            assert stage.calls == [RUN_ID]
        assert result.stages_completed == list(STAGE_ORDER)


# ── Only-stage ─────────────────────────────────────────────────────────────


class TestOnlyStage:
    async def test_only_named_stage_runs(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        stages = _all_success_stages()
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(stages),
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )
        result = await orchestrator.run(run_id=RUN_ID, only_stage=STAGE_WRITE)

        # Only the writer ran.
        assert stages[STAGE_WRITE].calls == [RUN_ID]
        for name, stage in stages.items():
            if name != STAGE_WRITE:
                assert stage.calls == [], f"{name} should not have run"
        assert result.stages_completed == [STAGE_WRITE]


# ── Dry run ────────────────────────────────────────────────────────────────


class TestDryRun:
    async def test_dry_run_uses_noop_stages(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        # Track what was constructed by hijacking the default factory
        # path indirectly: dry_run flips to DryRunStageFactory which
        # builds _NoOpStage instances. We capture them by monkey-
        # patching DryRunStageFactory? Simpler: just verify status +
        # absence of DB writes.
        stages = _all_success_stages()
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(stages),
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )
        result = await orchestrator.run(run_id=RUN_ID, dry_run=True)

        # Our injected fakes are NOT called because dry_run switches to
        # DryRunStageFactory internally.
        for stage in stages.values():
            assert stage.calls == []
        # Status still completes (no work failed).
        assert result.status == PipelineStatus.COMPLETED.value
        # No pipeline_runs row was created (dry-run skips DB writes).
        row = db.execute(
            "SELECT COUNT(*) FROM pipeline_runs WHERE run_id = ?", [RUN_ID]
        ).fetchone()
        assert row[0] == 0

    def test_noop_stage_records_call(self) -> None:
        stage = _NoOpStage("ingest")
        asyncio.get_event_loop()
        result = asyncio.run(stage.run("run_test"))
        assert stage.called is True
        assert result["dry_run"] is True
        assert result["stage"] == "ingest"


# ── Model lifecycle ────────────────────────────────────────────────────────


class TestModelLifecycle:
    async def test_models_load_before_stage_and_unload_after(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        ollama = _RecordingOllama()
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(_all_success_stages()),
            ollama=ollama,
            timing_budgets=_FAST_BUDGETS,
        )
        await orchestrator.run(run_id=RUN_ID)

        # Per goal-spec mapping: cluster→embed, score→worker, write→writer.
        # Each model is loaded once and unloaded once.
        load_calls = [(kind, m) for (kind, m) in ollama.events if kind == "load"]
        unload_calls = [(kind, m) for (kind, m) in ollama.events if kind == "unload"]
        load_models = [m for (_, m) in load_calls]
        unload_models = [m for (_, m) in unload_calls]
        assert settings.embed_model in load_models
        assert settings.worker_model in load_models
        assert settings.writer_model in load_models
        assert settings.embed_model in unload_models
        assert settings.worker_model in unload_models
        assert settings.writer_model in unload_models

    async def test_only_required_stages_load_models(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        ollama = _RecordingOllama()
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(_all_success_stages()),
            ollama=ollama,
            timing_budgets=_FAST_BUDGETS,
        )
        # Only ingest, normalize, arc-link, tts → no models.
        # We run only the ingest stage to verify no model load happens.
        ollama.events.clear()
        await orchestrator.run(run_id=RUN_ID + "_b", only_stage=STAGE_INGEST)
        assert ollama.events == []

    async def test_model_order_is_load_unload_per_stage(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        ollama = _RecordingOllama()
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(_all_success_stages()),
            ollama=ollama,
            timing_budgets=_FAST_BUDGETS,
        )
        await orchestrator.run(run_id=RUN_ID)

        # Walk the event log; every model load must precede its unload.
        seen_load: set[str] = set()
        for kind, model in ollama.events:
            if kind == "load":
                seen_load.add(model)
            elif kind == "unload":
                assert model in seen_load, (
                    f"unload({model}) without preceding load"
                )


# ── Disk pressure ──────────────────────────────────────────────────────────


class TestDiskPressure:
    async def test_disk_pressure_aborts_before_stage_one(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Simulate "only 100 MB free" while the floor is 1 GB.

        class _Usage:
            free = 100 * 1024 * 1024  # 100 MB

        def fake_disk_usage(_path: str) -> Any:
            return _Usage()

        monkeypatch.setattr(shutil, "disk_usage", fake_disk_usage)
        stages = _all_success_stages()
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(stages),
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )
        with pytest.raises(DiskPressureError, match="GB free"):
            await orchestrator.run(run_id=RUN_ID)
        # No stage ran.
        for stage in stages.values():
            assert stage.calls == []


# ── KeyboardInterrupt ─────────────────────────────────────────────────────


class TestKeyboardInterrupt:
    async def test_sigint_preserves_stages_done_and_reraises(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        stages: dict[str, Any] = _all_success_stages()
        # Score raises KeyboardInterrupt · catastrophic, re-raised.
        stages[STAGE_SCORE] = _RaisingStage(STAGE_SCORE, KeyboardInterrupt())
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(stages),
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )
        with pytest.raises(KeyboardInterrupt):
            await orchestrator.run(run_id=RUN_ID)

        # Stages_done preserves whatever ran before the interrupt:
        # ingest, normalize, cluster.
        row = db.execute(
            "SELECT stages_done, status FROM pipeline_runs WHERE run_id = ?",
            [RUN_ID],
        ).fetchone()
        stages_done = json.loads(row[0])
        assert STAGE_INGEST in stages_done
        assert STAGE_NORMALIZE in stages_done
        assert STAGE_CLUSTER in stages_done
        # Score is NOT in stages_done.
        assert STAGE_SCORE not in stages_done
        # Status flipped to FAILED.
        assert row[1] == PipelineStatus.FAILED.value


# ── Stderr traceback on soft failure (post-step-14 diagnostic pattern) ─────


class TestFailureTraceback:
    async def test_soft_failure_prints_traceback_to_stderr(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        stages: dict[str, Any] = _all_success_stages()
        stages[STAGE_TTS] = _RaisingStage(STAGE_TTS, RuntimeError("boom"))
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(stages),
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )
        result = await orchestrator.run(run_id=RUN_ID)
        captured = capsys.readouterr()
        assert "Traceback" in captured.err
        assert "RuntimeError" in captured.err
        assert "boom" in captured.err
        assert result.status == PipelineStatus.COMPLETED.value


# ── PipelineResult helpers ─────────────────────────────────────────────────


def test_pipeline_result_default_status_completed() -> None:
    r = PipelineResult(run_id="x", status="COMPLETED")
    assert r.stages_completed == []
    assert r.stages_failed == []
    assert r.total_seconds == 0.0
    assert r.catastrophic_reason is None


def _silence_unused_callable_type() -> Callable[[], None]:  # pragma: no cover
    return lambda: None


# ── Regression: target_date propagation (2026-05-24) ───────────────────────


class TestTargetDatePropagation:
    """The 2026-05-23 smoke run shipped the briefing to the wrong
    on-disk path because the writer derived the date from
    ``started_at`` (UTC) instead of the CLI's TR-local target. The
    fix adds ``target_date`` to ``Orchestrator.run`` and threads it
    through ``DefaultStageFactory`` into ``Briefer`` at construction
    time. These tests pin the two halves of the new contract."""

    async def test_run_stores_target_date_for_factory_to_read(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        """The orchestrator binds ``_current_target_date`` at the
        start of ``run()`` so the (default or test-supplied) factory
        can read the per-run value via a closure."""
        captured: list[date | None] = []

        def _spy_factory(name: str) -> Any:
            if name == STAGE_WRITE:
                captured.append(orchestrator._current_target_date)
            return _SuccessStage(name)

        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_spy_factory,
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )
        target = date(2026, 5, 24)
        result = await orchestrator.run(
            run_id=RUN_ID, target_date=target
        )
        assert result.status == PipelineStatus.COMPLETED.value
        assert captured == [target]

    def test_default_factory_passes_target_date_to_briefer(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        """``DefaultStageFactory`` reads ``get_target_date()`` at
        each call and constructs ``Briefer`` with the current value.
        The closure indirection means a single factory instance
        survives multiple runs with different dates."""
        from musahit.writer.briefer import Briefer  # local import keeps fixture light

        current: list[date | None] = [date(2026, 5, 24)]
        factory = DefaultStageFactory(
            db, settings, get_target_date=lambda: current[0]
        )
        stage = factory(STAGE_WRITE)
        assert isinstance(stage, Briefer)
        assert stage._target_date == date(2026, 5, 24)

        # Subsequent call with a different "current" value reflects
        # the new date · no caching.
        current[0] = date(2026, 5, 25)
        stage_2 = factory(STAGE_WRITE)
        assert isinstance(stage_2, Briefer)
        assert stage_2._target_date == date(2026, 5, 25)

    async def test_omitting_target_date_leaves_current_none(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        """When the caller omits ``target_date``, the orchestrator's
        ``_current_target_date`` stays ``None`` and the default
        factory's Briefer falls back to the legacy started_at path."""
        captured: list[date | None] = []

        def _spy_factory(name: str) -> Any:
            if name == STAGE_WRITE:
                captured.append(orchestrator._current_target_date)
            return _SuccessStage(name)

        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_spy_factory,
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )
        await orchestrator.run(run_id=RUN_ID)
        assert captured == [None]


# ── Regression: pipeline_runs lifecycle (2026-05-24) ───────────────────────


class TestPipelineRunsLifecycle:
    """Three failure modes the 2026-05-24 smoke run surfaced:

    1. The pipeline can leave ``status=RUNNING`` with all 7 stages
       complete · the process died (Ctrl-C, reboot) between the
       for-loop and ``_mark_run_completed``. Today's run should
       recover, not treat the stuck row as fresh work.
    2. An unhandled exception that escapes both the
       ``_CatastrophicError`` catch and the normal completion path
       must still mark a terminal status · the ``finally`` guard.
    3. A stage that internally wipes ``stages_done`` (the
       IngestPoller's destructive ``_upsert_run_start``) must not
       erase prior-stage progress when the orchestrator drives it ·
       the orchestrator's in-memory view + merge-on-write defends
       against the wipe without modifying the file-protected poller.
    """

    async def test_resume_against_completed_row_is_noop(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        """A row already in ``COMPLETED`` re-runs nothing and stays
        ``COMPLETED``. The orchestrator's ``_upsert_run_row`` flips
        status back to ``RUNNING`` first (that's by design · the
        operator may be retrying), but with every stage already in
        ``stages_done`` the auto-recovery path fires and marks
        ``COMPLETED`` again before any stage executes."""
        db.execute(
            "INSERT INTO pipeline_runs (run_id, started_at, completed_at, "
            "status, stages_done, counts, failed_stages) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                RUN_ID,
                NOW,
                datetime(2026, 5, 23, 1, 30, 0),
                PipelineStatus.COMPLETED.value,
                json.dumps(list(STAGE_ORDER)),
                json.dumps({"articles": 200}),
                json.dumps([]),
            ],
        )
        stages = _all_success_stages()
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(stages),
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )
        result = await orchestrator.run(run_id=RUN_ID)

        assert result.status == PipelineStatus.COMPLETED.value
        assert result.stages_completed == []
        for stage in stages.values():
            assert stage.calls == []
        row = db.execute(
            "SELECT status, stages_done FROM pipeline_runs WHERE run_id = ?",
            [RUN_ID],
        ).fetchone()
        assert row[0] == PipelineStatus.COMPLETED.value
        assert json.loads(row[1]) == list(STAGE_ORDER)

    async def test_recovery_branch_skips_completed_row_with_full_stages_done(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        """A COMPLETED row with full stages_done must NOT trigger the
        ``pipeline_auto_complete_stuck_running`` WARNING. The for-loop
        skip-completes every stage via the ``stage_skip_completed``
        branch and the normal completion path marks COMPLETED."""
        import structlog.testing

        db.execute(
            "INSERT INTO pipeline_runs (run_id, started_at, completed_at, "
            "status, stages_done, counts, failed_stages) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                RUN_ID,
                NOW,
                datetime(2026, 5, 23, 1, 30, 0),
                PipelineStatus.COMPLETED.value,
                json.dumps(list(STAGE_ORDER)),
                json.dumps({"articles": 200}),
                json.dumps([]),
            ],
        )
        stages = _all_success_stages()
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(stages),
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )
        with structlog.testing.capture_logs() as captured:
            result = await orchestrator.run(run_id=RUN_ID)

        assert result.status == PipelineStatus.COMPLETED.value
        assert result.stages_completed == []
        for stage in stages.values():
            assert stage.calls == []
        assert not any(
            log.get("event") == "pipeline_auto_complete_stuck_running"
            for log in captured
        ), "recovery WARNING must not fire for an already-COMPLETED row"
        row = db.execute(
            "SELECT status, stages_done FROM pipeline_runs WHERE run_id = ?",
            [RUN_ID],
        ).fetchone()
        assert row[0] == PipelineStatus.COMPLETED.value
        assert json.loads(row[1]) == list(STAGE_ORDER)

    async def test_resume_auto_recovers_stuck_at_running_with_full_stages(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        """The reported 2026-05-24 bug: row stuck at ``RUNNING`` with
        all 7 stages in ``stages_done``. Today's resume should
        immediately mark ``COMPLETED`` rather than treat the row as
        fresh work and re-run every stage."""
        db.execute(
            "INSERT INTO pipeline_runs (run_id, started_at, completed_at, "
            "status, stages_done, counts, failed_stages) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                RUN_ID,
                NOW,
                None,  # stuck · no completed_at
                PipelineStatus.RUNNING.value,
                json.dumps(list(STAGE_ORDER)),
                json.dumps({"articles": 200}),
                json.dumps([]),
            ],
        )
        stages = _all_success_stages()
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(stages),
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )
        result = await orchestrator.run(run_id=RUN_ID)

        assert result.status == PipelineStatus.COMPLETED.value
        for stage in stages.values():
            assert stage.calls == []
        row = db.execute(
            "SELECT status, completed_at, stages_done "
            "FROM pipeline_runs WHERE run_id = ?",
            [RUN_ID],
        ).fetchone()
        assert row[0] == PipelineStatus.COMPLETED.value
        assert row[1] is not None
        assert json.loads(row[2]) == list(STAGE_ORDER)

    async def test_resume_runs_only_missing_stages(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        """Row stuck at ``RUNNING`` with stages 1-3 complete · today's
        resume runs stages 4-7 only and ends ``COMPLETED``."""
        db.execute(
            "INSERT INTO pipeline_runs (run_id, started_at, completed_at, "
            "status, stages_done, counts, failed_stages) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                RUN_ID,
                NOW,
                None,
                PipelineStatus.RUNNING.value,
                json.dumps([STAGE_INGEST, STAGE_NORMALIZE, STAGE_CLUSTER]),
                json.dumps({"articles": 200}),
                json.dumps([]),
            ],
        )
        stages = _all_success_stages()
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(stages),
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )
        result = await orchestrator.run(run_id=RUN_ID)

        assert result.status == PipelineStatus.COMPLETED.value
        # Stages 1-3 skipped.
        assert stages[STAGE_INGEST].calls == []
        assert stages[STAGE_NORMALIZE].calls == []
        assert stages[STAGE_CLUSTER].calls == []
        # Stages 4-7 ran.
        assert stages[STAGE_SCORE].calls == [RUN_ID]
        assert stages[STAGE_ARC_LINK].calls == [RUN_ID]
        assert stages[STAGE_WRITE].calls == [RUN_ID]
        assert stages[STAGE_TTS].calls == [RUN_ID]
        # Final stages_done covers all 7.
        row = db.execute(
            "SELECT status, stages_done FROM pipeline_runs WHERE run_id = ?",
            [RUN_ID],
        ).fetchone()
        assert row[0] == PipelineStatus.COMPLETED.value
        assert set(json.loads(row[1])) == set(STAGE_ORDER)

    async def test_finally_marks_failed_when_stage_wipes_stages_done(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        """When a stage internally wipes ``stages_done`` (the documented
        IngestPoller behaviour), the orchestrator's merge-on-write
        restores prior progress instead of losing it. Simulated here
        with a fake ingest stage that resets the row inside its
        ``run()`` body."""

        # Seed: stages 1-3 already done · score, arc_link, write, tts
        # remain. Without the merge, calling ingest (which wipes
        # stages_done) would erase normalize and cluster from the row.
        db.execute(
            "INSERT INTO pipeline_runs (run_id, started_at, completed_at, "
            "status, stages_done, counts, failed_stages) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                RUN_ID,
                NOW,
                None,
                PipelineStatus.RUNNING.value,
                json.dumps([STAGE_NORMALIZE, STAGE_CLUSTER]),  # NO ingest
                json.dumps({}),
                json.dumps([]),
            ],
        )

        class _IngestWipeStage:
            """Mimics IngestPoller's destructive _upsert_run_start."""

            def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
                self.name = STAGE_INGEST
                self._conn = conn
                self.calls: list[str] = []

            async def run(self, run_id: str) -> dict[str, Any]:
                self.calls.append(run_id)
                # Wipe the way the real poller does.
                self._conn.execute(
                    "UPDATE pipeline_runs SET stages_done = ?, "
                    "started_at = ?, completed_at = NULL WHERE run_id = ?",
                    [json.dumps([]), datetime(2026, 5, 25, 0, 0, 0), run_id],
                )
                return {"stage": STAGE_INGEST}

        wipe_stage = _IngestWipeStage(db)
        stages: dict[str, Any] = _all_success_stages()
        stages[STAGE_INGEST] = wipe_stage
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(stages),
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )
        result = await orchestrator.run(run_id=RUN_ID)

        assert result.status == PipelineStatus.COMPLETED.value
        assert wipe_stage.calls == [RUN_ID]
        # The merge-on-write must rebuild the full union despite the
        # wipe. Without the merge, normalize + cluster would be gone.
        row = db.execute(
            "SELECT stages_done FROM pipeline_runs WHERE run_id = ?",
            [RUN_ID],
        ).fetchone()
        assert set(json.loads(row[0])) == set(STAGE_ORDER)

    async def test_finally_marks_failed_on_uncaught_exception(
        self,
        db: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        """A non-catastrophic exception that escapes the for-loop body
        (rare, but happens when a stage's post-success bookkeeping
        crashes) must still leave the row at a terminal status. The
        ``finally`` guard writes ``FAILED`` so the row is never
        stuck-at-RUNNING."""

        # Patch _append_stage_done on a single orchestrator instance to
        # raise an uncaught exception after the first stage succeeds.
        orchestrator = Orchestrator(
            db,
            settings,
            stage_factory=_make_factory(_all_success_stages()),
            ollama=_RecordingOllama(),
            timing_budgets=_FAST_BUDGETS,
        )

        original = orchestrator._append_stage_done
        call_count = {"n": 0}

        def _booby_trap(run_id: str, stage_name: str) -> None:
            call_count["n"] += 1
            if call_count["n"] == 2:  # explode on normalize's bookkeeping
                raise RuntimeError("simulated bookkeeping failure")
            original(run_id, stage_name)

        orchestrator._append_stage_done = _booby_trap  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="simulated bookkeeping"):
            await orchestrator.run(run_id=RUN_ID)

        row = db.execute(
            "SELECT status, completed_at FROM pipeline_runs WHERE run_id = ?",
            [RUN_ID],
        ).fetchone()
        # Row is no longer stuck at RUNNING.
        assert row[0] == PipelineStatus.FAILED.value
        assert row[1] is not None
