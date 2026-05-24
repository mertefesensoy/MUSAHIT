# ADR-001 · Architecture overview

**Status** · Accepted · 2026-05-22
**Author** · Mert Efe Şensoy
**Supersedes** · none
**Cross-references** · ADR-002 · ADR-006 · ADR-007

---

## ❯ Context

MÜŞAHİT is a personal, single-operator OSINT pipeline that processes Turkish open-source
material overnight and produces a Turkish briefing at 07:00. The operator runs a Windows
laptop with iGPU only, has no dedicated server, and prefers locally-hosted LLMs over
cloud APIs for both privacy and cost reasons.

The processing must complete in a 6-hour window. Source volume is roughly 30 outlets,
producing an estimated 200-500 raw articles per night. The system must be fault-tolerant
per source: one broken RSS feed cannot bring down the run.

## ❯ Decision

MÜŞAHİT is a **batch pipeline** organized as **six sequential stages** running in a single
Python process. Each stage reads from DuckDB, processes, and writes back. Stages are
isolated such that a partial failure in stage N still allows stages N+1 onward to operate
on whatever was produced.

```
[ingest] → [normalize] → [cluster] → [score] → [arc-link] → [write]
                                                              └→ [tts]
                                                              └→ [dashboard]
```

### Stage definitions

1. **ingest** · poll all configured sources · store raw HTML/PDF/text into `raw_articles`
   with provenance · per-source timeout and retry · failures recorded in `ingest_log`

2. **normalize** · extract clean article text via `trafilatura` · run Turkish entity
   tagging (people · parties · ministries · companies · tickers) · detect language ·
   write to `articles`

3. **cluster** · compute `bge-m3` embeddings · cluster by cosine similarity within a
   24-hour window · cross-outlet deduplication · assign cluster IDs · tag each cluster
   with the union of bands its sources belong to

4. **score** · for each cluster · call Qwen2.5 7B with the cluster's headlines and lead
   paragraphs · classify (category) · score (DEFCON 0-5 raw) · extract entities · then
   apply promotion rules from ADR-005 to compute the final ceiling-adjusted DEFCON

5. **arc-link** · for each scored cluster · attempt to link to an existing OPEN or WATCH
   arc via embedding similarity + entity overlap (Jaccard ≥ 0.4) within a 30-day window ·
   create new arc if no match · update arc state per ADR-008

6. **write** · for the day's scored clusters and arc updates · invoke Trendyol-LLM 7B to
   produce the Turkish briefing per the template in ADR-009 · write `briefing.md` ·
   `briefing.html` · then invoke Piper for `briefing.mp3` per ADR-010

### Runtime characteristics

- **Single Python process** · `python -m musahit.pipeline` · entrypoint at `src/pipeline.py`
- **Stage checkpoints** · each stage marks completion in `pipeline_runs` table · the next
  stage reads only items marked ready
- **Resumable** · if the process crashes at stage 4, restarting picks up from stage 4
  without re-running stages 1-3
- **Concurrency** · within a stage, ingestion is concurrent via `asyncio` with per-source
  semaphores · clustering and scoring are CPU-bound and run serially to avoid Ollama
  contention
- **Single Ollama instance** · models are pre-loaded · only one model held in memory at a
  time · worker model unloaded before writer model loads to fit in available RAM

### Hardware envelope

- iGPU only · CPU inference for all models
- Estimated peak RAM with worker model loaded · ~7 GB
- Estimated peak RAM with writer model loaded · ~7 GB
- Disk · ~20 GB for models · ~5 GB/year for archived articles and briefings
- Network · ~500 MB/night for source fetching

## ❯ Consequences

**Positive**
- Single-process simplicity · no orchestrator dependency · no message queue · no Docker
- Resumability built in via stage checkpoints
- Per-source isolation prevents cascading failures
- The whole pipeline can be debugged by replaying a single night from raw_articles

**Negative**
- Sequential stages mean the slowest stage gates the next · careful timing required
- Single Python process means a crash in shared infrastructure (DB connection) kills the
  whole night · mitigated by liveness probe per ADR-007
- Two-model load/unload costs ~30 seconds each · acceptable in a 6-hour window

## ❯ Alternatives considered

- **Prefect or Dagster orchestrator** · rejected · overkill for single-operator nightly
  batch · adds dependency surface · operator's experience with GitHub Actions cron
  dropping ticks argued for the simplest possible scheduler stack
- **Multi-process workers with Redis queue** · rejected · adds Redis dependency · the
  6-hour window does not need parallelism beyond ingestion
- **Real-time streaming pipeline** · rejected · operator explicitly wants nightly batch ·
  daytime alerts are out of scope

## ❯ Open questions

- Stage timing may need tuning after the first week of real runs · the 01:00-07:00 budget
  is generous but the writer stage with Trendyol-LLM is the unknown · revisit in ADR-013
  if needed
