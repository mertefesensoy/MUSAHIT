# ADR-012 · Failure and retention

**Status** · Accepted · 2026-05-22
**Author** · Mert Efe Şensoy
**Supersedes** · none
**Cross-references** · ADR-001 · ADR-006 · ADR-007

---

## ❯ Context

MÜŞAHİT runs unattended overnight. Sources fail. Models occasionally produce malformed
output. Disks fill. Windows updates reboot machines. The briefing must still ship at
07:00 even when components fail, and the operator must have enough information at 07:00
to know what went wrong.

The retention policy must balance the operator's interest in historical archive (the
briefings themselves are forever) against disk pressure (raw HTML is bulky).

## ❯ Decision

**Per-source failure isolation** for ingest. **Best-effort completion with explicit
gap reporting** for the briefing. **Auditable failure logs** for the operator's morning
review. **Tiered retention** that keeps the operator-valuable artifacts forever and
prunes raw content after 90 days.

### Failure isolation by stage

#### Stage 1 · Ingest

Each source ingestion runs in an isolated task with timeout and exception handling:

```python
async def ingest_source(source: Source, run_id: str) -> IngestResult:
    started_at = utcnow()
    try:
        async with asyncio.timeout(source.timeout_seconds or 60):
            articles = await fetch(source)
        store_articles(articles)
        return IngestResult(status=OK, count=len(articles))
    except asyncio.TimeoutError:
        return IngestResult(status=TIMEOUT, count=0,
                            error="exceeded timeout")
    except httpx.HTTPStatusError as e:
        return IngestResult(status=HTTP_ERROR, count=0,
                            error=f"HTTP {e.response.status_code}")
    except Exception as e:
        return IngestResult(status=PARSE_ERROR, count=0,
                            error=f"{type(e).__name__}: {e}")
    finally:
        log_ingest(run_id, source.id, started_at, utcnow(), result)
```

A single source failure does not propagate. The ingest stage finishes with whatever it
got and writes the failures to `ingest_log`. The briefing footer lists failed sources.

#### Stage 2-5 · Normalize, cluster, score, arc-link

These stages process whatever ingest delivered. They have soft per-item error handling:

- A malformed article that breaks `trafilatura` is logged and skipped · the rest continue
- A cluster that fails LLM classification is logged with `category=UNCLASSIFIED` and
  `final_defcon=AMBIENT` (DEFCON 5) so it still appears, demoted
- An arc-link failure (e.g., embedding query times out) creates a new arc rather than
  crashing

The principle is **degrade gracefully · never abort**.

#### Stage 6 · Writer

The writer is the riskiest stage because Trendyol-LLM can produce malformed output. The
writer stage has explicit retry logic:

```python
def write_briefing(payload: BriefingPayload) -> str:
    for attempt in range(3):
        output = call_writer(payload)
        if validate_template(output):
            return output
        log_writer_retry(attempt, validation_errors)
    # All retries failed · use fallback template
    return render_fallback_briefing(payload)
```

The fallback template is a deterministic Python-rendered version that produces the same
structure with simpler prose (one-sentence summaries per item, no cross-band framing
discipline). It is functional · the operator gets a briefing · just not the polished
Trendyol prose.

#### Stage 7 · TTS

TTS failure is handled per ADR-010 · a silent placeholder mp3 is written and a banner
appears on the dashboard.

### Run-level failures

If the entire pipeline crashes before completing (e.g., process killed):

1. The crash exception is logged with full traceback to `logs/crash-YYYYMMDD-HHMMSS.log`
2. The `pipeline_runs` row for that run has `status=FAILED` and `completed_at=null`
3. Restarting the pipeline resumes from the last completed stage per the resumability
   design in ADR-007
4. If no resume happens (e.g., Windows didn't reboot in time), the 06:45 liveness probe
   detects the missing briefing and triggers the alert

### Liveness probe

Per ADR-007, the liveness probe runs at 06:45 and:

- Confirms today's briefing markdown exists
- Confirms today's `pipeline_runs.status` is `COMPLETED`

If either fails:

1. The probe writes a `failure_briefing.md` to today's briefings directory · a
   one-page summary explaining the failure mode with whatever data was successfully
   processed before the crash
2. The probe sends a Windows toast notification
3. The probe attempts an email send if SMTP is configured
4. The dashboard shows a `SİSTEM ARIZASI` banner

The `failure_briefing.md` includes:
- Last successful stage
- Counts of articles, clusters, arcs as of the last completed stage
- The list of sources that did ingest successfully (so the operator knows what *was*
  fetched even if not analyzed)
- The crash exception (truncated)

### Gap reporting in the normal briefing

Even when the pipeline completes successfully, gaps may exist. The briefing's
`SİSTEM LOG` footer always reports:

- Failed sources with the failure type
- Number of articles skipped during normalization
- Number of clusters that ended up `UNCLASSIFIED`
- Writer retry attempts

This is the operator's daily check that the system is healthy.

### Failure email format (when SMTP configured)

```
Subject · MÜŞAHİT LIVENESS · briefing missing for 2026-05-22

Briefing artifact at briefings/2026/05/22/briefing.md was not found at 06:45.

Pipeline run · run_20260522
Status        · FAILED
Last stage    · score
Started at    · 2026-05-22 01:00:12
Last update   · 2026-05-22 03:47:51

Stages completed:
  ingest    · ok · 28/30 sources
  normalize · ok · 412 articles
  cluster   · ok · 94 clusters
  score     · FAILED · ollama process died at cluster 47

Logs · C:\Users\<user>\musahit\logs\crash-20260522-034751.log

Resume command:
  python -m musahit.pipeline run --date 2026-05-22 --resume
```

### Retention policy

The system stores data at different tiers with different lifespans.

| Data | Location | Retention | Notes |
|---|---|---|---|
| Briefings (md, html, mp3) | `briefings/YYYY/MM/DD/` | Forever | The operator's archive |
| Cluster records | DuckDB | Forever | Severity history matters |
| Arc records | DuckDB | Forever | Story continuity is the point |
| Articles (normalized) | DuckDB | 1 year | Lead + entities · ~5 KB/article |
| Raw articles (HTML/PDF) | DuckDB BLOB | 90 days | Bulky · pruned monthly |
| Embeddings | DuckDB | 1 year | Matched to articles retention |
| Pipeline runs | DuckDB | Forever | Operational history |
| Ingest log | DuckDB | 1 year | Source reliability tracking |
| Promotion log | DuckDB | Forever | Audit trail · small footprint |
| Manual overrides | DuckDB | Forever | Operator history |
| Logs (`logs/*.jsonl`) | Filesystem | 90 days | Daily rotated |
| Backups | `data/backups/` | 30 days | Daily DuckDB copy |
| Crash logs | `logs/crash-*.log` | Forever | Rare events worth keeping |

### Retention enforcement

A `pruning` task runs once a week (Sunday at 12:00) and:

- Drops `raw_articles` older than 90 days
- Drops `articles` older than 1 year (their entities and headlines survive in clusters)
- Drops `article_embeddings` older than 1 year
- Deletes `logs/*.jsonl` older than 90 days
- Deletes `data/backups/musahit-*.duckdb` older than 30 days

Each prune is logged.

### Backup policy

After every successful pipeline run, `data/musahit.duckdb` is copied to
`data/backups/musahit-YYYYMMDD.duckdb`. The 30-day rolling window means at any moment
the operator has 30 daily snapshots.

The operator owns geographic redundancy. The system does not automatically push backups
off-machine. If the operator wants offsite backup, they configure a `scripts/offsite.ps1`
hook (suggestion: rclone to a personal cloud drive) but this is out of scope for v0.1.

### Disk pressure

The pipeline checks free disk before starting:

```python
def precheck_disk(min_gb: int = 5) -> None:
    free = shutil.disk_usage("data").free / (1024 ** 3)
    if free < min_gb:
        raise DiskPressureError(f"only {free:.1f} GB free, need {min_gb}")
```

If disk is insufficient, the pipeline aborts immediately and the liveness probe handles
the failure path. The dashboard shows a `DİSK DOLDU` banner.

### Override audit

Every manual override the operator performs via the dashboard is logged with:

- Action (PROMOTE · DEMOTE · RESOLVE · MERGE · SPLIT · DISMISS · RENAME)
- Target (cluster_id or arc_id)
- Old value
- New value
- Operator-provided reason (optional, prompted but not required)
- Timestamp

The override log is auditable from the dashboard's system view. This protects the
operator's ability to understand why something is in its current state weeks later.

## ❯ Consequences

**Positive**
- The briefing always ships · partial outputs are honest about their gaps
- Per-source isolation means MÜŞAHİT survives the inevitable RSS feed deaths
- Tiered retention keeps the operator's valuable artifacts forever while controlling
  disk pressure
- Audit trails for promotion decisions and operator overrides make the system honest
  about its editorial choices

**Negative**
- Fallback writer template is utilitarian · the briefing is uglier when Trendyol-LLM
  fails · acceptable as a degraded mode
- 90-day raw HTML retention means re-running the pipeline on older data has reduced
  fidelity · the normalized articles still exist for 1 year so most replay is possible
- Pruning is a fixed weekly schedule · if the operator wants to keep specific raw
  articles longer, they must manually export them

## ❯ Alternatives considered

- **Abort on first source failure** · rejected · operator wants the briefing even with
  gaps · partial signal is more valuable than no signal
- **Keep raw HTML forever** · rejected · DB grows unbounded · 90 days is the threshold
  past which the operator hasn't needed raw HTML in practice for similar projects
- **External backup automation** · rejected for v0.1 · adds complexity · operator can
  add a hook later

## ❯ Open questions

- 90-day raw HTML window may need adjustment if operator wants to re-analyze older
  events · adjustable in `config.toml` · revisit after 6 months
- Whether the failure_briefing.md should be voiced by Piper · currently it is not ·
  consider adding if liveness alerts happen often enough to matter
