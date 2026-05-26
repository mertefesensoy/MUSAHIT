# Implementation: Nightly pipeline scheduler (Step 18)

**Date** · 2026-05-26
**Author** · Claude Code
**ADR refs** · ADR-007 (pipeline lifecycle)

---

## Problem / Motivation

The pipeline must run unattended at 02:00 Turkey local time daily so the
operator has a fresh briefing on disk by morning. Without a scheduler the
operator runs `python -m musahit.pipeline run --date today` manually each
night — unsustainable and error-prone.

---

## What Changed

| File | Description |
|---|---|
| `scripts/scheduling/nightly_task.xml.template` | Task Scheduler XML with `{{PLACEHOLDER}}` markers for project root, run script, user, log dir, start boundary. |
| `scripts/scheduling/run_nightly.ps1` | Entry point invoked by Task Scheduler: ensures Ollama is running, runs the pipeline, captures logs. |
| `scripts/scheduling/register_nightly_task.ps1` | Reads XML template, substitutes placeholders from current environment, registers the task. |
| `scripts/scheduling/unregister_nightly_task.ps1` | Stops + removes the scheduled task cleanly. |
| `docs/operations/scheduler-setup.md` | Operator runbook: register, test-fire, unregister, troubleshoot. |

---

## Implementation Approach

**Windows Task Scheduler** via XML import rather than programmatic task
construction. The XML template separates configuration (schedule, wake
settings, battery policy) from environment-specific values (paths, user).
The registration script substitutes `{{PLACEHOLDER}}` markers with values
derived from `$PSScriptRoot` and the current Windows identity, then imports
via `Register-ScheduledTask` (PowerShell 7 cmdlet) with a `schtasks /Create
/XML` fallback.

The run script (`run_nightly.ps1`) is the sole Action. It:
1. Resolves the project root from its own file location.
2. Checks Ollama liveness (`ollama list`), starts `ollama serve` if needed,
   waits up to 30 s.
3. Runs `python -m musahit.pipeline run --date today` with stdout+stderr
   captured to `logs/nightly-<timestamp>.jsonl` via `Tee-Object`.
4. Exits with the pipeline's exit code (0/1/2 per `pipeline.py` contract).

**Key Task Scheduler settings:**
- `WakeToRun=true` — laptop wakes from sleep at 02:00.
- `StartWhenAvailable=true` — if 02:00 was missed (laptop off), runs on next wake.
- `DisallowStartIfOnBatteries=false` + `StopIfGoingOnBatteries=false` — runs on battery.
- `RunOnlyIfNetworkAvailable=true` — skips if offline (pipeline needs network for sources).
- `ExecutionTimeLimit=PT2H` — hard kill after 2 hours (generous ceiling).
- `MultipleInstancesPolicy=IgnoreNew` — prevents overlapping runs.

---

## Design Decisions

| Alternative | Why rejected |
|---|---|
| Python-based scheduler (APScheduler, cron library) | Requires a running Python process 24/7. Task Scheduler is OS-native, survives reboots, wakes from sleep. |
| `schtasks /Create` with inline arguments (no XML) | Can't express all settings (WakeToRun, battery policy, network check) without XML. |
| Hardcoded paths in scripts | Breaks on any directory move. `$PSScriptRoot`-relative paths + `{{PLACEHOLDER}}` substitution make it portable. |
| Pre-warm Ollama models in run script | Over-engineering for single operator. The pipeline's orchestrator handles model loading. |
| Pause OneDrive during pipeline run | Operator accepted the sync-conflict risk. Adding OneDrive control would require COM automation. |

---

## Verification

```powershell
# Register (elevated terminal):
& .\scripts\scheduling\register_nightly_task.ps1

# Test-fire:
schtasks /Run /TN "MUSAHIT_Nightly"

# Check log after ~30s:
Get-ChildItem logs/nightly-*.jsonl | Sort-Object LastWriteTime -Descending | Select-Object -First 1

# Verify task state:
Get-ScheduledTask MUSAHIT_Nightly | Select-Object State, LastRunTime, LastTaskResult

# Clean removal:
& .\scripts\scheduling\unregister_nightly_task.ps1
```

---

## Related Docs

- `docs/operations/scheduler-setup.md` — operator runbook
- `musahit/pipeline.py` — CLI entry point invoked by the scheduler
- BOOTSTRAP.md — Step 18 (nightly scheduler)
