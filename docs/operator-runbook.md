# MÜŞAHİT · Operator Runbook

> *Quick reference · what to do when something breaks · who else to ask · nobody.*

For the full narrative of the 2026-05-26 scheduler bring-up, see
`docs/postmortems/2026-05-26-scheduler-bringup-postmortem.md`. This
runbook is the load-bearing reference · keep it short.

---

## ❯ Daily morning check · ~07:00 Turkey local

Run on the laptop after wake-up. Should take 30 seconds.

```powershell
# Today's date in run_id format
$today = "run_" + (Get-Date -Format "yyyyMMdd")

# 1. Task fired and completed cleanly?
Get-ScheduledTaskInfo -TaskName "MUSAHIT_Nightly" |
  Select-Object LastRunTime, LastTaskResult, NextRunTime
# Want · LastTaskResult: 0 · LastRunTime around 02:00 TR

# 2. Briefing landed?
$datepath = (Get-Date -Format "yyyy\\MM\\dd")
Get-ChildItem "briefings\$datepath\" -ErrorAction SilentlyContinue |
  Select-Object Name, Length, LastWriteTime
# Want · briefing.md ~50-80 KB · briefing.mp3 ~4-5 MB

# 3. DB row state?
python scripts\check_run_state.py
# Want · today's run_id · status=COMPLETED · all 7 stages · real counts

# 4. Last log clean?
Get-ChildItem "logs\nightly-$(Get-Date -Format yyyyMMdd)-*.jsonl" |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1 |
  ForEach-Object { Get-Content $_.FullName | Select-Object -Last 20 }
```

All four pass · system is live · go drink coffee.

Any one fails · drop to the relevant playbook below.

---

## ❯ Recovery playbooks

### A · Briefing missing this morning

```powershell
# Is the row even there?
python scripts\check_run_state.py
```

Three cases:

1. **No row at all** · Task Scheduler never fired the action. Go to *playbook B · scheduler not firing*.
2. **Row exists, status=RUNNING, partial stages_done** · pipeline crashed mid-run. Read the log, fix root cause, then `python scripts\reset_stuck_run.py --run-id <run_id>` followed by a re-fire.
3. **Row exists, status=COMPLETED, but no briefing on disk** · the writer stage failed and the fallback failed too, or the briefing artifact path is wrong. Inspect `musahit/writer/` logs.

### B · Task Scheduler not firing or returning non-zero

```powershell
Get-ScheduledTaskInfo -TaskName "MUSAHIT_Nightly" |
  Select-Object LastRunTime, LastTaskResult
```

Cross-reference `LastTaskResult` against the *Exit code reference*
table below. Most common · `0x80070002` (binary not found) · go to
*playbook E · scheduler binary resolution*.

If `LastRunTime` is stale (yesterday or older), the task never fired.
Likely causes in descending probability:

1. Laptop slept and did not wake · check `powercfg /lastwake` and
   `powercfg /waketimers`. Confirm `WakeToRun=true` in the task XML.
2. Network was unavailable at 02:00 · `RunOnlyIfNetworkAvailable=true`
   blocks the action. Confirm with `Get-NetConnectionProfile`.
3. Battery + `DisallowStartIfOnBatteries` (should be false, but check).

Re-fire manually to confirm fix · `schtasks /Run /TN "MUSAHIT_Nightly"`.

### C · Stuck-at-RUNNING row

The orchestrator auto-recovers on the next run when `stages_done`
covers all 7 stages. For partial stages_done with status=RUNNING:

```powershell
python scripts\reset_stuck_run.py --run-id run_YYYYMMDD --dry-run  # preview
python scripts\reset_stuck_run.py --run-id run_YYYYMMDD            # commit
```

Then either wait for the next 02:00 fire or force a re-run · see
*playbook D · force a re-run*.

### D · Force a same-day re-run

The default `pipeline run` invocation skips stages already in
`stages_done`. To genuinely re-run everything today:

```powershell
pwsh.exe -File scripts\scheduling\run_nightly.ps1 -LogDir logs -Force
```

Or directly · `python -m musahit.pipeline run --date today --force`.

This is rare · only needed when you reset a stuck row to COMPLETED and
want a real briefing for the same date.

### E · Scheduler binary resolution failed (`0x80070002`)

```powershell
# Which pwsh is the registration script picking?
.\scripts\scheduling\register_nightly_task.ps1
# Banner should show:
#   pwsh.exe : C:\Program Files\PowerShell\7\pwsh.exe
```

If the banner shows the MSI path · the action XML is correct, the
problem is elsewhere. Inspect `Export-ScheduledTask -TaskName ...`.

If the banner shows a `\WindowsApps\` path · you are on the Store
version. Stable for now but brittle to Store updates. Install PS7 via
MSI from [github.com/PowerShell/PowerShell/releases](https://github.com/PowerShell/PowerShell/releases):

```powershell
$release = Invoke-RestMethod "https://api.github.com/repos/PowerShell/PowerShell/releases/latest"
$asset = $release.assets | Where-Object { $_.name -like "PowerShell-*-win-x64.msi" } | Select-Object -First 1
$msi = "$env:TEMP\$($asset.name)"
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $msi
Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /qb" -Wait
```

Then unregister + re-register.

If the registration script itself fails to find any pwsh · the probe
order is broken. See `docs/postmortems/2026-05-26-scheduler-bringup-postmortem.md`
§ "Failure 2 · over-aggressive filter."

### F · Writer fallback fired

`writer_used_fallback: true` in the counts row means Trendyol-LLM did
not produce and `musahit/writer/fallback.py` shipped instead. The
briefing exists but the prose is the fallback shape, not the model
shape.

```powershell
# Is Ollama healthy?
ollama list
ollama ps

# Was the model loaded at the right moment?
Get-ChildItem logs\nightly-*.jsonl |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1 |
  ForEach-Object { Get-Content $_.FullName } |
  Select-String -Pattern "writer|trendyol|fallback"
```

Common causes · model not pulled · ollama service down · timeout in
generation · prompt token budget exceeded · context window overflow.
Fix is case-by-case. If fallback fires repeatedly, file an issue and
investigate the writer prompt assembly.

---

## ❯ Standing rules

1. **Install PS7 via MSI, never trust the Store package alone.** The
   `winget install Microsoft.PowerShell` package resolves to the Store
   version on Windows 11 · the alias stub is unusable from Task
   Scheduler. MSI lands at the stable
   `C:\Program Files\PowerShell\7\pwsh.exe`.

2. **Never embed `pwsh.exe` as a bare name in `<Command>`.** Always
   resolve to absolute path at registration time. Task Scheduler does
   not honor App Execution Aliases.

3. **Never edit FILE-PROTECTED files without an ADR amendment.**
   Current protected list: `sources.py` · `poller.py` · `defcon.py`.

4. **Never short-circuit the orchestrator recovery branch by hand.**
   It exists for a reason · understand the stuck row first before
   bypassing.

5. **Never delete a `pipeline_runs` row.** Use
   `scripts/reset_stuck_run.py` to mark COMPLETED · preserve history.

6. **The briefing always ships.** Per ADR-012 § fault tolerance · soft
   per-stage failures still produce `status=COMPLETED`. A missing
   briefing means the orchestrator itself crashed.

---

## ❯ Key locations

| Path | What |
|---|---|
| `data/musahit.duckdb` | Primary store · all stages read/write here |
| `data/backups/` | Nightly rotated copies |
| `logs/nightly-YYYYMMDD-HHMMSS.jsonl` | Per-run structured log |
| `briefings/YYYY/MM/DD/briefing.{md,html,mp3}` | Daily output |
| `adr/ADR-*.md` | Locked architectural decisions |
| `docs/postmortems/` | Incident write-ups |
| `docs/implementations/` | Implementation notes by date |
| `memory/build-progress.md` | Build order checklist |

---

## ❯ Key scripts

| Script | Purpose |
|---|---|
| `scripts/scheduling/register_nightly_task.ps1` | Install/re-install the Task Scheduler entry · resolves pwsh absolute path |
| `scripts/scheduling/unregister_nightly_task.ps1` | Remove the Task Scheduler entry |
| `scripts/scheduling/run_nightly.ps1` | What Task Scheduler invokes · activates venv, runs pipeline |
| `scripts/check_run_state.py` | Print latest `pipeline_runs` row to stdout |
| `scripts/reset_stuck_run.py` | Mark a stuck-at-RUNNING row COMPLETED (preserves stages_done) |
| `scripts/check_run.py` | (related diagnostic · use `check_run_state.py` instead unless you know why) |

---

## ❯ Task Scheduler exit code reference

| Hex | Decimal | Symbol | Meaning · what to do |
|---|---|---|---|
| `0x00000000` | 0 | `S_OK` | Action ran and exited cleanly · check the log for stage-level results |
| `0x00041301` | 267009 | `SCHED_S_TASK_RUNNING` | Action still running · wait 30+ seconds and re-check |
| `0x80070002` | 2147942402 | `ERROR_FILE_NOT_FOUND` | Binary or script not found · go to playbook E |
| `0x00041303` | 267011 | `SCHED_S_TASK_HAS_NOT_RUN` | Task has never run · trigger may be in future, or registration silently failed |
| `0x00041306` | 267014 | `SCHED_S_TASK_TERMINATED` | Action was force-killed · likely exceeded `ExecutionTimeLimit` (currently 2h) |

For any other code, look it up · `[System.ComponentModel.Win32Exception]::new($code).Message` from PowerShell.

---

## ❯ Verification sequence

After any change to `scripts/scheduling/`:

```powershell
.\scripts\scheduling\unregister_nightly_task.ps1
.\scripts\scheduling\register_nightly_task.ps1
# Banner · confirm pwsh path is C:\Program Files\PowerShell\7\pwsh.exe

schtasks /Run /TN "MUSAHIT_Nightly"
Start-Sleep -Seconds 30

Get-ScheduledTaskInfo -TaskName "MUSAHIT_Nightly" |
  Select-Object LastRunTime, LastTaskResult, State
# Want · LastTaskResult: 0 · State: Ready

Get-ChildItem logs\nightly-*.jsonl |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1
# Want · file timestamped within the last minute

python scripts\check_run_state.py
# Want · today's row · status reasonable
```

---

## ❯ Bootstrap period rules · v0.1

For the first 7 days of operation (counting from first 02:00 fire):

- All DEFCON ceiling rules active
- All confidence tags emitted one level lower than computed (`YÜKSEK` → `ORTA`)
- Operator MUST review and promote/demote via dashboard daily · this is what trains the day-7 recalibration
- After 7 days, system reads override history and recalibrates

Skipping the daily override review during bootstrap means the
recalibration trains on nothing. The system will still ship briefings
but its confidence calibration will be useless.

---

*Last updated · 2026-05-26 · scheduler bring-up complete · first
unattended fire pending 2026-05-27 02:00.*
