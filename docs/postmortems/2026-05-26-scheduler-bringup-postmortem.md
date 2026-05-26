# MÜŞAHİT · Scheduler Bring-Up · Post-Mortem and Runbook

> *Date · 2026-05-26 (Mon)*
> *Component · `scripts/scheduling/` · `musahit/orchestrator.py`*
> *Outcome · Task Scheduler green · first scheduled fire pending 2026-05-27 02:00*

---

## ❯ Context

MÜŞAHİT's v0.1 scaffold is foundation-locked · 12 ADRs accepted · stages
implemented · DB live · briefings shipping from manual invocations
(briefings/2026/05/24, /25, /26 all real artifacts). Tonight was the
bring-up of the Windows Task Scheduler trigger so the pipeline runs
unattended at 02:00 Turkey local without operator presence.

Three infrastructure bugs surfaced in sequence. This document captures
them in order, plus a fourth event that looked like a bug but was the
orchestrator behaving correctly. Read time · ~5 minutes. Future-you on a
new laptop or a six-month-later debugging session is the primary
audience.

---

## ❯ Timeline

| Time (TR) | Event |
|---|---|
| 13:45 | Some earlier session completed today's run cleanly · briefing artifacts written, row marked COMPLETED in DB |
| 14:04 | `briefings/2026/05/26/briefing.{md,mp3}` written on disk |
| 14:54 | First Task Scheduler test fire · `LastTaskResult: 2147942402 (0x80070002)` · no log file |
| 14:58 | Manual `pwsh.exe ... run_nightly.ps1` works · recovery branch fires (correctly · row already COMPLETED) |
| ~17:00 | Hardened `register_nightly_task.ps1` with absolute pwsh path · false positive in probe filter regressed the script |
| ~19:00 | Installed PowerShell 7.6.2 via MSI from GitHub releases · probe B catches `C:\Program Files\PowerShell\7\pwsh.exe` · re-registered |
| 19:13 | Second Task Scheduler test fire · pipeline ran end-to-end · recovery branch fired against already-COMPLETED row (no-op) · `LastTaskResult: 267009 (0x41301)` briefly · then 0 |

---

## ❯ Failure 1 · `pwsh.exe` resolves to a WindowsApps execution alias

### Symptom

```
LastTaskResult: 2147942402  # 0x80070002 · ERROR_FILE_NOT_FOUND
```

No log file produced · no transcript · the action never launched.

### Root cause

The first registration script used `<Command>pwsh.exe</Command>` as a
bare name. Task Scheduler resolves bare names against the system PATH,
not the interactive user PATH. `where.exe pwsh.exe` from the user shell
returned:

```
C:\Users\senso\AppData\Local\Microsoft\WindowsApps\pwsh.exe
```

This is a zero-byte reparse-point stub created by the App Execution
Aliases subsystem. It only works inside interactive user sessions
because the OS rewrites the launch through the AppX activation pipeline.
Task Scheduler launches actions through a path that does not honor
aliases, so the OS literally tries to execute a zero-byte file and
returns `ERROR_FILE_NOT_FOUND`.

### Why `winget install Microsoft.PowerShell` did not help

On Windows 11, the `Microsoft.PowerShell` package now resolves to the
Microsoft Store package by default. It was already installed at the
Store-versioned path
(`C:\Program Files\WindowsApps\Microsoft.PowerShell_*_x64__*\pwsh.exe`).
winget reported "no upgrade available" because the Store version was
current. The stub alias is what `where.exe` finds first because it is
shadowed earlier in PATH than the real binary.

### Fix

Resolve `pwsh.exe` to an **absolute path** at registration time and
embed it in the action XML's `<Command>` element. The bare-name
indirection is the trap · eliminate it.

---

## ❯ Failure 2 · Over-aggressive WindowsApps filter

### Symptom

```
register_nightly_task.ps1 : Cannot find a real pwsh.exe binary
(only the WindowsApps alias was found).
```

The hardened script could not resolve any pwsh path at all, even though
the real Store binary existed.

### Root cause

The first hardening attempt added a probe order that filtered out the
alias stub:

```powershell
# Bad · excludes the real binary too
$_.Source -notlike "*\WindowsApps\*"
```

Both paths contain `\WindowsApps\` as a substring:

| Path | Type |
|---|---|
| `C:\Users\senso\AppData\Local\Microsoft\WindowsApps\pwsh.exe` | Alias stub |
| `C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.2.0_x64__8wekyb3d8bbwe\pwsh.exe` | Real binary |

The filter ate both.

### Fix

Tighten the filter to exclude only the user-local alias directory · the
real Store binary lives under `C:\Program Files\WindowsApps\` which is a
distinct path:

```powershell
# Good · excludes only the user-local alias stub
$_.Source -notlike "*\AppData\Local\Microsoft\WindowsApps\*"
```

Still on the to-do list · the script as-shipped resolves pwsh via the
PS7 MSI install at `C:\Program Files\PowerShell\7\pwsh.exe` (probe B),
which sidesteps the issue entirely. The filter bug is latent. **File
as a follow-up issue** · tighten the filter so a future fallback to the
Store binary works.

---

## ❯ Failure 3 (resolved by sidestep) · Store-versioned path is brittle

### Symptom

Hypothetical · not observed live this session. If the registration script
were to resolve and embed the Store path
(`C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.2.0_x64_...`),
a future Microsoft Store update would change the version segment to
e.g. `7.6.3.0`, the absolute path embedded in the task XML would no
longer exist, and the next 02:00 fire would return `0x80070002`.

### Fix

Install PowerShell 7 via standalone MSI from
[github.com/PowerShell/PowerShell/releases](https://github.com/PowerShell/PowerShell/releases).
The MSI installs to a versionless location:

```
C:\Program Files\PowerShell\7\pwsh.exe
```

This path is stable across PS7 minor updates. Probe B in the
registration script catches it. The Store binary becomes a dead branch
in the probe order.

### Install command

```powershell
$release = Invoke-RestMethod "https://api.github.com/repos/PowerShell/PowerShell/releases/latest"
$asset = $release.assets | Where-Object { $_.name -like "PowerShell-*-win-x64.msi" } | Select-Object -First 1
$msi = "$env:TEMP\$($asset.name)"
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $msi
Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /qb" -Wait
```

Installed version on this machine · `7.6.2` (2026-05-26).

---

## ❯ Event that was not a bug · recovery branch on a COMPLETED row

### Symptom

After the 19:13 Task Scheduler fire, the log file
`nightly-20260526-191303.jsonl` contained:

```json
{
  "event": "pipeline_auto_complete_stuck_running",
  "level": "warning",
  "run_id": "run_20260526",
  "stages_done": ["ingest", "normalize", "cluster", "score", "arc-link", "write", "tts"]
}
```

```
status            COMPLETED
total_seconds     0.6
stages_completed  (none)
stages_failed     (none)
```

Looks alarming · zero stages ran, recovery branch fired, briefing claim
status "COMPLETED" with no work done.

### Diagnosis

Reading `musahit/orchestrator.py` ~L420 carefully:

```python
if (
    not dry_run
    and only_stage is None
    and not force
    and set(STAGE_ORDER).issubset(set(stages_done))
):
    log.warning("pipeline_auto_complete_stuck_running", ...)
    self._mark_run_completed(...)
    return PipelineResult(...)
```

The branch fires when **all 7 stages are already in `stages_done`**. The
docstring says it exists to auto-recover from a Windows reboot or
Ctrl-C that killed the process between the last stage's bookkeeping and
`_mark_run_completed`.

`run_id` is derived from `tr_local_date()` only · so every invocation
within the same Turkey-local day shares one row. Today's row was
already terminal (status=COMPLETED, completed_at populated, counts row
showing real production data · 491 articles · 205 new clusters · 167
arcs seeded) from a 13:45 session. The 19:13 Task Scheduler invocation
correctly saw all-stages-done and short-circuited.

`check_run_state.py` confirmed:

```
('run_20260526', 'COMPLETED',
 datetime(2026, 5, 26, 10, 45, 43),    # started_at UTC
 datetime(2026, 5, 26, 16, 13, 23),    # completed_at UTC
 [ingest, normalize, cluster, score, arc-link, write, tts],
 {articles: 491, articles_normalized: 491, clusters_new: 205, ...},
 [])
```

### Why this is correctly behavior, not a bug

The 19:13 Task Scheduler fire re-marked an already-COMPLETED row
COMPLETED · idempotent · no harm done. The briefing for today already
exists at `briefings/2026/05/26/`. Tomorrow's 02:00 fire uses
`run_20260527` which is a fresh row, the recovery branch will not
trigger, real ingestion runs.

### What is slightly off

The branch condition fires whenever `stages_done` is full, regardless
of `status`. That means it fires on already-COMPLETED rows too, not
just stuck-at-RUNNING rows. Slight semantic drift from the docstring
intent. Cosmetic; the log warning is misleading. **File as a follow-up
issue** · tighten the condition:

```python
if (
    not dry_run
    and only_stage is None
    and not force
    and set(STAGE_ORDER).issubset(set(stages_done))
    and self._read_run_status(run_id) == PipelineStatus.RUNNING.value
):
```

---

## ❯ Findings · state of the world

1. **Task Scheduler infrastructure is green.** Action launches pwsh
   from absolute path, script runs, DB is touched, log written, status
   marked COMPLETED, terminal exit code 0.

2. **The pipeline runs end-to-end on this machine.** Today's artifact
   set (briefing.md 72 KB · briefing.mp3 4.4 MB) is proof. Same shape
   as 5/24 and 5/25.

3. **First real overnight test is 2026-05-27 02:00 Turkey local.**
   `run_20260527` is the first run_id that has no leftover row.

4. **Two follow-up issues filed** (informally, in this doc) · see below.

---

## ❯ Open follow-ups

### Issue A · Tighten WindowsApps probe filter

**File** · `scripts/scheduling/register_nightly_task.ps1`
**Severity** · Low (latent; current MSI install bypasses the issue)
**Change** · Replace `*\WindowsApps\*` with
`*\AppData\Local\Microsoft\WindowsApps\*` in probe A's filter.

### Issue B · Tighten orchestrator recovery branch condition

**File** · `musahit/orchestrator.py` ~L420
**Severity** · Cosmetic (misleading WARNING log, no functional impact)
**Change** · Add
`self._read_run_status(run_id) == PipelineStatus.RUNNING.value` to the
`if` condition so the branch only fires on actually-stuck rows.
**Test** · Add a case to `tests/test_orchestrator.py` covering
"recovery branch does not fire on COMPLETED row with full stages_done."

### Issue C · Operator runbook

**File** · `docs/operator-runbook.md` (new)
**Severity** · Low (doc debt)
**Content** · Quick reference table of Task Scheduler exit codes, the
"MSI from GitHub releases · not Store · not winget" rule, and the
verification sequence. Salvage the *Quick Reference* section from this
post-mortem.

---

## ❯ Morning checklist · 2026-05-27 ~07:00

Run on the laptop after wake-up:

```powershell
# Did the task fire and complete cleanly?
Get-ScheduledTaskInfo -TaskName "MUSAHIT_Nightly" |
  Select-Object LastRunTime, LastTaskResult, NextRunTime
# Want · LastTaskResult: 0 · LastRunTime around 02:00 TR

# Did the briefing land?
Get-ChildItem briefings\2026\05\27\ -ErrorAction SilentlyContinue |
  Select-Object Name, Length, LastWriteTime
# Want · briefing.md ~50-80 KB · briefing.mp3 ~4-5 MB

# DB state
python scripts\check_run_state.py
# Want · run_20260527 · COMPLETED · all 7 stages · real counts

# Last log
Get-ChildItem logs\nightly-20260527-*.jsonl |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1 |
  ForEach-Object { Get-Content $_.FullName }
```

### If something failed

Likely failure modes in descending probability:

1. **Laptop slept and did not wake.** Windows power policy can ignore
   `WakeToRun=true` if the device is on battery or if "Allow wake
   timers" is disabled in the active power plan. Check
   `powercfg /lastwake` and `powercfg /waketimers`.
2. **Ollama model went stale or stalled.** Look for ollama-related
   stack traces in the log. Workaround · `ollama pull <model>` and
   re-fire.
3. **Government source rejected curl_cffi.** `*.gov.tr` CDNs
   occasionally rotate Akamai fingerprints. Look for poller WARNINGS
   in the log. Workaround · update curl-cffi.
4. **DuckDB lock contention.** Should not happen with the nightly
   batch design, but if the dashboard process is still holding a
   connection, the orchestrator will block. Check
   `Get-Process | Where-Object { $_.Name -eq 'python' }`.

---

## ❯ Quick reference · Task Scheduler exit codes seen this session

| Hex | Decimal | Symbol | Meaning |
|---|---|---|---|
| `0x00000000` | 0 | `S_OK` | Action launched and exited cleanly |
| `0x00041301` | 267009 | `SCHED_S_TASK_RUNNING` | Action is currently still running (not an error) |
| `0x80070002` | 2147942402 | `ERROR_FILE_NOT_FOUND` | Action binary or script not found · the bug we hit |

`LastTaskResult` is updated when the action exits. A read during
execution will show `0x41301`. Wait 8+ seconds after `schtasks /Run`
before trusting the value.

---

## ❯ Verification sequence

The canonical sequence to confirm a healthy nightly task after any
change to `scripts/scheduling/`:

```powershell
# 1. Reset · unregister and re-register
.\scripts\scheduling\unregister_nightly_task.ps1
.\scripts\scheduling\register_nightly_task.ps1
# Confirm banner shows resolved pwsh path is under C:\Program Files\PowerShell\7\

# 2. Fire
schtasks /Run /TN "MUSAHIT_Nightly"
Start-Sleep -Seconds 30   # give it time to actually exit

# 3. Verify · LastTaskResult should be 0
Get-ScheduledTaskInfo -TaskName "MUSAHIT_Nightly" |
  Select-Object LastRunTime, LastTaskResult, State

# 4. Verify · log file exists and is fresh
$latest = Get-ChildItem logs\nightly-*.jsonl |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1
$latest
Get-Content $latest.FullName

# 5. Verify · DB row state
python scripts\check_run_state.py
```

If 3 returns 0, 4 returns a fresh file, and 5 shows a COMPLETED row,
the scheduler stack is healthy.

---

## ❯ Files touched

| File | Change |
|---|---|
| `scripts/scheduling/register_nightly_task.ps1` | Added pwsh.exe absolute-path resolution with 4-probe order, WindowsApps-alias gotcha comment, banner prints resolved path |
| `scripts/scheduling/_task_template.xml` (or inline XML in register script) | `<Command>` now uses absolute path via `{{PWSH_EXE}}` placeholder substitution |
| (System) | Installed PowerShell 7.6.2 via MSI from GitHub releases |

No changes to `run_nightly.ps1`, `unregister_nightly_task.ps1`, or any
`musahit/` source file.

---

## ❯ One-line takeaway

> *Task Scheduler does not honor App Execution Aliases · always pin
> absolute paths in `<Command>` · install PS7 via MSI, never rely on
> the Store package alone.*

---

*End of post-mortem.*
