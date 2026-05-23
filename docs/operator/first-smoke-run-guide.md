# First smoke run · operator guide

**Audience** · the operator running MÜŞAHİT end-to-end for the first time
**Goal** · validate that all 7 stages work against real Turkish sources,
real Ollama models, and real Piper TTS · and capture the inevitable
first-run findings into `memory/operator-tasks.md`

This is the **step 16 prep**; the run itself is operator-driven. There
is no automated CI for the first smoke · the operator is the system
under test.

---

## ❯ TL;DR

```powershell
.\scripts\run_first_smoke.ps1
```

That command runs the five pre-flight checks, then invokes
`python -m musahit.pipeline run --date today` with a tee-captured log.
On completion it prints summary and artifact paths; on failure it
prints the structured-log tail and suggests diagnostics.

Expect **5-15 issues** to surface during the first run. That is normal;
the pipeline is composed of seven stages that each have soft
expectations about source registries, LLM behavior, and selectors that
real-world data will violate. File each finding to
`memory/operator-tasks.md` as it surfaces.

---

## ❯ Pre-flight checklist

The smoke script automates the five hard checks. The items below are
operator-side prep work that the script cannot verify · make sure
they're done before running.

### URLs and selectors

- [ ] **7 RSS URLs pending operator verification** (see step 3 notes in
  `memory/build-progress.md`):
  - `anadolu`, `t24`, `medyascope`, `dw_tr`, `voa_tr`, `reuters_tr`, `kap`
  - All currently use placeholder URLs in `musahit/ingest/sources.py`
  - Verify each by opening the feed in a browser · if it 404s or
    looks wrong, file to `operator-tasks.md` under Pending
- [ ] **9 HTML selector entries are first-pass placeholders** (step 5):
  - `ap_tr`, `tcmb`, `bist`, `tuik`, `tbmm`, `cumhurbaskanligi`,
    `anayasa_mahkemesi`, `yargitay`, `danistay`
  - Selectors in `musahit/ingest/html_selectors.py` · most will fail
    on first run; that's expected
  - Failures land in `ingest_log` as `PARSE_ERROR` and the day's
    briefing reports them in SİSTEM LOG · footer

### Models

- [ ] `ollama list` shows all three required models:
  - `qwen2.5:7b-instruct-q4_K_M` · score stage
  - `serkandyck/trendyol-llm-7b-chat-v1.8-gguf` · write stage
  - `bge-m3` · cluster stage
- [ ] Pull missing models:

  ```powershell
  ollama pull qwen2.5:7b-instruct-q4_K_M
  ollama pull serkandyck/trendyol-llm-7b-chat-v1.8-gguf
  ollama pull bge-m3
  ```

- [ ] `ollama serve` is running in another terminal (or as a service)

### Piper voice

- [ ] ONNX file exists at
  `C:\Users\senso\AppData\Local\piper\voices\tr_TR-dfki-medium.onnx`
- [ ] If missing, follow ADR-010 § Installation:

  ```powershell
  $voiceDir = "$env:LOCALAPPDATA\piper\voices"
  New-Item -ItemType Directory -Force -Path $voiceDir
  Invoke-WebRequest -Uri "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/tr/tr_TR/dfki/medium/tr_TR-dfki-medium.onnx" -OutFile "$voiceDir\tr_TR-dfki-medium.onnx"
  Invoke-WebRequest -Uri "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/tr/tr_TR/dfki/medium/tr_TR-dfki-medium.onnx.json" -OutFile "$voiceDir\tr_TR-dfki-medium.onnx.json"
  ```

### ffmpeg

- [ ] `ffmpeg -version` works in the PowerShell session that will
  run the smoke (the TTS stage uses pydub which shells out to ffmpeg)
- [ ] If missing: `winget install Gyan.FFmpeg` or equivalent

### Reddit credentials (optional)

- [ ] `.env` has `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` if the
  Reddit source should be ingested; otherwise it will land as
  `SKIPPED` in `ingest_log` and the pipeline continues without it

### Disk space

- [ ] At least 5 GB free on the drive holding `data/` (matches
  `Settings.min_free_disk_gb`)

### DuckDB schema

- [ ] `python scripts/init_db.py` has been run once to create
  `data/musahit.duckdb`
- [ ] Current migration version is **3** (initial schema + article
  metadata + failed_stages column from step 15). The smoke script
  pre-flight checks this; rerun `scripts/init_db.py` if it's wrong.

---

## ❯ Expected timing (rough)

These are **guesses** based on ADR-007's soft budgets. The operator
will refine after a few real runs. Per the orchestrator's design,
each stage has a hard timeout at **2× the soft budget**; a stage
exceeding that is aborted and logged as a soft failure (the next
stage still runs).

| Stage      | Soft budget (ADR-007) | First-run guess           |
|------------|-----------------------|---------------------------|
| ingest     | 60 min                | 30 · 60 min               |
| normalize  | 30 min                | 5 · 10 min                |
| cluster    | 60 min                | 10 · 20 min               |
| score      | 60 min                | 30 · 90 min               |
| arc-link   | 30 min                | 5 · 10 min                |
| write      | 60 min                | 15 · 30 min               |
| tts        | 30 min                | 5 · 10 min                |
| **total**  | **5h 30min cap**      | **~1h 40min · 4h 30min**  |

The first run's score / write are the unknowns · Qwen2.5 and
Trendyol-LLM on this laptop have never been measured against real
clusters. If either runs past its hard timeout (2× soft), it's
recorded as a soft failure but the pipeline continues.

Document actuals in `operator-tasks.md` under "First-month tuning":
once 7-14 nights of data exist, ADR-007 can be amended with measured
times.

---

## ❯ What to expect from the first run

A clean COMPLETED status is **unlikely on the first run**. The
expected mode is:

- **Some sources fail** · placeholder URLs 404, placeholder selectors
  miss the article body, an outlet's RSS feed format is unfamiliar
- **The briefing still ships** · per ADR-012's always-ships invariant,
  the failures land in `ingest_log` and the briefing's SİSTEM LOG
  footer lists them
- **Trendyol-LLM may need a retry** · first prompt evaluation on real
  data sometimes produces malformed sections; the writer's
  3-retry-then-fallback loop catches this
- **TTS may use the silent placeholder** if ffmpeg isn't on PATH or
  the chunking surfaces an edge case

**This is not a failure mode.** It's the system telling you which of
the 5-15 first-run findings to address first. File each one to
`memory/operator-tasks.md` under Pending or First-month tuning.

---

## ❯ How to inspect results after the run

### 1. The briefing itself

```powershell
# Markdown briefing (what the writer produced)
notepad briefings\<yyyy>\<mm>\<dd>\briefing.md

# Audio briefing (what Piper synthesized)
explorer briefings\<yyyy>\<mm>\<dd>\briefing.mp3
```

If `briefing.mp3` is exactly 44.1 KB and plays silence, the TTS stage
fell back to its silent placeholder · check stderr / log for the
underlying error.

### 2. Pipeline status

```powershell
python -m musahit.pipeline status --date today
```

Prints the `pipeline_runs` row: `run_id`, `started_at`, `completed_at`,
`status`, `stages_done`, `counts`, `failed_stages`.

### 3. Quick DB checks

```powershell
python -c "
import duckdb
conn = duckdb.connect('data/musahit.duckdb')

# Per-stage ingest counts.
print('=== ingest_log ===')
for row in conn.execute(
    'SELECT status, COUNT(*) FROM ingest_log GROUP BY status'
).fetchall():
    print(f'  {row[0]:12s} : {row[1]}')

# Articles ingested vs normalized.
print('=== article counts ===')
raw   = conn.execute('SELECT COUNT(*) FROM raw_articles').fetchone()[0]
norm  = conn.execute('SELECT COUNT(*) FROM articles').fetchone()[0]
print(f'  raw_articles : {raw}')
print(f'  articles     : {norm}')

# Cluster + DEFCON distribution.
print('=== cluster final_defcon distribution ===')
for row in conn.execute(
    'SELECT final_defcon, COUNT(*) FROM clusters GROUP BY final_defcon ORDER BY final_defcon'
).fetchall():
    print(f'  DEFCON {row[0]} : {row[1]}')

# Arc states.
print('=== arc states ===')
for row in conn.execute(
    'SELECT state, COUNT(*) FROM arcs GROUP BY state'
).fetchall():
    print(f'  {row[0]:10s} : {row[1]}')

# Failures.
print('=== failed_stages ===')
print(conn.execute(
    'SELECT failed_stages FROM pipeline_runs ORDER BY started_at DESC LIMIT 1'
).fetchone()[0])
"
```

What to look at:

- **ingest_log distribution** · how many sources succeeded vs failed.
  A few PARSE_ERROR / HTTP_ERROR is normal; widespread failure usually
  means a misconfigured `sources.py` entry or Ollama down.
- **raw_articles vs articles** · should be very close. A big gap
  means the normalize stage is dropping rows; check the log for
  `normalize_skip` events.
- **final_defcon distribution** · most clusters should land at
  AMBIENT (5) or GÜNDEM (4); a few at MATERIAL (3); rarely SEVERE (2)
  or below. If most clusters land at SEVERE, the score prompt or
  promotion rules likely need attention.
- **arc states** · should be mostly OPEN on day 1. WATCH appears on
  day 8, RESOLVED on day 38.
- **failed_stages** · exact failures the orchestrator caught and
  isolated. Each entry is a candidate for `operator-tasks.md`.

### 4. Structured log

The smoke script captures the run's stdout to `logs/smoke-<timestamp>.jsonl`
as JSON lines. Useful greps:

```powershell
# All stage starts and completions
Select-String -Path logs\smoke-*.jsonl -Pattern 'stage_complete|stage_failed|stage_slow'

# Tracebacks
Select-String -Path logs\smoke-*.jsonl -Pattern 'Traceback'

# Slow stages (over their soft budget)
Select-String -Path logs\smoke-*.jsonl -Pattern 'stage_slow'

# Specific stage's events
Select-String -Path logs\smoke-*.jsonl -Pattern '"stage": "score"'
```

---

## ❯ How to retry

The orchestrator is resumable per ADR-007. Three retry shapes:

### Resume from the last completed stage

```powershell
python -m musahit.pipeline resume --date today
```

The orchestrator reads `pipeline_runs.stages_done` and skips anything
already in it. Use this when a mid-pipeline failure crashed the
process or the operator manually killed it.

### Force a full re-run

```powershell
python -m musahit.pipeline run --date today --force
```

`--force` ignores `stages_done` and re-runs every stage. Note that
re-running an idempotent stage is safe (everything uses `INSERT OR
IGNORE` / `INSERT OR REPLACE` patterns) but expensive · re-runs the
LLM calls.

### Re-run a single stage

```powershell
python -m musahit.pipeline run --date today --stage cluster
```

`--stage NAME` filters to just that stage. Useful when fixing a
narrow bug · e.g., a selector update affects normalize only.

### Backfill a missed day

```powershell
python -m musahit.pipeline run --date 2026-05-20
```

The orchestrator treats explicit dates the same as today; the
`run_id` is derived from the date and stages_done is per-run-id.

### Dry-run (no DB writes, no LLM)

```powershell
python -m musahit.pipeline run --date today --dry-run
```

Useful to verify the CLI plumbing or stage_factory dispatch without
touching the real models or DB. Does NOT touch `pipeline_runs`.

---

## ❯ Where to file findings

Every first-run finding goes to `memory/operator-tasks.md`:

- **Pending** · must address before step 17 starts (e.g., placeholder
  URL is wrong, a stage crashes with no useful diagnostic, the score
  prompt produces unparseable JSON for a specific category)
- **First-month tuning** · operational refinements that need run-history
  to inform (e.g., adjust the cluster cosine threshold from 0.7 → 0.65,
  add a 10th DEFCON category, tune Reddit min_score)
- **Resolved** · moved here once fixed, with the resolution date

Each entry should be short · one line of description, plus the
file / module name if known. Long context belongs in
`docs/implementations/` or a separate ADR amendment.

---

## ❯ When the first run is "done"

The smoke run isn't a binary pass / fail · it's the start of an
iterative process. The operator declares the first run complete when:

1. The pipeline reached COMPLETED status at least once (any number of
   soft-failed stages is fine)
2. A readable Turkish `briefing.md` was produced with the ADR-009
   structure
3. A playable `briefing.mp3` exists (silent placeholder is acceptable
   for the first run if ffmpeg isn't yet set up)
4. `memory/operator-tasks.md` has every surfaced issue filed
5. The operator has a feel for the actual per-stage timings (filed
   under First-month tuning)

Once all five hold, step 17 prep can begin.

---

## ❯ Related docs

- [ADR-001 · Architecture overview](../../ADR-001-architecture-overview.md) (the 7 stages)
- [ADR-007 · Scheduler and liveness](../../ADR-007-scheduler-and-liveness.md) (timing budgets, resumability)
- [ADR-012 · Failure and retention](../../ADR-012-failure-and-retention.md) (always-ships invariant)
- [docs/implementations/2026-05-23-pipeline.md](../implementations/2026-05-23-pipeline.md) (step 15 · orchestrator + CLI implementation)
- `memory/operator-tasks.md` (the findings backlog)
- `memory/build-progress.md` (build-step history)
