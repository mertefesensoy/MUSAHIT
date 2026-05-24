# ADR-007 · Scheduler and liveness

**Status** · Accepted · 2026-05-22
**Author** · Mert Efe Şensoy
**Supersedes** · none
**Cross-references** · ADR-001 · ADR-012

---

## ❯ Context

MÜŞAHİT runs nightly between 01:00 and 07:00 on a Windows laptop. The operator's prior
experience with GitHub Actions cron in SuperconducTED produced ~60% missed ticks, which
made scheduler reliability a known concern.

The Windows laptop has unique failure modes: lid-close suspending the system, screen
sleep interfering with running processes, USB power management cutting peripherals,
Windows Update reboots, and battery-saver modes throttling CPU. Each of these can break
a nightly run.

## ❯ Decision

**Windows Task Scheduler** as the primary scheduler, with explicit configuration for
reliability on a laptop. A **liveness probe** runs at 06:45 and alerts the operator if
the day's briefing artifact does not exist.

### Task Scheduler configuration

A single task `MUSAHIT_NIGHTLY` is registered. Defined in `scripts/task_scheduler.xml`,
importable via:

```powershell
schtasks /Create /TN "MUSAHIT_NIGHTLY" /XML scripts\task_scheduler.xml
```

Key settings:

- **Trigger** · Daily at 01:00
- **Wake the computer to run this task** · ENABLED
- **Run only when user is logged on** · DISABLED · runs as SYSTEM
- **Run with highest privileges** · ENABLED · for Ollama process priority
- **Start the task only if the computer is on AC power** · ENABLED (laptop must be
  plugged in for nightly operation · runs are skipped on battery)
- **Stop if the computer switches to battery power** · ENABLED · safety against ungraceful
  shutdown
- **If the task fails, restart every** · 10 minutes, up to 3 attempts
- **If the running task does not end when requested, force it to stop** · ENABLED
- **Stop the task if it runs longer than** · 6 hours · hard ceiling

Action:

```
Program/script  C:\Python311\python.exe
Arguments       -m musahit.pipeline run --date today
Start in        C:\Users\<operator>\musahit
```

### Liveness probe configuration

A second task `MUSAHIT_LIVENESS` runs at 06:45 daily:

```
Program/script  C:\Python311\python.exe
Arguments       -m musahit.liveness check --date today
Start in        C:\Users\<operator>\musahit
```

The liveness probe:

1. Checks `briefings/YYYY/MM/DD/briefing.md` exists and is non-empty
2. Checks `pipeline_runs` table has status `COMPLETED` for today's `run_id`
3. If either check fails · writes a fallback briefing stub explaining the failure ·
   triggers an alert (see below)
4. If both pass · no action

### Failure alert path

When the liveness probe detects failure, it sends an alert via the most reliable channel
that works in a degraded state. Order of preference:

1. **Local notification** · Windows toast notification (built-in, no dependency, works
   offline) · "MÜŞAHİT · briefing failed at 06:45 · check logs"
2. **Email fallback** · if SMTP credentials are configured in `.env`, send to operator's
   email · subject `MÜŞAHİT LIVENESS · briefing missing for YYYY-MM-DD`
3. **Dashboard banner** · the dashboard at 07:00 shows a `SİSTEM ARIZASI` banner if the
   liveness check failed

Email is optional. If it's not configured, the toast is the alert mechanism.

### Windows power plan setup

The install script `scripts/install_windows.ps1` sets up the power plan:

```powershell
# Set to High Performance plan
powercfg /setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c

# Disable sleep on AC
powercfg /change standby-timeout-ac 0

# Disable hibernate on AC
powercfg /change hibernate-timeout-ac 0

# Disable display sleep on AC
powercfg /change monitor-timeout-ac 0

# Disable USB selective suspend
powercfg /setacvalueindex SCHEME_CURRENT SUB_USB USBSELECTIVE 0

# Allow wake timers
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP ALLOWWAKETIMERS 1

# Apply
powercfg /setactive SCHEME_CURRENT
```

These settings apply on AC only · battery behavior is unchanged so the laptop can still
sleep normally when used as a laptop.

### Lid-close behavior

This is the laptop's primary failure mode. The install script also configures the lid to
NOT trigger sleep on AC:

```powershell
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
```

If the operator wants standard lid behavior during the day (use as laptop normally),
this setting prevents that. The current decision is to override · the laptop is treated
as a dedicated MÜŞAHİT host once docked at the operator's workstation. If the operator
needs lid-sleep for travel, they suspend the Task Scheduler entries.

### Pipeline timing budget

```
01:00  ingest start              (target completion 02:00 · 60 min)
02:00  normalize + entity tag    (target 02:30 · 30 min)
02:30  embed + cluster           (target 03:30 · 60 min)
03:30  score + classify          (target 04:30 · 60 min)
04:30  arc linking               (target 05:00 · 30 min)
05:00  writer pass               (target 06:00 · 60 min)
06:00  voice synthesis           (target 06:30 · 30 min)
06:30  artifact lock + backup    (target 06:45 · 15 min)
06:45  liveness probe runs
07:00  delivered to operator
```

Each stage has a soft deadline. If a stage runs over, the next stage starts late but
the pipeline does not abort. A stage running 2x over its budget logs a `STAGE_SLOW`
event and the operator reviews timing the next morning.

### Resumability

If the pipeline process is killed mid-run (Windows Update reboot, power loss, manual
intervention), restarting `python -m musahit.pipeline run --date today` resumes from the
last completed stage. The `pipeline_runs.stages_done` JSON tracks which stages have
finished.

### Manual operation

The operator can also run the pipeline manually for debugging:

```powershell
# Full run for today
python -m musahit.pipeline run --date today

# Single stage
python -m musahit.pipeline run --date today --stage cluster

# Backfill a missed day
python -m musahit.pipeline run --date 2026-05-20

# Dry run · no LLM calls · uses stub responses
python -m musahit.pipeline run --date today --dry-run
```

## ❯ Consequences

**Positive**
- Task Scheduler is the most reliable scheduler on Windows · `wake the computer` works
  reliably from S3 sleep · battery-power restriction prevents half-runs on low battery
- Liveness probe gives a 15-minute window between intended completion (06:30) and
  delivery (07:00) for the operator to notice failures before reading the briefing
- Resumability handles Windows Update reboots gracefully

**Negative**
- Laptop must stay plugged in · battery operation is incompatible with nightly runs
- Lid-close override means the laptop is not a portable laptop while configured this way
- Windows Update can reboot the machine mid-run · the resumability path handles this but
  the briefing is late · revisit if it happens more than once a month
- Task Scheduler XML must be importable cleanly · file is checked into the repo

## ❯ Alternatives considered

- **WSL cron** · rejected · WSL2 sleeps when laptop sleeps · less reliable than Windows
  Task Scheduler
- **Long-running Python service via NSSM** · viable alternative · single service that
  internally schedules · adds NSSM dependency · rejected for v0.1 simplicity · revisit
  if Task Scheduler proves unreliable
- **systemd in WSL** · viable but ties to WSL availability · rejected
- **APScheduler in a long-running process** · rejected · process must be kept alive ·
  one more thing to monitor

## ❯ Open questions

- Windows Update can reboot at unpredictable times · the operator should set Active Hours
  to cover 00:00-08:00 · this is in the install script but Windows can still override ·
  monitor and adjust
- If the operator travels with the laptop, the configuration must be temporarily disabled ·
  consider a `scripts/travel_mode.ps1` toggle script
