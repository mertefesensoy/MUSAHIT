"""Pipeline orchestrator: bind the seven stages into one nightly run.

The :class:`Orchestrator` is the seam between the per-stage modules
(ingest · normalize · cluster · score · arc-link · write · tts) and
the operator-facing CLI (:mod:`musahit.pipeline`). It owns three
responsibilities:

1. **Resumability** — read ``pipeline_runs.stages_done``, skip stages
   already completed unless ``--force``. The crash-mid-run path (Windows
   Update reboot, power loss) is handled by re-running ``pipeline
   run --date today``; the orchestrator resumes from the last completed
   stage per ADR-007 § Resumability.
2. **Failure isolation** — per ADR-012 § Stage 2-5 a single stage's
   exception is logged + recorded in ``failed_stages`` and the next
   stage runs anyway. The briefing always ships, even when one
   component fails. Only catastrophic conditions (``KeyboardInterrupt``,
   ``DiskPressureError``, DuckDB I/O errors) abort the run.
3. **Ollama model lifecycle** — the laptop has ~16 GB RAM and only one
   model fits comfortably with overhead. The orchestrator loads the
   model each stage needs immediately before that stage runs and
   unloads it after the last stage that needs it. Per ADR-001 § Single
   Ollama instance the worker model is unloaded before the writer
   model loads.

Dependency injection:

- ``stage_factory`` — a callable ``str -> Stage`` so tests inject
  fake stages. Defaults to :class:`DefaultStageFactory` which
  constructs the production stages with their real Ollama / Piper
  clients.
- ``ollama`` — :class:`OllamaModelManager` for load / unload calls.
  Defaults to a real httpx-backed manager; tests inject a fake.
- ``timing_budgets`` — overrideable :data:`STAGE_BUDGETS` so tests
  use fractional-minute budgets to exercise the timeout path quickly.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import httpx

from musahit.arcs.linker import ArcLinker
from musahit.cluster.clusterer import Clusterer
from musahit.cluster.embedder import OllamaEmbeddingClient
from musahit.common.config import Settings
from musahit.common.logging import get_logger
from musahit.common.time import utcnow
from musahit.common.types import PipelineStatus
from musahit.ingest.poller import IngestPoller
from musahit.normalize.normalizer import Normalizer
from musahit.score.classifier import Classifier
from musahit.score.llm_client import OllamaLlmClient
from musahit.stages import (
    STAGE_ARC_LINK,
    STAGE_BUDGETS,
    STAGE_CLUSTER,
    STAGE_INGEST,
    STAGE_NORMALIZE,
    STAGE_ORDER,
    STAGE_SCORE,
    STAGE_TTS,
    STAGE_WRITE,
    Stage,
    StageTimingBudget,
)
from musahit.tts.piper import PiperPythonClient
from musahit.tts.synthesizer import Synthesizer
from musahit.writer.briefer import Briefer

_log = get_logger("musahit.orchestrator")


# ── Custom exceptions ──────────────────────────────────────────────────────


class DiskPressureError(RuntimeError):
    """Raised before stage 1 when free disk falls below the configured floor.

    Per ADR-012 § Disk pressure the pipeline aborts immediately rather
    than start a run that can't possibly finish — the liveness probe
    surfaces the failure to the operator via a Windows toast.
    """


# ── PipelineResult ─────────────────────────────────────────────────────────


@dataclass
class StageFailure:
    """One stage that raised an exception during the run."""

    name: str
    reason: str  # ``"{ExceptionType}: {message}"``


@dataclass
class PipelineResult:
    """Summary returned from :meth:`Orchestrator.run`.

    ``status`` is one of the :class:`PipelineStatus` string values:
    ``"COMPLETED"`` when every stage succeeded or only soft-failed
    stages occurred, ``"FAILED"`` when a catastrophic exception broke
    the run (KeyboardInterrupt, disk full, DB corruption).

    ``stages_completed`` is the list of stages that finished without
    exception, in execution order. ``stages_failed`` is the list of
    soft failures (exceptions caught and recorded). A stage can be in
    ``stages_completed`` from a prior run and re-failed on this run —
    but that doesn't happen in the same run; one run records each
    stage exactly once.
    """

    run_id: str
    status: str
    stages_completed: list[str] = field(default_factory=list)
    stages_failed: list[StageFailure] = field(default_factory=list)
    total_seconds: float = 0.0
    catastrophic_reason: str | None = None  # populated when status=FAILED


# ── Stage factory ──────────────────────────────────────────────────────────


StageFactory = Callable[[str], Stage]


class DefaultStageFactory:
    """Build production stages with their real Ollama / Piper clients.

    The factory is called once per stage by the orchestrator at the
    moment the stage is about to run. Lazy construction means a Piper
    voice missing on disk fails the tts stage specifically rather than
    blocking the whole pipeline at startup.
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        settings: Settings,
    ) -> None:
        self._conn = conn
        self._settings = settings

    def __call__(self, name: str) -> Stage:
        if name == STAGE_INGEST:
            return IngestPoller(self._conn, settings=self._settings)
        if name == STAGE_NORMALIZE:
            return Normalizer(self._conn)
        if name == STAGE_CLUSTER:
            return Clusterer(
                self._conn,
                OllamaEmbeddingClient(base_url=self._settings.ollama_base_url),
            )
        if name == STAGE_SCORE:
            return Classifier(
                self._conn,
                OllamaLlmClient(base_url=self._settings.ollama_base_url),
            )
        if name == STAGE_ARC_LINK:
            return ArcLinker(self._conn)
        if name == STAGE_WRITE:
            return Briefer(
                self._conn,
                OllamaLlmClient(base_url=self._settings.ollama_base_url),
                briefings_root=self._settings.briefings_dir,
                writer_model=self._settings.writer_model,
            )
        if name == STAGE_TTS:
            return Synthesizer(
                self._conn,
                PiperPythonClient(self._settings.piper_voice_path),
                briefings_root=self._settings.briefings_dir,
            )
        raise ValueError(f"unknown stage: {name!r}")


class _NoOpStage:
    """Dry-run stub. Records the call but performs no DB writes / I/O."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.called: bool = False

    async def run(self, run_id: str) -> dict[str, Any]:
        self.called = True
        _log.info("dry_run_stage", stage=self.name, run_id=run_id)
        return {"dry_run": True, "stage": self.name}


class DryRunStageFactory:
    """Factory used when ``--dry-run`` is set. Returns :class:`_NoOpStage`."""

    def __call__(self, name: str) -> Stage:
        return _NoOpStage(name)


# ── Ollama model manager ───────────────────────────────────────────────────


class OllamaModelManager:
    """Load / unload Ollama models via the ``/api/generate`` endpoint.

    Per ADR-001 § Single Ollama instance only one model is held in
    memory at a time on the operator's laptop. Loading is a warm-cache
    call (``keep_alive: "5m"``); unloading is the same endpoint with
    ``keep_alive: 0`` which tells Ollama to evict the model immediately.

    Failures (Ollama not running, network blip) are logged at WARN and
    swallowed — the stage that needs the model will then fail with a
    more specific error and the orchestrator's per-stage failure
    isolation handles it. The pipeline does not abort if load/unload
    misbehaves.
    """

    LOAD_KEEP_ALIVE: str = "5m"
    UNLOAD_KEEP_ALIVE: int = 0
    DEFAULT_TIMEOUT_SECONDS: float = 60.0

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout_seconds = timeout_seconds

    async def load(self, model: str) -> None:
        await self._call(model, keep_alive=self.LOAD_KEEP_ALIVE)

    async def unload(self, model: str) -> None:
        await self._call(model, keep_alive=self.UNLOAD_KEEP_ALIVE)

    async def _call(self, model: str, *, keep_alive: Any) -> None:
        body = {
            "model": model,
            "prompt": "",
            "stream": False,
            "keep_alive": keep_alive,
        }
        try:
            if self._client is not None:
                await self._client.post(
                    f"{self._base_url}/api/generate",
                    json=body,
                    timeout=self._timeout_seconds,
                )
                return
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                await client.post(
                    f"{self._base_url}/api/generate",
                    json=body,
                )
        except Exception as exc:
            _log.warning(
                "ollama_lifecycle_failed",
                model=model,
                keep_alive=keep_alive,
                error=f"{type(exc).__name__}: {exc}",
            )


class _NoOpOllamaManager:
    """Used in dry-run mode and as a fallback for tests with no factory override."""

    async def load(self, model: str) -> None:
        _log.debug("noop_ollama_load", model=model)

    async def unload(self, model: str) -> None:
        _log.debug("noop_ollama_unload", model=model)


# ── Orchestrator ───────────────────────────────────────────────────────────


# Per-stage model requirements per the build-step-15 goal. ``cluster`` and
# ``score`` load their own models; ``write`` loads the writer model;
# others (ingest / normalize / arc-link / tts) need none.
_MODELS_FOR_STAGE: dict[str, tuple[str, ...]] = {
    STAGE_INGEST: (),
    STAGE_NORMALIZE: (),
    STAGE_CLUSTER: ("embed_model",),
    STAGE_SCORE: ("worker_model",),
    STAGE_ARC_LINK: (),
    STAGE_WRITE: ("writer_model",),
    STAGE_TTS: (),
}


def _models_to_unload_after(stage: str) -> tuple[str, ...]:
    """Models the orchestrator should unload AFTER ``stage`` completes.

    A model is unloaded after the last stage that uses it. With the
    current mapping (cluster → embed, score → worker, write → writer)
    this is straightforward: each model is loaded by exactly one stage
    and unloaded right after.
    """
    return _MODELS_FOR_STAGE.get(stage, ())


class Orchestrator:
    """Run the seven stages of a nightly MÜŞAHİT pipeline."""

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        settings: Settings,
        *,
        stage_factory: StageFactory | None = None,
        ollama: OllamaModelManager | _NoOpOllamaManager | None = None,
        timing_budgets: dict[str, StageTimingBudget] | None = None,
        disk_check_path: Path | None = None,
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._stage_factory: StageFactory = stage_factory or DefaultStageFactory(
            conn, settings
        )
        self._ollama: OllamaModelManager | _NoOpOllamaManager = (
            ollama or OllamaModelManager(base_url=settings.ollama_base_url)
        )
        self._budgets = timing_budgets or STAGE_BUDGETS
        # Default disk-check path is the data directory the DB lives in;
        # operator can override for tests via the explicit kwarg.
        self._disk_check_path = (
            disk_check_path
            if disk_check_path is not None
            else Path(settings.data_dir)
        )

    # ── Public entry point ─────────────────────────────────────────────

    async def run(
        self,
        run_id: str | None = None,
        *,
        only_stage: str | None = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> PipelineResult:
        """Execute the pipeline. Returns a :class:`PipelineResult`."""
        from musahit.common.time import tr_local_date

        if run_id is None:
            run_id = "run_" + tr_local_date().isoformat().replace("-", "")
        log = _log.bind(run_id=run_id)
        start = time.monotonic()

        # Pre-flight checks. Disk pressure is a hard abort — running a
        # stage that needs disk space when there's none is worse than
        # not running at all.
        self._precheck_disk()

        # Swap to dry-run plumbing if requested.
        stage_factory: StageFactory = (
            DryRunStageFactory() if dry_run else self._stage_factory
        )
        ollama: OllamaModelManager | _NoOpOllamaManager = (
            _NoOpOllamaManager() if dry_run else self._ollama
        )

        if dry_run:
            log.info("dry_run_mode_active")
        else:
            self._upsert_run_row(run_id)

        stages_done = self._read_stages_done(run_id) if not dry_run else []
        completed: list[str] = []
        failed: list[StageFailure] = []

        try:
            for stage_name in STAGE_ORDER:
                if only_stage is not None and stage_name != only_stage:
                    continue
                if stage_name in stages_done and not force:
                    log.info("stage_skip_completed", stage=stage_name)
                    continue

                ok = await self._run_one_stage(
                    stage_name=stage_name,
                    run_id=run_id,
                    stage_factory=stage_factory,
                    ollama=ollama,
                    dry_run=dry_run,
                    log=log,
                    failed=failed,
                )
                if ok:
                    completed.append(stage_name)
                # Unload any model the stage held — even on failure, so
                # the next stage's model loads into a clean memory budget.
                for model_attr in _models_to_unload_after(stage_name):
                    model_name: str = getattr(self._settings, model_attr)
                    await ollama.unload(model_name)
        except _CatastrophicError as exc:
            # KeyboardInterrupt / DiskPressureError / DB corruption —
            # preserve whatever stages_done we have and re-raise so
            # the CLI exits with the right code.
            self._mark_run_failed(run_id, reason=str(exc), dry_run=dry_run)
            elapsed = time.monotonic() - start
            log.warning("pipeline_catastrophic_abort", reason=str(exc))
            if isinstance(exc.original, KeyboardInterrupt):
                # Re-raise the original SIGINT so the CLI's outer
                # KeyboardInterrupt handler emits exit code 2.
                # ``from None`` suppresses chaining since the wrapper
                # adds no operator-facing value beyond the original.
                raise exc.original from None
            result = PipelineResult(
                run_id=run_id,
                status=PipelineStatus.FAILED.value,
                stages_completed=completed,
                stages_failed=failed,
                total_seconds=elapsed,
                catastrophic_reason=str(exc),
            )
            return result

        elapsed = time.monotonic() - start
        status = PipelineStatus.COMPLETED.value
        self._mark_run_completed(
            run_id, status=status, failed=failed, dry_run=dry_run
        )
        log.info(
            "pipeline_done",
            status=status,
            completed=completed,
            failed=[f.name for f in failed],
            total_seconds=round(elapsed, 2),
        )
        return PipelineResult(
            run_id=run_id,
            status=status,
            stages_completed=completed,
            stages_failed=failed,
            total_seconds=elapsed,
        )

    # ── Per-stage runner ───────────────────────────────────────────────

    async def _run_one_stage(
        self,
        *,
        stage_name: str,
        run_id: str,
        stage_factory: StageFactory,
        ollama: OllamaModelManager | _NoOpOllamaManager,
        dry_run: bool,
        log: Any,
        failed: list[StageFailure],
    ) -> bool:
        """Load required models, run the stage with timeout, persist outcome.

        Returns True on success, False on soft failure (caught
        Exception). Re-raises wrapped :class:`_CatastrophicError` for
        KeyboardInterrupt / DiskPressureError / DuckDB I/O errors.
        """
        budget = self._budgets.get(stage_name) or StageTimingBudget(soft_minutes=60.0)
        log = log.bind(stage=stage_name)

        # Load any models this stage needs.
        for model_attr in _MODELS_FOR_STAGE.get(stage_name, ()):
            model_name: str = getattr(self._settings, model_attr)
            await ollama.load(model_name)

        # Construct the stage right before running it.
        try:
            stage = stage_factory(stage_name)
        except Exception as exc:
            # Constructor failure (e.g. Piper voice missing) is a soft
            # stage failure — record + continue.
            self._record_stage_failure(
                run_id=run_id,
                stage_name=stage_name,
                exc=exc,
                failed=failed,
                dry_run=dry_run,
                log=log,
            )
            return False

        stage_start = time.monotonic()
        try:
            await asyncio.wait_for(
                stage.run(run_id),
                timeout=budget.timeout_seconds,
            )
        except KeyboardInterrupt as exc:
            # Re-raise wrapped so the outer catch can persist state.
            raise _CatastrophicError("KeyboardInterrupt", original=exc) from exc
        except DiskPressureError as exc:
            raise _CatastrophicError(
                f"DiskPressureError: {exc}", original=exc
            ) from exc
        except duckdb.IOException as exc:
            raise _CatastrophicError(
                f"duckdb.IOException: {exc}", original=exc
            ) from exc
        except TimeoutError as exc:
            reason_text = (
                f"TimeoutError: stage exceeded {budget.timeout_seconds:.0f}s "
                f"(2× the {budget.soft_minutes:g}-minute ADR-007 budget)"
            )
            self._record_stage_failure(
                run_id=run_id,
                stage_name=stage_name,
                exc=exc,
                failed=failed,
                reason_override=reason_text,
                dry_run=dry_run,
                log=log,
            )
            return False
        except Exception as exc:
            self._record_stage_failure(
                run_id=run_id,
                stage_name=stage_name,
                exc=exc,
                failed=failed,
                dry_run=dry_run,
                log=log,
            )
            return False

        elapsed = time.monotonic() - stage_start
        if elapsed > budget.soft_seconds:
            log.warning(
                "stage_slow",
                elapsed_seconds=round(elapsed, 1),
                budget_seconds=round(budget.soft_seconds, 1),
            )
        log.info("stage_complete", elapsed_seconds=round(elapsed, 2))
        if not dry_run:
            self._append_stage_done(run_id, stage_name)
        return True

    # ── Failure recording ─────────────────────────────────────────────

    def _record_stage_failure(
        self,
        *,
        run_id: str,
        stage_name: str,
        exc: BaseException,
        failed: list[StageFailure],
        reason_override: str | None = None,
        dry_run: bool,
        log: Any,
    ) -> None:
        """Per ADR-012 a stage's exception is logged + recorded in
        ``failed_stages`` + the next stage runs anyway.

        ``traceback.print_exc(file=sys.stderr)`` ensures manual /
        smoke-test invocations see the underlying error even when
        ``configure_logging()`` hasn't been called. The structured
        log call fires for production runs where JSON logs are wired.
        """
        # Print to stderr first so the operator sees the traceback at
        # the top of the failure block in their terminal.
        traceback.print_exc(file=sys.stderr)
        reason = reason_override or f"{type(exc).__name__}: {exc}"
        failure = StageFailure(name=stage_name, reason=reason)
        failed.append(failure)
        log.warning("stage_failed", stage=stage_name, reason=reason)
        if not dry_run:
            self._append_stage_failure(run_id, failure)

    # ── Persistence helpers ───────────────────────────────────────────

    def _upsert_run_row(self, run_id: str) -> None:
        """Create the ``pipeline_runs`` row (status=RUNNING) or no-op."""
        existing = self._conn.execute(
            "SELECT run_id FROM pipeline_runs WHERE run_id = ?",
            [run_id],
        ).fetchone()
        if existing is None:
            self._conn.execute(
                "INSERT INTO pipeline_runs (run_id, started_at, status, "
                "stages_done, counts, failed_stages) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    run_id,
                    utcnow(),
                    PipelineStatus.RUNNING.value,
                    json.dumps([]),
                    json.dumps({}),
                    json.dumps([]),
                ],
            )
        else:
            self._conn.execute(
                "UPDATE pipeline_runs SET status = ? WHERE run_id = ?",
                [PipelineStatus.RUNNING.value, run_id],
            )

    def _read_stages_done(self, run_id: str) -> list[str]:
        row = self._conn.execute(
            "SELECT stages_done FROM pipeline_runs WHERE run_id = ?",
            [run_id],
        ).fetchone()
        if row is None or row[0] is None:
            return []
        try:
            stages = json.loads(row[0])
            return list(stages) if isinstance(stages, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def _append_stage_done(self, run_id: str, stage_name: str) -> None:
        """Append ``stage_name`` to ``stages_done`` if absent.

        The per-stage orchestrators in earlier build steps already append
        their own stages_done entries (idempotent INSERT). Doing it again
        here is intentionally redundant — it guarantees the orchestrator's
        view of completion stays consistent even if a stage forgets to
        update the row.
        """
        stages = self._read_stages_done(run_id)
        if stage_name not in stages:
            stages.append(stage_name)
            self._conn.execute(
                "UPDATE pipeline_runs SET stages_done = ? WHERE run_id = ?",
                [json.dumps(stages), run_id],
            )

    def _append_stage_failure(self, run_id: str, failure: StageFailure) -> None:
        row = self._conn.execute(
            "SELECT failed_stages FROM pipeline_runs WHERE run_id = ?",
            [run_id],
        ).fetchone()
        existing: list[dict[str, str]] = []
        if row is not None and row[0]:
            try:
                parsed = json.loads(row[0])
                if isinstance(parsed, list):
                    existing = parsed
            except (json.JSONDecodeError, TypeError):
                existing = []
        existing.append({"name": failure.name, "reason": failure.reason})
        self._conn.execute(
            "UPDATE pipeline_runs SET failed_stages = ? WHERE run_id = ?",
            [json.dumps(existing), run_id],
        )

    def _mark_run_completed(
        self,
        run_id: str,
        *,
        status: str,
        failed: list[StageFailure],
        dry_run: bool,
    ) -> None:
        if dry_run:
            return
        self._conn.execute(
            "UPDATE pipeline_runs SET status = ?, completed_at = ? "
            "WHERE run_id = ?",
            [status, utcnow(), run_id],
        )

    def _mark_run_failed(
        self,
        run_id: str,
        *,
        reason: str,
        dry_run: bool,
    ) -> None:
        if dry_run:
            return
        self._conn.execute(
            "UPDATE pipeline_runs SET status = ?, completed_at = ? "
            "WHERE run_id = ?",
            [PipelineStatus.FAILED.value, utcnow(), run_id],
        )

    # ── Pre-flight ────────────────────────────────────────────────────

    def _precheck_disk(self) -> None:
        """Raise :class:`DiskPressureError` if free disk < configured floor."""
        path = self._disk_check_path
        path.mkdir(parents=True, exist_ok=True)
        free_bytes = shutil.disk_usage(path).free
        floor_bytes = self._settings.min_free_disk_gb * 1024**3
        if free_bytes < floor_bytes:
            raise DiskPressureError(
                f"only {free_bytes / 1024**3:.2f} GB free at {path}; "
                f"need at least {self._settings.min_free_disk_gb} GB"
            )


# ── Internal exception wrapper for catastrophic failures ───────────────────


class _CatastrophicError(RuntimeError):
    """Internal wrapper so the orchestrator's main loop can unwind cleanly.

    Carries the original exception so the run() method can re-raise it
    (KeyboardInterrupt) or surface its message (disk pressure / DuckDB
    I/O) in the PipelineResult.
    """

    def __init__(self, message: str, *, original: BaseException) -> None:
        super().__init__(message)
        self.original = original


__all__ = [
    "DefaultStageFactory",
    "DiskPressureError",
    "DryRunStageFactory",
    "OllamaModelManager",
    "Orchestrator",
    "PipelineResult",
    "StageFactory",
    "StageFailure",
]
