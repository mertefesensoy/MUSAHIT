"""One-off reset for the 2026-05-24 stuck-at-RUNNING incident.

The 2026-05-23 -> 2026-05-24 smoke run completed all 7 stages and shipped
the briefing/audio, but the pipeline process died before
``Orchestrator._mark_run_completed`` fired (Ctrl-C, reboot, or unhandled
exception during stage bookkeeping). The pipeline_runs row sat at
``status=RUNNING`` with ``completed_at=NULL`` despite ``stages_done``
holding all 7 stages. Today's ``pipeline run --date today`` then reset
``started_at`` and re-ran every stage because of a separate destructive
UPSERT in the ingest poller.

The orchestrator now auto-recovers stuck-at-RUNNING rows on the next
run (see ``docs/implementations/2026-05-24-pipeline-runs-lifecycle-fix.md``),
so this script is only needed to hand-clean the row that already exists
before the next live pipeline run · or to verify the fix's effect on
the current DB state.

Usage::

    python scripts/reset_stuck_run.py            # reset run_20260524 to COMPLETED
    python scripts/reset_stuck_run.py --dry-run  # print intent, don't write
    python scripts/reset_stuck_run.py --run-id run_20260525
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from musahit.common.config import get_settings  # noqa: E402
from musahit.common.db import open_connection  # noqa: E402
from musahit.common.time import utcnow  # noqa: E402
from musahit.common.types import PipelineStatus  # noqa: E402
from musahit.stages import STAGE_ORDER  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id",
        default="run_20260524",
        help="run_id to reset (default: run_20260524 · the original incident)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print intent without writing",
    )
    args = parser.parse_args()

    settings = get_settings()
    with open_connection(settings.db_path, load_vss=False) as conn:
        row = conn.execute(
            "SELECT status, completed_at, stages_done, failed_stages "
            "FROM pipeline_runs WHERE run_id = ?",
            [args.run_id],
        ).fetchone()
        if row is None:
            print(f"no pipeline_runs row for run_id={args.run_id}", file=sys.stderr)
            return 1

        status, completed_at, stages_done_json, failed_stages_json = row
        stages_done = json.loads(stages_done_json) if stages_done_json else []
        failed_stages = json.loads(failed_stages_json) if failed_stages_json else []

        print(f"run_id           {args.run_id}")
        print(f"current status   {status}")
        print(f"completed_at     {completed_at}")
        print(f"stages_done      {stages_done}")
        print(f"failed_stages    {failed_stages}")

        if status == PipelineStatus.COMPLETED.value:
            print()
            print("row is already COMPLETED · nothing to do")
            return 0

        missing = [s for s in STAGE_ORDER if s not in stages_done]
        print()
        print("planned UPDATE (stages_done preserved as-is):")
        print("  status        -> COMPLETED")
        print(f"  completed_at  -> {utcnow().isoformat()}Z")
        if missing:
            print()
            print("WARNING: the following stages are NOT in stages_done:")
            for m in missing:
                print(f"  - {m}")
            print(
                "Marking this row COMPLETED claims the run is done even "
                "though the missing stages never ran. If today's "
                "briefing/audio is required, re-run the pipeline AFTER "
                "this script · with the orchestrator's auto-recovery and "
                "merge-on-write fixes in place, resume will work without "
                "re-running stages 1-3."
            )

        if args.dry_run:
            print()
            print("--dry-run · not writing")
            return 0

        conn.execute(
            "UPDATE pipeline_runs SET status = ?, completed_at = ? "
            "WHERE run_id = ?",
            [
                PipelineStatus.COMPLETED.value,
                utcnow(),
                args.run_id,
            ],
        )
        print()
        print(f"reset {args.run_id} to COMPLETED (stages_done unchanged)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
