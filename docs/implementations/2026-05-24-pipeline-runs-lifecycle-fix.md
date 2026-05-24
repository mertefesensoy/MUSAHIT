# Implementation: pipeline_runs lifecycle fix — stuck-at-RUNNING + resume-rewipe

**Date** · 2026-05-24
**Author** · MERT EFE ŞENSOY
**ADR refs** · ADR-007 (Resumability), ADR-012 (Failure isolation)

---

## ❯ Problem / Motivation

Two bugs surfaced from the 2026-05-23 → 2026-05-24 smoke run:

1. **Stuck-at-RUNNING.** The smoke run completed every stage and shipped
   `briefings/2026/05/23/briefing.md` + `briefing.mp3`, yet
   `pipeline_runs.run_20260524` remained at `status=RUNNING`,
   `completed_at=NULL`. `Orchestrator._mark_run_completed` had not fired
   even though every stage had succeeded. The terminal-status write
   lived outside any `finally`, so any exception or SIGINT that escaped
   the `except _CatastrophicError` block (e.g., between the for-loop and
   the completion write, or during a stage's post-success
   `_append_stage_done` bookkeeping) silently aborted the run leaving
   the row stuck.

2. **Resume re-runs every stage.** Today's
   `python -m musahit.pipeline run --date today` did not resume from
   the last completed stage. Instead it reset `started_at`, wiped
   `stages_done`, and re-ran ingest → normalize → cluster from scratch
   (the row ended up at `stages_done=["ingest","normalize","cluster"]`
   with `articles=0` from the re-ingest and
   `articles_normalized=223` from re-normalising yesterday's articles).

The two bugs compound: stuck-at-RUNNING leaves a row in an unsafe
intermediate state, and any subsequent run that re-enters the ingest
stage triggers `IngestPoller._upsert_run_start`, whose destructive
`ON CONFLICT UPDATE` resets `stages_done` / `started_at` /
`completed_at` / `counts`. The poller is `FILE-PROTECTED` (per
`BOOTSTRAP.md § File protection list`) because its reset is
load-bearing for standalone poller invocation (`tests/test_poller.py`
re-runs the poller alone and depends on the wipe to start fresh). The
fix had to live in the orchestrator.

---

## ❯ Root cause

| # | Symptom | Root cause |
|---|---|---|
| 1 | Row stuck at RUNNING with all 7 stages done | `_mark_run_completed` not in a `finally`; any unhandled exception, SIGINT between `await` points, or post-success bookkeeping failure skips the terminal-status write. |
| 2 | Resume re-ran every stage | When the orchestrator's loop entered the ingest stage (because "ingest" was missing from a partial `stages_done`), `IngestPoller._upsert_run_start`'s `ON CONFLICT UPDATE` wiped `stages_done` back to `[]`. `_append_stage_done` then rebuilt only the stages that ran *this attempt*, losing any prior progress not in the orchestrator's local view. |

---

## ❯ What Changed

| File | Description |
|---|---|
| `musahit/orchestrator.py` | (a) Added `try/finally` around `run()`'s main body that guarantees a terminal status write (`_mark_run_failed` if nothing else marked the row). (b) Added auto-recovery: if `stages_done` already contains every stage in `STAGE_ORDER` at the start of a non-`--force` non-`--stage` run, immediately mark `COMPLETED` and return without running any stage. (c) Rewrote `_append_stage_done` to merge an in-memory `_stages_view` with the DB's current `stages_done` on every write, so a stage that internally wipes the row (the IngestPoller's destructive UPSERT) cannot erase prior-stage progress. (d) Moved the normal-completion `_mark_run_completed` call inside the `try` so `terminal_set=True` happens before the `finally`. |
| `tests/test_orchestrator.py` | Added `TestPipelineRunsLifecycle` with five tests: resume-against-COMPLETED-is-noop, auto-recovery-fires-for-stuck-with-7-stages, resume-runs-only-missing-stages, merge-on-write-defeats-the-wipe, finally-guard-marks-FAILED-on-uncaught-exception. |
| `scripts/reset_stuck_run.py` | One-off script to mark a stuck `pipeline_runs` row `COMPLETED` (preserving `stages_done` as-is) with a clear warning about missing stages. Operator runs after the fix is verified. |
| `docs/implementations/2026-05-24-pipeline-runs-lifecycle-fix.md` | This document. |
| `memory/build-progress.md` | Between-step entry recording the fix. |

---

## ❯ Implementation Approach

### A · `try / finally` for terminal-status guarantee

The orchestrator now wraps the entire body of `run()` (auto-recovery
check + main for-loop + normal completion) in `try/except/finally`:

```python
terminal_set = False
try:
    # auto-recovery, for-loop, normal completion (sets terminal_set=True)
    ...
except _CatastrophicError as exc:
    self._mark_run_failed(...)
    terminal_set = True
    ...
finally:
    if not dry_run and not terminal_set:
        self._mark_run_failed(
            run_id, reason="orchestrator_uncaught_exit", dry_run=dry_run
        )
```

The `terminal_set` flag means the finally only writes if no normal /
catastrophic path already wrote a terminal status. `_mark_run_failed`
is the safe default: future operator inspection will see the failure
and re-run with the new auto-recovery available if the stages had
actually all completed.

### B · Auto-recovery for stuck-at-RUNNING

```python
if (
    not dry_run
    and only_stage is None
    and not force
    and set(STAGE_ORDER).issubset(set(stages_done))
):
    log.warning("pipeline_auto_complete_stuck_running", stages_done=...)
    self._mark_run_completed(run_id, ...)
    terminal_set = True
    return PipelineResult(status=COMPLETED, ...)
```

Fires only when the operator is doing a "normal" resume (no
`--stage`, no `--force`) and every stage already shows complete. The
log is at WARNING so the operator notices and investigates the
*upstream* cause of stuck-RUNNING rather than relying on this masking
the underlying problem (tripwire from the goal).

### C · Merge-on-write `_append_stage_done`

```python
def _append_stage_done(self, run_id, stage_name):
    view = self._stages_view  # in-memory, populated at run() start
    if stage_name not in view:
        view.append(stage_name)
    db_stages = self._read_stages_done(run_id)
    merged = list(view)
    for s in db_stages:  # also include anything else the DB has
        if s not in merged:
            merged.append(s)
    self._conn.execute(
        "UPDATE pipeline_runs SET stages_done = ? WHERE run_id = ?",
        [json.dumps(merged), run_id],
    )
```

The orchestrator initialises `_stages_view = list(initial_stages_done)`
at the top of `run()`. Every `_append_stage_done` writes the union of
its in-memory view and whatever the DB currently holds. If
`IngestPoller._upsert_run_start` wipes `stages_done` to `[]` between
two orchestrator-driven stages, the next `_append_stage_done` rewrites
the row from the in-memory view and the wipe is invisible.

---

## ❯ Mathematical / Statistical Details

n/a · purely structural change.

---

## ❯ Design Decisions

| Decision | Alternatives considered | Why this one |
|---|---|---|
| Auto-recover stuck-at-RUNNING only when all 7 stages are in `stages_done` | (a) auto-complete any RUNNING row regardless of stages; (b) require operator confirmation | (a) hides real failures; (b) defeats the point of auto-recovery for a known-good state. Logging at WARN gives the operator visibility without blocking. |
| Merge-on-write `_append_stage_done` over removing `IngestPoller._upsert_run_start` | (a) delete `_upsert_run_start`; (b) add a flag to `IngestPoller(__init__, manage_run_row=False)` | (a) breaks standalone poller invocation (`test_poller.py:278` rerun-same-runid test) AND the file is `FILE-PROTECTED`. (b) is cleaner but requires modifying the protected file, which the goal's tripwire forbids. Merge-on-write is invisible to the poller while protecting the orchestrator's invariants. |
| `_mark_run_failed` (not `_mark_run_completed`) in the `finally` | Always-COMPLETED, or skip-finally | If we got to the `finally` without `terminal_set=True`, *something is wrong* — an exception escaped both the normal path and the `_CatastrophicError` catch. FAILED is honest. The next run's auto-recovery handles the case where the stages really all completed. |
| Move normal completion inside the `try` | Leave it after the `try/except` with a sentinel | Tighter coupling between "stages succeeded" and "row marked COMPLETED"; eliminates the window where the finally would write FAILED then the post-try code would overwrite with COMPLETED. |

---

## ❯ Verification

### Automated

```powershell
python -m pytest tests/test_orchestrator.py::TestPipelineRunsLifecycle -v
python -m pytest --tb=short -q   # full suite · 627 passed, 2 skipped
```

The five new tests pin every behaviour in the fix:

1. `test_resume_against_completed_row_is_noop` — resume an already-COMPLETED run, no stages execute.
2. `test_resume_auto_recovers_stuck_at_running_with_full_stages` — the reported scenario; row stays at all-7-stages but flips to COMPLETED on the next run.
3. `test_resume_runs_only_missing_stages` — partial `stages_done` (3/7) resumes from stage 4 only.
4. `test_finally_marks_failed_when_stage_wipes_stages_done` — fake "ingest wipes stages_done" stage; orchestrator's merge restores the full union.
5. `test_finally_marks_failed_on_uncaught_exception` — monkey-patched `_append_stage_done` raises; row ends FAILED, not stuck-at-RUNNING.

### Operator verification on the live DB

```powershell
# 1. Inspect current state
python scripts/check_run_state.py

# 2. Optionally reset the original incident row (dry-run first)
python scripts/reset_stuck_run.py --dry-run
python scripts/reset_stuck_run.py

# 3. Run the next pipeline normally. With the fix in place:
#    · auto-recovery flips a stuck-at-RUNNING row to COMPLETED
#    · resume picks up from the last successful stage
#    · the IngestPoller's wipe is masked by merge-on-write
python -m musahit.pipeline run --date today
```

---

## ❯ Operator caveats

1. **Auto-recovery logs at WARNING.** When the operator sees
   `pipeline_auto_complete_stuck_running` in the logs, the *resume*
   succeeded but the *original run* died unexpectedly. Investigate
   what killed the prior process (reboot? SIGINT? unhandled stage
   exception?) before dismissing it.

2. **`scripts/reset_stuck_run.py` does NOT re-run stages.** It only
   flips `status` to `COMPLETED` (and writes `completed_at`). If the
   row's `stages_done` is partial, the script warns about missing
   stages. After running it, decide whether to re-run the pipeline.

3. **`IngestPoller.run()` called standalone (outside the
   orchestrator) still wipes the row.** That behaviour is preserved
   intentionally so the poller's own tests and any future direct
   invocation work as documented. Only orchestrator-driven runs are
   defended.

4. **The `finally` writes `FAILED`, not `COMPLETED`.** If the run
   genuinely completed but an unhandled exception escaped post-success
   bookkeeping, the row will read `FAILED` until the next run's
   auto-recovery flips it to `COMPLETED`. The pipeline_runs table's
   `failed_stages` column is still authoritative for *which* stages
   raised — the run-level `status` reflects the *terminal write
   outcome*.

---

## ❯ Related Docs

- `docs/implementations/2026-05-23-pipeline.md` — original orchestrator
  implementation
- `docs/implementations/2026-05-23-poller.md` — IngestPoller
  implementation (the `_upsert_run_start` UPSERT lives there)
- `docs/implementations/2026-05-24-date-propagation-fix.md` — sibling
  fix from earlier today
- `adr/ADR-007-resumability.md` — defines the "skip stages already
  done" contract this fix preserves
- `adr/ADR-012-failure-isolation.md` — defines the soft-failure
  semantics this fix does not disturb
