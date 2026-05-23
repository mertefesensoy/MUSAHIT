"""Tests for musahit.pipeline (the argparse CLI).

We exercise :func:`main` directly with synthetic ``argv`` rather than
shelling out to ``python -m musahit.pipeline``. Production
:class:`Orchestrator` is monkey-patched to a stub so the CLI's job
(argument parsing, date resolution, exit-code mapping) is tested in
isolation from the orchestrator's own coverage in
``test_orchestrator.py``.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pytest

import musahit.pipeline as pipeline_module
from musahit.common.config import Settings
from musahit.common.migrations import init_db
from musahit.common.types import PipelineStatus
from musahit.orchestrator import PipelineResult, StageFailure

# ── Stubs ──────────────────────────────────────────────────────────────────


class _StubOrchestrator:
    """Records constructor + run() calls; returns configurable result."""

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        settings: Settings,
        **kwargs: Any,
    ) -> None:
        _StubOrchestrator.last_conn = conn
        _StubOrchestrator.last_settings = settings
        _StubOrchestrator.last_kwargs = kwargs

    @classmethod
    def reset(cls) -> None:
        cls.last_conn = None
        cls.last_settings = None
        cls.last_kwargs = None
        cls.run_kwargs = None
        cls.next_result = PipelineResult(
            run_id="run_test", status=PipelineStatus.COMPLETED.value
        )
        cls.next_exception: BaseException | None = None

    async def run(self, **kwargs: Any) -> PipelineResult:
        _StubOrchestrator.run_kwargs = kwargs
        if _StubOrchestrator.next_exception is not None:
            raise _StubOrchestrator.next_exception
        return _StubOrchestrator.next_result


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Generator[Path, None, None]:
    db_path = tmp_path / "musahit.duckdb"
    init_db(db_path, load_vss=False)
    yield db_path


@pytest.fixture()
def patched_pipeline(
    tmp_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> type[_StubOrchestrator]:
    _StubOrchestrator.reset()
    # Pin Settings so CLI doesn't read the real config.toml / .env.
    settings = Settings(
        db_path=tmp_db,
        briefings_dir=tmp_path / "briefings",
        logs_dir=tmp_path / "logs",
        data_dir=tmp_path / "data",
        piper_voice_path=tmp_path / "voices" / "voice.onnx",
    )
    monkeypatch.setattr(pipeline_module, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline_module, "Orchestrator", _StubOrchestrator)
    return _StubOrchestrator


# ── Subcommand parsing ─────────────────────────────────────────────────────


class TestSubcommandParsing:
    def test_run_subcommand_invokes_orchestrator(
        self,
        patched_pipeline: type[_StubOrchestrator],
    ) -> None:
        exit_code = pipeline_module.main(["run", "--date", "2026-05-23"])
        assert exit_code == pipeline_module.EXIT_OK
        assert patched_pipeline.run_kwargs is not None
        assert patched_pipeline.run_kwargs["run_id"] == "run_20260523"

    def test_status_subcommand_reads_pipeline_runs(
        self,
        patched_pipeline: type[_StubOrchestrator],
        tmp_db: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Seed a row so status has something to print.
        from musahit.common.time import utcnow

        with duckdb.connect(str(tmp_db)) as conn:
            conn.execute(
                "INSERT INTO pipeline_runs (run_id, started_at, status, "
                "stages_done, counts, failed_stages) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    "run_20260523",
                    utcnow(),
                    PipelineStatus.COMPLETED.value,
                    '["ingest","normalize"]',
                    "{}",
                    "[]",
                ],
            )
        exit_code = pipeline_module.main(["status", "--date", "2026-05-23"])
        assert exit_code == pipeline_module.EXIT_OK
        out = capsys.readouterr().out
        assert "run_20260523" in out
        assert PipelineStatus.COMPLETED.value in out

    def test_status_missing_row_returns_failed_exit(
        self,
        patched_pipeline: type[_StubOrchestrator],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = pipeline_module.main(["status", "--date", "2026-05-23"])
        assert exit_code == pipeline_module.EXIT_FAILED
        out = capsys.readouterr().out
        assert "no pipeline_runs row" in out

    def test_resume_subcommand_runs_without_force(
        self,
        patched_pipeline: type[_StubOrchestrator],
    ) -> None:
        exit_code = pipeline_module.main(["resume", "--date", "2026-05-23"])
        assert exit_code == pipeline_module.EXIT_OK
        assert patched_pipeline.run_kwargs is not None
        # Resume defaults: no --force, no --stage filter, no --dry-run.
        assert patched_pipeline.run_kwargs.get("force") is False
        assert patched_pipeline.run_kwargs.get("only_stage") is None
        assert patched_pipeline.run_kwargs.get("dry_run") is False


# ── Date resolution ────────────────────────────────────────────────────────


class TestDateResolution:
    def test_today_resolves_via_tr_local_date(
        self,
        patched_pipeline: type[_StubOrchestrator],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fixed_date = date(2026, 7, 15)
        monkeypatch.setattr(
            pipeline_module, "tr_local_date", lambda: fixed_date
        )
        exit_code = pipeline_module.main(["run", "--date", "today"])
        assert exit_code == pipeline_module.EXIT_OK
        assert patched_pipeline.run_kwargs["run_id"] == "run_20260715"

    def test_explicit_iso_date_resolves(
        self,
        patched_pipeline: type[_StubOrchestrator],
    ) -> None:
        pipeline_module.main(["run", "--date", "2026-12-01"])
        assert patched_pipeline.run_kwargs["run_id"] == "run_20261201"

    def test_invalid_date_raises_systemexit(
        self,
        patched_pipeline: type[_StubOrchestrator],
    ) -> None:
        with pytest.raises(SystemExit):
            pipeline_module.main(["run", "--date", "not-a-date"])


# ── Flag plumbing ─────────────────────────────────────────────────────────


class TestFlagPlumbing:
    def test_stage_flag_propagates(
        self,
        patched_pipeline: type[_StubOrchestrator],
    ) -> None:
        pipeline_module.main(
            ["run", "--date", "2026-05-23", "--stage", "cluster"]
        )
        assert patched_pipeline.run_kwargs["only_stage"] == "cluster"

    def test_force_flag_propagates(
        self,
        patched_pipeline: type[_StubOrchestrator],
    ) -> None:
        pipeline_module.main(["run", "--date", "2026-05-23", "--force"])
        assert patched_pipeline.run_kwargs["force"] is True

    def test_dry_run_flag_propagates(
        self,
        patched_pipeline: type[_StubOrchestrator],
    ) -> None:
        pipeline_module.main(["run", "--date", "2026-05-23", "--dry-run"])
        assert patched_pipeline.run_kwargs["dry_run"] is True

    def test_unknown_stage_choice_rejected(
        self,
        patched_pipeline: type[_StubOrchestrator],
    ) -> None:
        with pytest.raises(SystemExit):
            pipeline_module.main(
                ["run", "--date", "2026-05-23", "--stage", "nonsense"]
            )


# ── Exit code mapping ─────────────────────────────────────────────────────


class TestExitCodes:
    def test_completed_returns_zero(
        self,
        patched_pipeline: type[_StubOrchestrator],
    ) -> None:
        patched_pipeline.next_result = PipelineResult(
            run_id="run_20260523",
            status=PipelineStatus.COMPLETED.value,
            stages_completed=["ingest", "normalize"],
        )
        exit_code = pipeline_module.main(["run", "--date", "2026-05-23"])
        assert exit_code == pipeline_module.EXIT_OK

    def test_failed_returns_one(
        self,
        patched_pipeline: type[_StubOrchestrator],
    ) -> None:
        patched_pipeline.next_result = PipelineResult(
            run_id="run_20260523",
            status=PipelineStatus.FAILED.value,
            catastrophic_reason="disk full",
        )
        exit_code = pipeline_module.main(["run", "--date", "2026-05-23"])
        assert exit_code == pipeline_module.EXIT_FAILED

    def test_sigint_returns_two(
        self,
        patched_pipeline: type[_StubOrchestrator],
    ) -> None:
        patched_pipeline.next_exception = KeyboardInterrupt()
        exit_code = pipeline_module.main(["run", "--date", "2026-05-23"])
        assert exit_code == pipeline_module.EXIT_SIGINT


# ── Output summary ────────────────────────────────────────────────────────


class TestOutputSummary:
    def test_summary_prints_run_id_and_status(
        self,
        patched_pipeline: type[_StubOrchestrator],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        patched_pipeline.next_result = PipelineResult(
            run_id="run_20260523",
            status=PipelineStatus.COMPLETED.value,
            stages_completed=["ingest", "normalize"],
            total_seconds=42.5,
        )
        pipeline_module.main(["run", "--date", "2026-05-23"])
        out = capsys.readouterr().out
        assert "run_20260523" in out
        assert "COMPLETED" in out
        assert "42.5" in out
        assert "ingest" in out

    def test_summary_lists_failed_stages(
        self,
        patched_pipeline: type[_StubOrchestrator],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        patched_pipeline.next_result = PipelineResult(
            run_id="run_20260523",
            status=PipelineStatus.COMPLETED.value,
            stages_completed=["ingest"],
            stages_failed=[
                StageFailure(name="score", reason="RuntimeError: boom"),
            ],
        )
        pipeline_module.main(["run", "--date", "2026-05-23"])
        out = capsys.readouterr().out
        assert "score" in out
        assert "RuntimeError: boom" in out


# ── configure_logging called ──────────────────────────────────────────────


class TestLoggingConfigured:
    def test_configure_logging_called_before_orchestrator(
        self,
        patched_pipeline: type[_StubOrchestrator],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Track call order: configure_logging must run before
        # the orchestrator constructor.
        events: list[str] = []
        original_configure = pipeline_module.configure_logging

        def tracked_configure() -> None:
            events.append("configure_logging")
            original_configure()

        original_init = _StubOrchestrator.__init__

        def tracked_init(self: Any, *args: Any, **kwargs: Any) -> None:
            events.append("orchestrator_init")
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(pipeline_module, "configure_logging", tracked_configure)
        monkeypatch.setattr(_StubOrchestrator, "__init__", tracked_init)
        pipeline_module.main(["run", "--date", "2026-05-23"])
        assert events.index("configure_logging") < events.index("orchestrator_init")
