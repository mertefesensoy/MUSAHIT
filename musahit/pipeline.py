"""Operator-facing CLI for the MÜŞAHİT pipeline.

Invocation form per ADR-007:

    python -m musahit.pipeline run --date today
    python -m musahit.pipeline run --date today --stage cluster
    python -m musahit.pipeline run --date 2026-05-20
    python -m musahit.pipeline run --date today --dry-run
    python -m musahit.pipeline status --date today
    python -m musahit.pipeline resume --date today

Exit codes:
    0 — pipeline reached COMPLETED status
    1 — pipeline reached FAILED status (catastrophic abort)
    2 — operator pressed Ctrl-C / process received SIGINT

The CLI's sole job is parsing args, configuring logging, opening the
DuckDB connection, and instantiating :class:`Orchestrator`. All
business logic lives in the orchestrator and stage modules.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import date

import duckdb

from musahit.common.config import get_settings
from musahit.common.db import open_connection
from musahit.common.logging import configure_logging, get_logger
from musahit.common.time import tr_local_date
from musahit.common.types import PipelineStatus
from musahit.orchestrator import Orchestrator, PipelineResult
from musahit.stages import STAGE_ORDER

_log = get_logger("musahit.pipeline")

EXIT_OK: int = 0
EXIT_FAILED: int = 1
EXIT_SIGINT: int = 2


# ── Argparse ───────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="musahit.pipeline",
        description="MÜŞAHİT nightly pipeline orchestrator (ADR-001, ADR-007).",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    p_run = sub.add_parser("run", help="execute the pipeline")
    _add_common_args(p_run)
    p_run.add_argument(
        "--stage",
        choices=STAGE_ORDER,
        default=None,
        help="run only this stage (default: run all)",
    )
    p_run.add_argument(
        "--force",
        action="store_true",
        help="re-run stages already in stages_done",
    )
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        help="skip DB writes / Ollama / Piper; record dry-run stage calls",
    )

    p_status = sub.add_parser(
        "status", help="show the latest pipeline_runs row for a date"
    )
    _add_common_args(p_status)

    p_resume = sub.add_parser(
        "resume",
        help="re-run only stages that haven't completed for the given date",
    )
    _add_common_args(p_resume)

    return p


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--date",
        default="today",
        help=(
            "TR-local date (YYYY-MM-DD) or the literal 'today' "
            "(default: today, resolved via musahit.common.time.tr_local_date)"
        ),
    )


def _resolve_date(arg: str) -> date:
    if arg == "today":
        return tr_local_date()
    try:
        return date.fromisoformat(arg)
    except ValueError as exc:
        raise SystemExit(
            f"--date must be 'today' or YYYY-MM-DD ({arg!r}: {exc})"
        ) from exc


def _run_id_for(target_date: date) -> str:
    return "run_" + target_date.isoformat().replace("-", "")


# ── Command handlers ───────────────────────────────────────────────────────


def _cmd_run(
    args: argparse.Namespace,
    conn: duckdb.DuckDBPyConnection,
) -> int:
    target_date = _resolve_date(args.date)
    run_id = _run_id_for(target_date)
    settings = get_settings()
    orchestrator = Orchestrator(conn, settings)
    try:
        result = asyncio.run(
            orchestrator.run(
                run_id=run_id,
                only_stage=args.stage,
                force=args.force,
                dry_run=args.dry_run,
            )
        )
    except KeyboardInterrupt:
        _log.warning("pipeline_sigint", run_id=run_id)
        return EXIT_SIGINT
    _print_result(result)
    return EXIT_OK if result.status == PipelineStatus.COMPLETED.value else EXIT_FAILED


def _cmd_status(
    args: argparse.Namespace,
    conn: duckdb.DuckDBPyConnection,
) -> int:
    target_date = _resolve_date(args.date)
    run_id = _run_id_for(target_date)
    row = conn.execute(
        "SELECT run_id, started_at, completed_at, status, stages_done, "
        "counts, failed_stages "
        "FROM pipeline_runs WHERE run_id = ?",
        [run_id],
    ).fetchone()
    if row is None:
        print(f"no pipeline_runs row for run_id={run_id}")
        return EXIT_FAILED
    (
        rid,
        started_at,
        completed_at,
        status,
        stages_done,
        counts,
        failed_stages,
    ) = row
    print(f"run_id        {rid}")
    print(f"started_at    {started_at}")
    print(f"completed_at  {completed_at}")
    print(f"status        {status}")
    print(f"stages_done   {stages_done}")
    print(f"counts        {counts}")
    print(f"failed_stages {failed_stages}")
    return EXIT_OK if status == PipelineStatus.COMPLETED.value else EXIT_FAILED


def _cmd_resume(
    args: argparse.Namespace,
    conn: duckdb.DuckDBPyConnection,
) -> int:
    # Resume == run without --force; the orchestrator's per-stage skip
    # logic does the resumption work. Convenience subcommand.
    args.stage = None
    args.force = False
    args.dry_run = False
    return _cmd_run(args, conn)


# ── Output helpers ─────────────────────────────────────────────────────────


def _print_result(result: PipelineResult) -> None:
    """One-screen summary the operator can read at 07:00."""
    print(f"run_id            {result.run_id}")
    print(f"status            {result.status}")
    print(f"total_seconds     {result.total_seconds:.1f}")
    print(f"stages_completed  {', '.join(result.stages_completed) or '(none)'}")
    if result.stages_failed:
        print("stages_failed:")
        for f in result.stages_failed:
            print(f"  - {f.name}: {f.reason}")
    else:
        print("stages_failed     (none)")
    if result.catastrophic_reason:
        print(f"catastrophic      {result.catastrophic_reason}")


# ── Entry point ────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    """Pipeline CLI entry point. Returns the desired exit code.

    Intentionally returns rather than calls ``sys.exit`` so tests can
    invoke ``main([...])`` directly.
    """
    configure_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    with open_connection(settings.db_path) as conn:
        if args.subcommand == "run":
            return _cmd_run(args, conn)
        if args.subcommand == "status":
            return _cmd_status(args, conn)
        if args.subcommand == "resume":
            return _cmd_resume(args, conn)
        parser.error(f"unknown subcommand {args.subcommand!r}")
    # parser.error raises SystemExit; this line is unreachable but
    # appeases mypy by giving every code path a return value.
    return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_SIGINT",
    "main",
]
