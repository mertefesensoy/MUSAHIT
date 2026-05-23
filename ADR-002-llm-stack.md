# ADR-002 · LLM stack

**Status** · Accepted · 2026-05-22
**Author** · Mert Efe Şensoy
**Supersedes** · none
**Cross-references** · ADR-001 · ADR-005 · ADR-009

> **Amended** · 2026-05-23 · writer model version updated to
> Trendyol-LLM 7B chat v1.8 (the latest publicly available Trendyol
> release as of May 2026) · pulled directly from Ollama Hub via
> `serkandyck/trendyol-llm-7b-chat-v1.8-gguf` · no manual GGUF download
> or Modelfile import needed · the original ADR-002 specification of
> v4 was based on an assumption that did not hold

---

## ❯ Context

MÜŞAHİT must run all language model inference locally on a Windows laptop with iGPU only.
This rules out anything above ~9B parameters at reasonable Q4 quantization. Turkish output
quality is critical: the final briefing is consumed by a Turkish reader and a Turkish TTS
engine. Multilingual capability is also critical for ingestion: roughly 25% of sources are
international (DW Türkçe excluded · BBC/Reuters/AP/Bloomberg/Euronews English where Turkish
versions are stale).

## ❯ Decision

A **two-model pipeline** served by **Ollama**, switched in and out as the pipeline moves
between stages.

### Worker model · Qwen2.5 7B Instruct

- **Role** · cluster classification · DEFCON scoring · entity extraction · summarization
  per cluster
- **Quantization** · Q4_K_M · ~4.5 GB on disk · ~5.5 GB resident
- **Why** · best multilingual model under 10B as of 2026 · reliable structured-output
  generation when prompted with JSON schema · strong Turkish reading comprehension though
  not native phrasing
- **Invocation** · 100-500 calls per night · structured prompts demanding strict JSON ·
  temperature 0.1 · max output 512 tokens

### Writer model · Trendyol-LLM 7B chat v1.8

- **Role** · final Turkish briefing prose generation · called once per night
- **Quantization** · Q4_K_M · pulled directly from Ollama Hub: `serkandyck/trendyol-llm-7b-chat-v1.8-gguf`
- **Why** · trained on Turkish corpora · native Turkish phrasing · the briefing reads like
  it was written by a Turkish speaker · the writer model needs to produce idiomatic prose
  the operator will read every morning
- **Invocation** · one call per night with the full day's scored clusters + arc updates ·
  temperature 0.3 · max output 4096 tokens

### Embedding model · bge-m3

- **Role** · embedding for clustering, deduplication, and arc-linking
- **Why** · multilingual including Turkish · 568M params · CPU-tractable · runs via Ollama
- **Invocation** · once per article and once per arc head · ~500-1000 embeddings per night

### Model lifecycle

```
01:00  ingest         no LLM
02:00  normalize      no LLM
02:30  embed+cluster  load bge-m3 · embed all articles · unload bge-m3
03:30  score+classify load qwen2.5 · score all clusters · keep loaded
04:30  arc linking    load bge-m3 alongside qwen2.5 if RAM permits ·
                     else unload qwen2.5 · load bge-m3 · embed arc heads ·
                     unload bge-m3 · reload qwen2.5 for any final scoring
05:00  writer pass    unload qwen2.5 · load trendyol-llm · generate briefing ·
                     unload trendyol-llm
```

Ollama handles model loading and the `keep_alive` parameter controls retention. Pipeline
code passes `keep_alive=0` to force unload between stage boundaries.

### Prompt discipline

- **Worker** · all prompts ask for JSON output with a strict schema · the pipeline
  validates and retries up to 2 times if JSON parsing fails · third failure logs the
  cluster as `unclassified` and continues
- **Writer** · the writer prompt contains the bias-handling rule · "report facts with
  attribution · where two bands frame an event differently, present both framings with
  KAYNAK·BAND tags · never editorialize · write in formal Turkish (resmi Türkçe)"
- **No streaming** · this is a batch pipeline · full responses are awaited before parsing

### Installation

Models are pulled by `scripts/pull_models.ps1`. The script must complete before first run.

```powershell
ollama pull qwen2.5:7b-instruct-q4_K_M
ollama pull bge-m3:latest
ollama pull serkandyck/trendyol-llm-7b-chat-v1.8-gguf
```

All three models pull directly from Ollama Hub. No HuggingFace GGUF download, no
Modelfile authoring required for any of them.

## ❯ Consequences

**Positive**
- Final briefing reads in idiomatic Turkish · operator does not skim past clunky phrasing
- Worker model is the workhorse · its multilingual strength handles mixed-language source
  material
- bge-m3 is a quiet workhorse · multilingual embeddings without separate Turkish-only model
- Switching models incurs ~30 seconds per swap · acceptable in nightly batch

**Negative**
- Two models double the disk footprint to ~9-10 GB
- Trendyol-LLM is less battle-tested than Qwen · may need replacement if writer output
  drifts · ADR amendment path exists
- Model swap timing makes the pipeline schedule less elastic · if scoring overruns,
  writer stage compresses

## ❯ Alternatives considered

- **Single model for everything** · Qwen2.5 7B handles writing too · simpler stack · but
  Turkish prose quality is the daily-consumed surface · operator picked the two-model
  pipeline for prose quality
- **Cosmos LLaMa 8B (Koç Üniversitesi)** · also Turkish-tuned · less battle-tested than
  Trendyol · slightly larger · rejected as second-choice writer · kept as fallback
- **Llama 3.1 8B** · safer baseline · weaker Turkish than Qwen · rejected for worker role
- **Gemma 2 9B** · stronger but heavier · rejected because the worker is called hundreds
  of times per night and latency adds up
- **Mistral Nemo 12B** · too heavy for CPU inference at acceptable latency
- **Cloud APIs (Claude, GPT-4o)** · rejected · operator requirement is local

## ❯ Open questions

- Trendyol-LLM v1.8 is the current publicly-available version. If Trendyol publishes a
  newer public release (v2.x or beyond), the writer model string in code should be
  updated and this ADR amended again. Fallback per ADR-002 alternatives section remains
  Cosmos LLaMa (`ytu-ce-cosmos/Turkish-Llama-8b-Instruct-v0.1-GGUF`) if Trendyol prose
  quality proves insufficient.
- Quantization choice · Q4_K_M is the default · Q5_K_M is preferable if RAM allows · the
  install script attempts Q5 first and falls back to Q4
