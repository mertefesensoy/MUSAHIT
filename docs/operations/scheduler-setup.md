# Nightly Pipeline Scheduler — Operator Runbook

The pipeline runs automatically at **02:00 Turkey local time** daily via
Windows Task Scheduler. The laptop wakes from sleep, runs the full pipeline,
writes the briefing to disk, and returns to sleep. The briefing is ready by
~02:20 for the operator's morning listen.

---

## Prerequisites

- **PowerShell 7+** (`pwsh.exe`) — ships with Windows 11 or install from
  [github.com/PowerShell/PowerShell](https://github.com/PowerShell/PowerShell)
- **Ollama** installed and accessible on `PATH`
- **Python** with the `musahit` package importable (`python -m musahit.pipeline`
  should print the help text)
- Network connectivity at 02:00 (RSS/HTML sources need it)

---

## Register the task

Open an **elevated** PowerShell terminal (right-click → "Run as Administrator")
and run:

```powershell
& "C:\Users\senso\OneDrive\Masaüstü\MÜŞAHİT\scripts\scheduling\register_nightly_task.ps1"
```

Or from the project root:

```powershell
& .\scripts\scheduling\register_nightly_task.ps1
```

The script will:
1. Read the XML template and substitute paths for this machine.
2. Register the `MUSAHIT_Nightly` task in Task Scheduler.
3. Print a verification summary and the test-fire command.

**Why elevated?** The `WakeToRun` setting (wake laptop from sleep) may
require Administrator privileges to register. If you register without
elevation the task will work normally but won't wake the machine.

---

## Test-fire

After registration, immediately test that the wiring works:

```powershell
schtasks /Run /TN "MUSAHIT_Nightly"
```

Then verify:

```powershell
# Wait ~30 seconds for the pipeline to start, then check logs:
Get-ChildItem "logs" | Sort-Object LastWriteTime -Descending | Select-Object -First 1

# Check task status:
Get-ScheduledTask -TaskName "MUSAHIT_Nightly" | Select-Object State, LastRunTime, LastTaskResult
```

A `LastTaskResult` of `0` means success (pipeline exit code 0 = COMPLETED).

---

## Check logs

Every run writes to `logs/nightly-YYYYMMDD-HHmmss.jsonl`. To inspect the
most recent log:

```powershell
Get-Content (Get-ChildItem "logs/nightly-*.jsonl" | Sort-Object LastWriteTime -Descending | Select-Object -First 1)
```

---

## Unregister the task

```powershell
& .\scripts\scheduling\unregister_nightly_task.ps1
```

Stops the task if running, removes it from Task Scheduler, and verifies
removal.

---

## Re-register after moving the project

If the project directory moves (e.g., to a different drive), re-run the
registration script. It reads all paths from `$PSScriptRoot` at registration
time — no manual path editing needed.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Task doesn't fire at 02:00 | `Get-ScheduledTask MUSAHIT_Nightly` — is it `Ready`? |
| Laptop doesn't wake | Re-register from an elevated terminal. Check Power Options → Sleep → "Allow wake timers" is Enabled. |
| Pipeline starts but Ollama fails | `run_nightly.ps1` starts Ollama automatically. Check `ollama serve` runs manually. |
| `LastTaskResult` is nonzero | Read the log file. Exit code 1 = FAILED, 2 = interrupted. |
| Network unavailable at 02:00 | `RunOnlyIfNetworkAvailable` is set — the task skips if offline. `StartWhenAvailable` causes it to run when the network returns. |
| Task exists but won't re-register | The registration script removes the old task first. If it's stuck, run `unregister_nightly_task.ps1` manually. |

---

## Files

| File | Purpose |
|---|---|
| `scripts/scheduling/nightly_task.xml.template` | Task Scheduler XML with `{{PLACEHOLDER}}` markers |
| `scripts/scheduling/run_nightly.ps1` | The script Task Scheduler invokes (the Action) |
| `scripts/scheduling/register_nightly_task.ps1` | Substitutes placeholders and imports the task |
| `scripts/scheduling/unregister_nightly_task.ps1` | Removes the task cleanly |
