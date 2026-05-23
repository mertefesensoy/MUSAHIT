# Implementation: Normalize stage · `musahit/normalize/`

**Date** · 2026-05-23
**Author** · Claude Code (Mert Efe Şensoy directing)
**ADR refs** · ADR-001 · ADR-006 · ADR-014 · ADR-015

---

## ❯ Problem / Motivation

Build step 9 of 20 — the first non-ingest stage and the start of Phase 2
(processing). The pipeline now has ingested raw bytes; this stage reads
them, extracts title + body + metadata, and writes the canonical
`articles` table that every downstream stage (embed, cluster, score,
arc-link, write) consumes.

Three concerns drive the design:

1. **Four ingester types, four extractor shapes.** RSS already has its
   entry's body sitting in the row's `headers` JSON. HTML has full page
   bytes. PDF (Resmî Gazete) has the GazetteItem's text in `headers`.
   Reddit has a JSON payload in `raw_content`. A single uniform
   extractor would be lossy.
2. **The Ingester Protocol is the boundary.** Each ingester deposits a
   `raw_articles` row with a known shape per source.kind; this stage
   dispatches on that kind. No new Protocol; no schema change.
3. **Failure isolation per ADR-012.** A malformed PDF or a truncated
   feed entry must not abort the whole normalize stage. Per-row try/
   except wraps every extractor invocation; the rest of the queue still
   lands.

---

## ❯ What Changed

| File | Description |
|---|---|
| `musahit/normalize/__init__.py` | Package marker. |
| `musahit/normalize/normalizer.py` | `Normalizer` class, `ExtractedArticle` / `RawArticleRow` dataclasses, default dispatcher. |
| `musahit/normalize/language.py` | `detect_language()` wrapper around `langdetect` with the "<20 chars → unknown / langdetect failure → tr" policy. |
| `musahit/normalize/entities.py` | Rule-based `extract_entities()` over the curated vocabulary with Turkish-locale folding. |
| `musahit/normalize/entities_vocab.py` | `VOCABULARY` tuple — ~60 entries across PARTY / INSTITUTION / PERSON / COMPANY. |
| `musahit/normalize/extractors/html.py` | `extract_html_body()`: trafilatura primary, body_selector fallback. |
| `musahit/normalize/extractors/rss.py` | `extract_rss_body()`: reads `headers["body"]`, runs trafilatura on HTML markup. |
| `musahit/normalize/extractors/pdf.py` | `extract_pdf_body()`: reads `headers["body"]`, normalises whitespace, strips page-number artifacts. |
| `musahit/normalize/extractors/reddit.py` | `extract_reddit_body()`: parses raw_content JSON, flattens to `selftext` + Turkish-marked comments. |
| `musahit/ingest/rss.py` | One-line additive change: store `body` (content:encoded → description → summary) in headers JSON. |
| `musahit/ingest/resmi_gazete.py` | One-line additive change: store `item.body` in headers JSON. |
| `pyproject.toml` | Adds `langdetect>=1.0.9` to runtime deps. |
| `tests/test_normalize_language.py` | 5 tests. |
| `tests/test_normalize_entities.py` | 17 tests across 7 classes. |
| `tests/test_normalize_extractors.py` | 13 tests across 4 classes (one per extractor). |
| `tests/test_normalizer.py` | 11 tests across 6 classes — full integration. |
| `docs/implementations/2026-05-23-normalize.md` | This document. |

No FILE-PROTECTED file (`sources.py`, `defcon.py`, `promotion.py`,
`poller.py`, any ADR) was modified. trafilatura was already a project
dependency; only `langdetect` was added.

---

## ❯ Implementation Approach

### Dispatch pattern

The Normalizer reads `source.kind` (joined from the `sources` table by
`source_id`) and routes to one of four extractor functions:

| `source.kind` | Extractor | Input shape |
|---|---|---|
| `SourceKind.HTML` | `extract_html_body(raw_content, source_id, headers)` | full HTML bytes |
| `SourceKind.RSS` | `extract_rss_body(headers)` | per-entry metadata in JSON |
| `SourceKind.PDF` | `extract_pdf_body(headers)` | already-extracted Gazette text |
| `SourceKind.API` | `extract_reddit_body(raw_content)` | JSON payload |

`source.kind` is the authoritative discriminator because it matches the
ingester that produced the row — same dispatch shape as the poller.
`content_type` is recorded for diagnostics but never the deciding factor.

The Normalizer accepts an `extractor_factory: Callable[[SourceKind],
ExtractorFn | None]` constructor kwarg so tests inject fake extractors
without exercising trafilatura/pdfplumber. The factory pattern matches
the project-wide DI convention (RSS/HTML/poller all do the same with
`client` / `ingester_factory`).

### Adding a new SourceKind

Per the project convention (`MEMORY.md`):

1. New enum member in `musahit/common/types.SourceKind` — **ADR amendment**.
2. New extractor module under `musahit/normalize/extractors/`.
3. Branch on the new value in `_default_extractor_for`.

If a future source needs metadata the current extractors cannot
synthesise (e.g. a video transcript), the extractor function signature
stays the same — `(RawArticleRow) -> ExtractedArticle` — and the
upstream ingester does whatever shape-tuning it needs in `raw_content`
or `headers` JSON.

### The trafilatura fallback rule

```python
body = trafilatura.extract(raw_content)
if len(body) < 100 and config.body_selector:
    fallback = _selector_body(raw_content, config.body_selector)
    if len(fallback) > len(body):
        body = fallback
```

trafilatura is the well-known main-content extractor — it handles
boilerplate-stripped news pages correctly out of the box. The fallback
exists for the long tail: SPA shells, paywalled stubs, listing pages
that look like article pages, etc. The 100-character threshold is a
heuristic; tuning happens after the operator's first nightly run.

The fallback uses the `body_selector` from `html_selectors.SELECTORS`
(the same dict the HTML ingester reads). Selectors are placeholders
until verified on real fetches; the operator updates them as needed
without touching this file.

### RSS body precedence (handled by the ingester)

The RSS ingester now writes the first non-empty of
`content:encoded` / `description` / `summary` into `headers["body"]`.
The normalize extractor reads that field directly. If the stored body
contains HTML markup (common — feeds often wrap their `<description>`
content in `<p>` and friends), trafilatura is run on it to strip tags.

This split — ingester stores the chosen body, normalize cleans it —
keeps the normalize stage from re-parsing the feed XML per entry. The
cost of the choice is paid once in the ingester.

### PDF whitespace + page-number cleanup

```python
_PAGE_NUMBER_RE = re.compile(r"^\s*(?:Sayfa\s+)?\d{1,4}\s*$", re.MULTILINE)
```

Matches **only** lines that are *exclusively* a page number (optionally
prefixed with "Sayfa"). Real content lines that happen to begin with a
digit are untouched. Whitespace cleanup collapses runs of spaces/tabs
and clamps consecutive blank lines to at most one paragraph break.

### Reddit JSON flattening

```text
<selftext>

--- Yorumlar ---

<comment 1>

<comment 2>

<comment 3>
```

The Turkish marker `--- Yorumlar ---` is operator-readable and lets the
embedding stage / writer LLM treat comments as a coherent section rather
than mistaking them for body content. Maximum 3 comments per post is
enforced upstream by the ingester (`TOP_COMMENTS=3`).

### `pipeline_runs.stages_done` lifecycle

The poller writes `stages_done = ["ingest"]` after the ingest stage
finishes. This stage appends `"normalize"` to that list (idempotently —
re-running the stage on the same `run_id` doesn't add duplicate
entries). Future stages (cluster, score, etc.) continue the pattern.

`pipeline_runs.counts` also gains `articles_normalized` here. The
combined dict eventually carries `{articles, articles_normalized,
clusters, arcs}` after every stage runs.

`pipeline_runs.status` stays `RUNNING`; the writer stage flips it to
`COMPLETED` at the end of the pipeline per ADR-007.

---

## ❯ Mathematical / Statistical Details

**Language detection.** `langdetect` ports Google's classic
[language-detection](https://github.com/shuyo/language-detection) Java
library — a Naive-Bayes classifier over character-trigram features
trained on Wikipedia text. Probabilistic output; we accept the
top-1 label. Determinism comes from
`DetectorFactory.seed = 0` (set at module import). For inputs shorter
than 20 characters the classifier is unreliable; we return `"unknown"`
to be honest about that. Detection failure (mojibake, all-symbol text)
returns `"tr"` because the corpus is overwhelmingly Turkish and a
default-to-Turkish error has zero effect on the briefing for that case.

**Entity extraction.** Pure rule-based linear scan of the curated
vocabulary. For each `(canonical, surface_form)` pair the matcher
compiles a word-boundary regex against the *Turkish-locale-lowered*
text. Candidates are sorted by `(start, -length)` and a greedy pass
keeps the longest non-overlapping span at every position — so
"Recep Tayyip Erdoğan" wins over the alias "Erdoğan" when both match
the same span.

**Turkish-locale case folding.** Python's `str.lower()` and
`str.casefold()` both mishandle Turkish:

- `"İ".lower() == "i̇"` (i + combining dot) — wrong; Turkish wants `"i"`.
- `"I".lower() == "i"` — wrong; Turkish wants `"ı"` (dotless).

The matcher pre-translates `{"İ": "i", "I": "ı"}` and then calls
`.lower()`, which handles the rest of the alphabet (`Ş→ş`, `Ğ→ğ`, etc.)
correctly. Both the vocabulary surface forms and the input text are
folded the same way before matching.

---

## ❯ Design Decisions

**Curated vocabulary instead of spaCy / Turkish NER.**
MÜŞAHİT runs CPU-only on a Windows laptop. Loading a Turkish NER
transformer (or even a spaCy CNN) would add 200-500 MB of model weight
and several seconds of cold-start latency per night for marginal
recall gain on long-tail names — most of which the operator does not
want in the briefing anyway. A 60-entry curated list covers parties,
the cabinet, key institutions, and blue-chip companies; the operator
edits it when the briefing's signal gaps surface a new entity. If
that turns out to be too restrictive, **ADR-016 (proposed) revisits
the decision** and may switch to a hybrid (vocabulary + on-demand
transformer NER for opt-in long-tail expansion). The vocabulary
mechanism stays either way — the high-frequency entities deserve
curation regardless.

**Source.kind-keyed dispatch, not content_type.**
The goal mentioned `content_type` for dispatch, but `source.kind` is
strictly more reliable: every ingester sets `content_type` based on
HTTP response headers (RSS may be `application/rss+xml`, `application/xml`,
`text/xml`, ...; PDF could in theory be `application/octet-stream`).
`source.kind` is the project's authoritative discriminator and matches
how the poller dispatches. The Normalizer's `content_type` column read
is preserved for diagnostics.

**The trafilatura fallback is conservative.**
We only fall back when trafilatura returns less than 100 characters
*and* the source has a `body_selector` configured. Without both
conditions the fallback is a no-op — we trust trafilatura's primary
output. Conservatism keeps the failure modes simple: either trafilatura
or a CSS-selector extraction owns the result, not a half-merged mix.

**Body stored in `headers` JSON for RSS and PDF.**
The cheapest place to put already-extracted text is the column already
designed for ingester-specific metadata (ADR-015). No schema change;
no new column; the normalize stage reads the body without reaching for
pdfplumber or feedparser. The pattern matches the existing
`title` / `feed_entry_id` / `canonical_timestamp_method` keys.

**ExtractedArticle is one dataclass, populated in two phases.**
The extractor returns an `ExtractedArticle` with `title`, `body`, and
`published_at` populated; `lead`, `language`, `entities`,
`word_count` are filled by `Normalizer._enrich`. One dataclass keeps
the SQL INSERT clean; the two-phase fill keeps the extractor's
responsibility focused on title+body without it needing to know about
language detection or entity tagging.

**`articles.id == raw_articles.id` (shared ADR-014 hash).**
`INSERT ... ON CONFLICT (id) DO NOTHING` on the articles table makes
the normalize stage trivially idempotent. A rerun finds zero pending
rows because every previously-normalised article is already in the
`articles` table by the same id.

**Pre-seeded `pipeline_runs` row in tests.**
`ingest_log.run_id` has a foreign key on `pipeline_runs.run_id`. The
test fixture inserts a `pipeline_runs` row before any `ingest_log`
INSERT so the FK holds. In production the poller does this naturally
at run() start.

---

## ❯ Verification

```powershell
# Lint
python -m ruff check .                # All checks passed!

# Step-9 tests
python -m pytest tests/test_normalize_language.py tests/test_normalize_entities.py \
                 tests/test_normalize_extractors.py tests/test_normalizer.py -v
# Expected: 46 passed

# Full suite — no regressions
python -m pytest tests/ -q
# Expected: 279 passed, 1 skipped
```

Goal-criteria mapping:

| Criterion | Verification |
|---|---|
| (1) `Normalizer.run(run_id)`, dispatch, ExtractedArticle, INSERT into articles, stages_done append, per-row isolation | `musahit/normalize/normalizer.py`; `TestDispatchAndPersistence`, `TestStagesDone`, `TestFailureIsolation` |
| (2) HTML extractor with trafilatura + body_selector fallback | `musahit/normalize/extractors/html.py`; `TestHtmlExtractor` |
| (3) RSS extractor with content/description/summary precedence + trafilatura on markup | `musahit/normalize/extractors/rss.py` + ingester additive change; `TestRssExtractor` |
| (4) PDF extractor with passthrough + whitespace + page-number stripping | `musahit/normalize/extractors/pdf.py`; `TestPdfExtractor` |
| (5) Reddit extractor with JSON flatten + "Yorumlar" separator | `musahit/normalize/extractors/reddit.py`; `TestRedditExtractor` |
| (6) Language detection with short-text "unknown" + tr fallback | `musahit/normalize/language.py`; `TestDetectLanguage` |
| (7) Rule-based entity tagger over curated vocabulary | `musahit/normalize/entities.py` + `entities_vocab.py`; `TestPartyDetection` etc. |
| (8) Per-extractor + Normalizer tests | 46 tests across 4 files |
| (9) Zero network | All extractors run on bytes/strings; no HTTP, no PRAW, no langdetect-network |
| (10) ruff + 233+ tests stay green | 279 passed, 1 skipped (was 233; +46 new) |
| (11) FILE-PROTECTED untouched | `sources.py`, `defcon.py`, `promotion.py`, `poller.py`, ADRs unchanged |
| (12) This document | ✓ |

No tripwires fired:

- **All content_types recognised.** Every source.kind in the registry has an extractor.
- **trafilatura fallback works.** The body_selector path lands when needed; verified by test.
- **langdetect is fast enough.** The deterministic seed avoids per-call hot-path randomness; ~100µs per detection at our text sizes. No caching needed.
- **Vocabulary fits the dataclass.** VocabEntry's (canonical, type, aliases) tuple is enough for every current entity; no hierarchical structure needed.
- **No ADR contradictions.** ADR-014 article_id formula is reused as the PK link between raw_articles and articles; ADR-015 typed columns are populated as expected; ADR-006 articles schema is filled.

---

## ❯ Operator caveats

- **Entity vocabulary needs maintenance.** Cabinet rotations, party
  splits, new corporate names — the vocabulary in `entities_vocab.py`
  is a snapshot. The operator updates it monthly (or after major news
  events) by editing the file and re-running. No migration needed.
- **The trafilatura fallback's 100-char threshold is heuristic.** Some
  sources produce 80-character abstracts that are still useful; others
  produce 200-character SPA shells that are not. Tune by inspecting
  the operator's first nightly run. Each adjustment is a one-line edit.
- **Language "unknown" is a real value.** Headlines, court case codes,
  and very short Resmî Gazete items often fall here. The cluster stage
  (step 10) should not assume `language != "unknown"`.
- **PDF cleanup is whitespace-only.** Real PDFs sometimes have
  hyphenated line breaks (mid-word splits across lines) or column
  artifacts. The current cleanup does not de-hyphenate or re-flow.
  Add per-source post-processing in `pdf.py` if Resmî Gazette parses
  surface those artifacts.
- **Reddit body can be empty.** A link post (`is_self=False`) with no
  comments has nothing for the normalize stage to write. The article
  row will have empty `body` and `lead`; downstream clustering will
  treat it as a thin row. This is expected.

---

## ❯ Related Docs

- BOOTSTRAP.md — build step 9 of 20
- ADR-001 — architecture overview (Phase 1 / Phase 2 boundary)
- ADR-006 — storage; `articles` schema this code writes
- ADR-014 — article id formula; reused as the `articles.id == raw_articles.id` PK link
- ADR-015 — typed metadata columns; reused for `feed_entry_id` and `canonical_timestamp`
- (proposed) ADR-016 — vocabulary-vs-transformer decision; revisit after first-month operation
- `docs/implementations/2026-05-23-rss-ingest.md` — step 4 (RSS ingester deposits body in headers)
- `docs/implementations/2026-05-23-resmi-gazete-ingest.md` — step 6 (PDF ingester deposits body in headers)
- `docs/implementations/2026-05-23-poller.md` — step 8 (writes the `pipeline_runs` row that this stage updates)
