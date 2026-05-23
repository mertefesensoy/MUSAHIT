# MÜŞAHİT · build progress

## Completed

### Step 1 · `musahit/common/` · 2026-05-22

Types, db, logging, config. Python package is `musahit/` (not `src/`) — this
matches `-m musahit.pipeline`. BOOTSTRAP's `src/X` references map to `musahit/X`.
DEFCON enum deliberately absent from types.py (goes in musahit/score/defcon.py,
step 11, FILE-PROTECTED).

### Step 2 · `scripts/init_db.py` + `scripts/migrations/001_initial_schema.sql` · 2026-05-22

DuckDB schema migration runner. 14 ADR-006 tables + 7 non-VSS indices + HNSW
(conditional on VSS). `init_db()` in `musahit/common/migrations.py`. Connection
always opened with `load_vss=False`; VSS handled internally with try/except.
`_install_and_load_vss` is a separate monkeypatchable helper.
22 tests, all pass.

### Step 3 · `musahit/ingest/sources.py` · 2026-05-23

FILE-PROTECTED source registry. 37 sources (24 NEWS / 6 MARKETS / 6 GOV / 1 SOCIAL).
ADR-013 created for two operator overrides: bloomberg_ht=CENTRIST, x_stub omitted.
7 RSS URLs pending operator verification (anadolu, t24, medyascope, dw_tr, voa_tr,
reuters_tr, kap). `seed_sources(conn)` upserts to sources table; called from init_db.py.
_build_sources_index() raises ValueError at import on any duplicate/invalid/empty entry.
39 tests, all pass. Full suite: 122 passed, 1 skipped.

### Step 4 · `musahit/ingest/rss.py` + `Ingester` Protocol · 2026-05-23

`Ingester` `typing.Protocol` and `IngestResult` dataclass added to `musahit/ingest/__init__.py`.
`RssIngester` class implements the Protocol using httpx (async, timeouts, UA=`MUSAHIT/0.1`)
to fetch and feedparser to parse bytes. Article id = `sha256(source_id|url)` — excludes
`fetched_at` (ADR-006 comment is descriptive; including it would defeat inter-fetch dedup).
Two dedup layers: in-memory `set` on feed_entry_id (intra-fetch), `ON CONFLICT (id) DO NOTHING`
on raw_articles.id (inter-fetch). Canonical timestamp = `min(published, updated)` stored in
`headers` JSON. Error mapping: TimeoutException→TIMEOUT, other HTTPError or status≥400→
HTTP_ERROR, bozo+empty→PARSE_ERROR, bozo+entries→OK (partial). 14 tests, all pass via
`httpx.MockTransport` (zero network). Full suite: 136 passed, 1 skipped. Incidental ruff
fix in `musahit/common/migrations.py` (SIM105 try/except/pass → contextlib.suppress).

### Same-day amendment · ADR-014 + ADR-015 · 2026-05-23

Both ADRs landed before step 5 starts, to promote in-flight deviations to canonical record:

- **ADR-014** · article id formula amendment: `sha256(source_id|url)` is now canonical
  across all ingesters. Extracted to `musahit/common/ids.py::article_id(source_id, url)`.
  Step 5+ MUST import the helper rather than reimplement.
- **ADR-015** · article metadata typed columns: `raw_articles` gains `feed_entry_id TEXT NULL`
  and `canonical_timestamp TIMESTAMP NULL` via migration 002, plus
  `idx_raw_articles_canonical_ts`. Ingester-specific metadata stays in `headers` JSON.
- `tests/common/test_ids.py` pins the three formula properties (stability, source-scope,
  url-scope). `tests/test_rss.py` updated to assert on typed columns. `tests/test_init_db.py`
  updated to expect `migrations_applied == 2`.
- Latent bug fixed: DuckDB TIMESTAMP is tz-naive; tz-aware Python datetimes were being
  silently shifted to local time on insert. RSS now stores naive UTC for `fetched_at` and
  `canonical_timestamp`.
- ADR-006 (FILE-PROTECTED) edited with amendment block + inline comment updates — explicitly
  authorized by ADR-014 and ADR-015's "Required follow-up edits" sections.
- Full suite: 144 passed, 1 skipped. Ruff clean.

### `musahit/common/time.py` · UTC-naive convention · 2026-05-23

Project-wide rule before step 5: every component that writes a TIMESTAMP to DuckDB MUST
use `musahit.common.time.utcnow()` (for "now") or `musahit.common.time.to_utc_naive(dt)`
(to normalize a possibly-aware datetime). DuckDB's TIMESTAMP column is tz-naive and
silently shifts tz-aware Python datetimes to local time on insert. RSS (step 4) was
refactored to use the helpers; later stages MUST do the same.

`utcnow() -> datetime` returns naive UTC. `to_utc_naive(dt) -> datetime | None`
preserves None, returns naive inputs unchanged, and converts tz-aware inputs to UTC
before stripping tzinfo. 7 tests in `tests/common/test_time.py`.

### Step 5 · `musahit/ingest/html.py` + `html_selectors.py` · 2026-05-23

`HtmlIngester` implements the Ingester Protocol; two-phase fetch (listing → article URLs
→ per-article pages). httpx async with `MUSAHIT/0.1` UA; selectolax for parsing. Per-source
CSS selectors in `musahit/ingest/html_selectors.py` (`SelectorConfig` dataclass + 9
placeholder entries for all kind=HTML sources in the registry). URL dedup before per-article
fetches (`list(dict.fromkeys(urls))`); rate-limit sleep between fetches (not before first).
Per-article failures isolated (HTTP, parse) — listing failures abort the source.
canonical_timestamp chain: JSON-LD → meta tags → Turkish-formatted date regex →
fetched_at; method name written to `headers.canonical_timestamp_method`. `published_selector`
narrows step 3 (not a 5th step). `feed_entry_id` is NULL for HTML (ADR-015). Article id via
shared `musahit.common.ids.article_id`. 16 tests, all pass via `httpx.MockTransport` + injected
fake sleep. Full suite: 167 passed, 1 skipped. All 9 selector entries are first-pass
placeholders — first nightly run will surface those needing tuning.

### Step 6 · `musahit/ingest/resmi_gazete.py` + `gazette_parsing.py` · 2026-05-23

`ResmiGazeteIngester` implements the Ingester Protocol; one PDF per day expands into N
`raw_articles` rows (one per parsed item). Pure parser in `musahit/ingest/gazette_parsing.py`
with `GazetteSection` (EXECUTIVE/JUDICIAL/ANNOUNCEMENT), `GazetteItemType` (LAW,
PRESIDENTIAL_DECREE, REGULATION, COMMUNIQUE, APPOINTMENT, COURT_DECISION, OTHER),
`GazetteItem` dataclass, `parse_gazette_pdf` (PDF→items) and `parse_gazette_pages` (pure
text→items). Synthetic URL `resmi-gazete://YYYY-MM-DD/<TYPE>/<reference>` feeds the shared
`article_id`. Today→yesterday URL fallback; Mükerrer probing stops on first 404. Main-PDF
parse error → PARSE_ERROR; supplement parse error → log + skip. canonical_timestamp =
publication date at 00:00 UTC (naive). feed_entry_id = extracted reference (NULL when
empty). Real HTTP URL in `headers.real_pdf_url` for traceability. 38 new tests (29 parser
+ 9 ingester) using hand-crafted fixture PDFs generated once by `reportlab` (not a project
dep). Full suite: 205 passed, 1 skipped.

### Step 7 · `musahit/ingest/reddit.py` + `reddit_subreddits.py` · 2026-05-23

`RedditIngester` implements the Ingester Protocol via PRAW (sync) wrapped in
`asyncio.to_thread`. Subreddits in `musahit/ingest/reddit_subreddits.py`:
r/Turkey, r/TurkeyJerky, r/AskTurkey, r/europe (Turkey flair required).
Filters per ADR-003: last 24h AND (score ≥ 50 OR num_comments ≥ 25). Per-post:
synthetic_url = `https://www.reddit.com{permalink}`; article_id via shared helper;
feed_entry_id = post.id; canonical_timestamp = post.created_utc as naive UTC;
raw_content = JSON {title, selftext (≤500), top-3 comments (≤200 each), author,
score, num_comments}; headers JSON = {subreddit, score, num_comments, flair,
external_url}. Hard cap DEFCON 4 is downstream (score stage) per ADR-005,
NOT enforced here. `prawcore.ResponseException` → HTTP_ERROR;
`prawcore.RequestException` → TIMEOUT. No credentials / no client / praw missing
→ SKIPPED. 11 tests via constructor-injected `FakeRedditClient`.

Also: `tr_local_date()` added to `musahit/common/time.py` (TR is UTC+3 year-round,
no DST since 2016). `resmi_gazete.py` refactored to use it. `memory/MEMORY.md`
extended with the "enum expansion = ADR amendment" convention. Full suite:
219 passed, 1 skipped.

### Step 8 · `musahit/ingest/poller.py` (FILE-PROTECTED) · 2026-05-23

`IngestPoller.run(run_id=None)` orchestrates the full ingest stage. `get_ingester(source, conn, settings)`
dispatches by `SourceKind` to RssIngester / HtmlIngester / ResmiGazeteIngester / RedditIngester;
DEFERRED and unknown kinds return None → SKIPPED ingest_log row. asyncio.gather with
asyncio.Semaphore (default cap 8); per-source `asyncio.wait_for` floor =
`max(default_timeout_seconds, rate_limit_seconds * 12)`. Per-source failure isolation: TimeoutError →
TIMEOUT, any Exception → PARSE_ERROR. Default run_id = `"run_" + tr_local_date().isoformat().replace("-","")`.
pipeline_runs UPSERT (status=RUNNING, stages_done=[] at start; stages_done=["ingest"] + counts JSON
at end; status STAYS RUNNING — writer stage marks COMPLETED). ingest_log UPSERT on (run_id, source_id)
so manual reruns overwrite. 14 tests via constructor-injected FakeIngester + ConcurrencyTracker.
Full suite: 233 passed, 1 skipped.

**Phase 1 (ingestion) complete · Step 8 binds the four ingesters into one orchestrator.**

### Step 9 · `musahit/normalize/` · 2026-05-23

First non-ingest stage. `Normalizer.run(run_id)` LEFT JOINs raw_articles against articles to
find pending rows, dispatches by `source.kind` to one of four extractors. RSS reads
headers["body"] (added in step 9 to the RSS ingester). HTML uses `trafilatura.extract` with a
< 100-char fallback to selectolax + body_selector from html_selectors.py. PDF reads
headers["body"] (added in step 9 to the resmi_gazete ingester), normalises whitespace, strips
standalone page numbers. Reddit parses raw_content JSON, flattens to selftext + "Yorumlar"
separator + top-3 comments. ExtractedArticle dataclass holds title/body/lead/published_at/
language/entities/word_count; Normalizer enriches lead (first 500 chars) + language (via
langdetect; <20 chars → "unknown"; LangDetectException → "tr") + entities (rule-based over
curated vocab) + word_count. Turkish-locale case folding handles İ→i and I→ı correctly
before regex matching. Vocabulary: 60+ entries across PARTY/INSTITUTION/PERSON/COMPANY.
ON CONFLICT(id) DO NOTHING for idempotence. stages_done appends "normalize". Per-row
exceptions logged and skipped. 46 new tests. Full suite: 279 passed, 1 skipped.

langdetect added to pyproject deps. trafilatura already there. Ingester additive changes:
RSS now stores `body` in headers (content:encoded > description > summary), Resmi Gazete
now stores `body` (GazetteItem.body) in headers. Both backward-compatible with existing tests.

**Phase 2 (processing) begins · normalize is the first stage.**

### Step 10 · `musahit/cluster/` · 2026-05-23

First Ollama-dependent stage. `Clusterer.run(run_id)` embeds via bge-m3, partitions by
language, greedy single-pass cosine clustering. `EmbeddingClient` Protocol with
`OllamaEmbeddingClient` (httpx → /api/embed, batch 50, 60s timeout) and `FakeEmbeddingClient`
(deterministic bag-of-words hash → 1024-dim) for tests. Pure `cosine_similarity` and
`compute_centroid` in centroid.py (no numpy). Default 0.7 threshold + 24h window.
word_count < 10: skipped entirely. word_count < 30: headline-only (can join, can't seed).
word_count >= 30: full text. Language partition: tr / en / unknown (and any other code)
each in its own bucket; no cross-language matching. Greedy pass sorted by published_at
ascending: each article finds in-window cluster with highest cosine ≥ threshold, joins or
seeds. Cluster id `cl_YYYYMMDD_NNNN` (daily counter). bands_present = union of member
source bands. Writes article_embeddings + clusters + cluster_articles + cluster_embeddings.
INSERT OR IGNORE everywhere for idempotence. Appends "cluster" to stages_done. 32 new
tests via FakeEmbeddingClient. Full suite: 311 passed, 1 skipped.

MEMORY.md updated with two conventions: Turkish locale case folding
(`str.maketrans({"İ":"i","I":"ı"})` + `.lower()`) and ADR-016 trigger (vocabulary-vs-
transformer NER reconsideration if >20% clusters lack entities or operator manually
tags >3/week).

### Step 11 · `musahit/score/` · 2026-05-23

First worker-LLM stage. `Classifier.run(run_id)` reads unscored clusters, calls Qwen2.5 7B
via FakeLlmClient/OllamaLlmClient, parses JSON via WorkerResponse pydantic, retries on
ValidationError (max 2), falls back to {AMBIENT, UNCLASSIFIED, low} after retries.
Two-step: worker → raw_defcon, then deterministic promotion → ceiling/confidence/final.
`final_defcon = min(raw, ceiling)` per ADR-005. Bootstrap demotion bumps final +1 (less
severe) when cluster.created_at < MIN(pipeline_runs.started_at) + 7d. FILE-PROTECTED:
`defcon.py` (IntEnum 0-5 + Turkish labels + anchors from ADR-004) and `promotion.py`
(PRIMARY_BANDS, IDEOLOGICAL_SIDES, compute_ceiling, ideological_sides, confidence,
final_defcon, bootstrap_demoted, apply_bootstrap_demotion). `llm_client.py` LlmClient
Protocol + OllamaLlmClient (httpx → /api/generate, 120s, qwen2.5:7b-instruct-q4_K_M) +
FakeLlmClient (substring or callable responder with attempt counter). `schema.py`
WorkerResponse pydantic (defcon 0-5, Category, Literal high/medium/low, entities, summary
≤500, headline ≤200) + `parse_worker_response` (strips markdown fences). `prompt.py`
reads DEFCON_ANCHORS as single source of truth. Writes clusters (UPDATE with DuckDB FK
workaround: DELETE cluster_articles + cluster_embeddings → UPDATE → re-INSERT) +
promotion_log (UPSERT on cluster_id). stages_done += "score". 52 new tests via
FakeLlmClient. Full suite: 363 passed, 1 skipped.

MEMORY.md: FILE-PROTECTED list now includes `score/defcon.py` and `score/promotion.py`.
DuckDB FK gotcha documented in impl doc: UPDATE on referenced row fires FK check;
workaround is per-statement auto-commit DELETE+UPDATE+reINSERT (NOT inside a transaction).

### Between step 11 and step 12 · ADR-005 prose correction · 2026-05-23

ADR-005's "the ceiling can only *lower* the worker's raw score · it cannot raise it"
sentence was rewritten to remove the directional ambiguity. The implementation was
unchanged (final_defcon = min(raw, ceiling) is still the formula); only the prose
explaining the formula was updated to describe the IntEnum direction explicitly.
Amendment block added at the top of ADR-005 following the ADR-006-amended-by-014/015
precedent. No code change; no test change.

### Between step 11 and step 12 · DuckDB FK Update Pattern convention · 2026-05-23

Added the "DuckDB FK Update Pattern" project convention to `memory/MEMORY.md`. Documents
the DELETE-child → UPDATE-parent → INSERT-child workaround discovered in step 11 for
`UPDATE clusters` when `cluster_articles`/`cluster_embeddings` reference it. Each
statement auto-commits (no explicit `BEGIN`/`COMMIT`); inside a transaction the FK check
still fires. Applies to `clusters`, the upcoming `arcs` updates in step 12, and any
future parent-with-active-children update. No code change; documentation only.

### Between step 11 and step 12 · ADR-005 formula correction · 2026-05-23

**Supersedes the prose-only amendment above.** The prose amendment surfaced a worked-
example contradiction: the new prose described `max(raw, ceiling)` behavior but the
formula text and the implementation both used `min(raw, ceiling)`. Tracing through the
three semantic intents (X-only cap at DEFCON 4 · single-band cap at DEFCON 4 · primary
sources don't auto-promote routine events to UNTHINKABLE), `max` is what ADR-005 actually
requires. Implementation in `musahit/score/promotion.py::final_defcon` switched to `max`.
Updated: `tests/test_promotion.py::TestFinalDefcon` (renamed test method + 4 new
assertions for max-formula cases), `tests/test_classifier.py` (three pinned `final_defcon`
assertions recomputed under max; the bootstrap-demotion case stayed unchanged because
raw and ceiling were equal). Full suite: 363 passed, 1 skipped (zero regressions).
MEMORY.md gained an "ADR semantic intent overrides formula text" convention with the
process for the next discovery.

### Step 12 · `musahit/arcs/` · 2026-05-23

Story arc linking. `ArcLinker.run(run_id)` reads scored clusters without arc_id ordered
by final_defcon ASC (lower int = more severe seeds first), loads OPEN+WATCH arcs from
last 30 days into memory, and matches each cluster via cosine ≥ 0.55 AND Jaccard ≥ 0.4
(stopword-filtered entity sets). Match: update cluster.arc_id, refresh arc fields and
arc_centroids; WATCH → OPEN on re-link. No match: seed `arc_YYYYMMDD_NNNN`. Cleanup pass
`transition_states(conn, now)` runs at end: OPEN > 7d → WATCH; WATCH > 30d → RESOLVED;
RESOLVED never auto-transitions. peak_defcon update uses MIN (lower int = more severe;
ADR-008's "max" prose is the same bug pattern as ADR-005 had — implementation matches
intent; ADR-008 prose amendment queued). FK workaround applied at both cluster and arc
UPDATEs (DELETE child → UPDATE parent → re-INSERT child, per-statement auto-commit;
clusters pointing at the arc being updated get nulled then restored using the
cluster-level workaround). Stopword list (`STOPWORD_ENTITIES`) prevents the "every
political cluster matches every other" collapse. 34 new tests across 3 files (matching:17,
transitions:9, linker:10). Full suite: 397 passed, 1 skipped.

### Between step 12 and step 13 · ADR-002 amendment · 2026-05-23

Writer-model version corrected in ADR-002 ahead of step 13. The original spec assumed
Trendyol-LLM 7B v4 (GGUF + Modelfile import from HuggingFace), but v4 does not exist
publicly. The latest publicly-available Trendyol chat release is v1.8 (mid-2025),
published on Ollama Hub by community maintainer serkandyck as
`serkandyck/trendyol-llm-7b-chat-v1.8-gguf` (4.4 GB, 32K context, pullable with a
single `ollama pull`). Amendment block at top of ADR-002; Writer-model section + Installation
section + Open-questions item all updated. Step 13 will reference the corrected model
string from the start. No code change (writer not yet built).

### Between step 12 and step 13 · ADR-008 prose amendment · 2026-05-23

ADR-008's `peak_defcon` prose ("highest DEFCON ever seen") was clarified to remove the
same IntEnum-direction ambiguity that ADR-005 had. Amendment block added at the top;
the data-model comment now says "highest severity (= lowest integer) ever seen"; the
Arc-updates formula now reads `min(arc.peak_defcon, cluster.final_defcon)` with the
directional reasoning inline. No code change; no test change. The flagged bug from the
step-12 impl doc is now closed.

### Step 13 · `musahit/writer/` · 2026-05-23

First Trendyol-LLM stage. `Briefer.run(run_id)` builds a `BriefingPayload` from the DB,
calls the writer LLM with `build_writer_prompt(payload)`, validates the output via
`validate_briefing_markdown` (8 required ❯-prefixed `##` sections in ADR-009 order,
exact marker match, no extra top-level sections), retries up to 3 times with the
validator's errors appended to the prompt, and falls through to
`render_fallback_briefing(payload)` (deterministic Python renderer that always passes
the validator) if all retries fail. Writes `briefings/YYYY/MM/DD/briefing.md` and
upserts the `briefings` row (PRIMARY KEY = date, per-date idempotent). Reuses the
`LlmClient` Protocol + `FakeLlmClient` from step 11 — model string is
`serkandyck/trendyol-llm-7b-chat-v1.8-gguf` per the ADR-002 amendment landed earlier today.
`config.toml` + `Settings.writer_model` updated to match. Estimated worst-case prompt ~13K
tokens (comfortably under Trendyol-LLM's 32K context window); a test guard pins it
under ~16K chars. 47 new tests across `tests/test_writer/`. Full suite: 444 passed, 1
skipped. stages_done += "write".

**Phase 2 (processing) complete via the writer; Phase 3 (delivery) is next.**

### Between step 13 and step 14 · ADR-010 amendment · 2026-05-23

Piper integration path corrected in ADR-010 ahead of step 14. The original spec described
a standalone Windows binary with subprocess invocation against `rhasspy/piper`, but that
project is archived and Piper development moved to `OHF-Voice/piper1-gpl` which ships as
a Python package (`pip install piper-tts`, v1.4.2 as of April 2026) with a `PiperVoice`
API. License changed from MIT to GPL-3.0 (no practical impact for personal use; would
impose GPL obligations on any future redistribution). Amendment block at top of
ADR-010; Installation section now leads with `pip install piper-tts`; Python integration
block flags "no subprocess, no binary in PATH" explicitly; Open questions adds a guard
against silently switching back to subprocess invocation. No code change (step 14 not
yet built); the corrected integration lands cleanly when step 14 starts.

### Step 14 · `musahit/tts/` · 2026-05-23

First Phase 3 (delivery) stage. `Synthesizer.run(run_id)` reads today's briefing
markdown via `briefings.markdown_path`, extracts the ADR-009 voiced scope via
`extract_voiced_briefing(md)` (header + DEFCON 1-2 ÖNCELİKLİ + DEFCON 3 MATERYAL
sans Kaynaklar + AÇIK GELİŞMELER + literal closing line), preprocesses each chunk
via `preprocess_for_tts` (TCMB→"Te Ce Me Be", DEFCON N→Turkish numeral, markdown
strip, Kaynaklar line drop, blank-line collapse), synthesises each chunk via
`PiperClient.synthesize`, interleaves a 200ms 80Hz tick between chunks, concatenates
the WAVs via pure-stdlib `concatenate_wavs`, encodes to 128kbps mono MP3 via
`wav_to_mp3` (pydub→ffmpeg), writes `briefings/YYYY/MM/DD/briefing.mp3`, UPDATEs
`briefings.audio_path`, appends `tts` to `stages_done`. Per ADR-012 § Stage 7
always-ships: ANY exception in the synthesis chain catches → writes
`silent_placeholder_mp3()` (1s of stdlib-`wave` silence inside the `.mp3` extension;
HTML5 audio content-sniffs and plays it fine).

`PiperPythonClient` uses the ADR-010-amended `from piper import PiperVoice` API
(NO subprocess, NO binary in PATH) — loads the ONNX voice once in the constructor,
calls `voice.synthesize_wav(text, wav_file)` per chunk (the canonical method in
piper-tts==1.4.2; ADR-010's amendment example showed an outdated `synthesize(text, f)`
signature, the actual API uses `synthesize_wav`). Synchronous Piper call wrapped in
`asyncio.to_thread` + `asyncio.wait_for(timeout=60s)`. `FakePiper` returns
deterministic silent WAV bytes; `FailingPiper` raises on every call.

Synthesizer constructor: `(db, piper, briefings_root)` positional + keyword-only
optional `mp3_encoder` (defaults to `wav_to_mp3`). Tests inject a tiny fake encoder
returning `b"FAKE_MP3:..."` so the happy-path test runs on ffmpeg-less machines.
Real `wav_to_mp3` roundtrip test gated on `shutil.which("ffmpeg")` — skips one test
on this environment.

pyproject.toml gains `piper-tts>=1.4.2`, `pydub>=0.25.1`, and `audioop-lts>=0.2.2`
conditional on Python 3.13+ (Python 3.13 removed the stdlib `audioop` module that
pydub depends on; the `-lts` backport is the maintained drop-in). Settings +
config.toml updated: `piper_voice_path` default points at
`C:/Users/senso/AppData/Local/piper/voices/tr_TR-dfki-medium.onnx` (forward-slash
form; the path written by `scripts/install_windows.ps1`).

70 new tests across `tests/test_tts/`: 23 extractor (voiced/skipped section assertions,
Kaynaklar stripping, degraded inputs), 17 preprocessor (acronym/DEFCON/markdown/source
line/whitespace integration), 8 piper (FakePiper + FailingPiper + monkey-patched
`PiperVoice.load` so the real ONNX is never touched), 4 transitions (WAV format,
duration, cache identity), 7 encoder (concatenate happy/empty/order/format-mismatch +
ffmpeg-gated MP3 roundtrip), 11 synthesizer (happy path, piper failure → placeholder,
encoder failure → placeholder, missing briefings row, idempotent rerun, stages_done,
placeholder WAV validity). Full suite: 513 passed, 2 skipped (zero regressions; +69
passed, +1 skipped for the ffmpeg-gated case). Ruff clean.

**Phase 3 (delivery) begins · TTS is the first delivery-side stage. Pipeline runner
(step 17), liveness probe (step 18), and dashboard (step 19+) follow.**

### Between step 14 and step 15 · TTS diagnostic improvements · 2026-05-23

Two diagnostic gaps surfaced during the post-step-14 smoke test (the smoke run
produced a silent placeholder MP3 because ffmpeg wasn't on PATH for the
PowerShell session, and the actual cause was invisible) — both fixed in place:

1. `musahit/tts/encoder.py` gains `check_ffmpeg_available()` — calls
   `shutil.which("ffmpeg")`, raises `RuntimeError("ffmpeg not found on PATH ·
   install via winget install ffmpeg on Windows or apt install ffmpeg on
   Linux")` if missing. Called at module load time wrapped in try/except that
   emits a `UserWarning` (so unit tests that don't actually encode MP3 keep
   importing cleanly). `wav_to_mp3` itself calls `check_ffmpeg_available()` at
   the start of its body — the strict raise that surfaces the env gap before
   pydub's opaque `FileNotFoundError [WinError 2]` subprocess failure.
2. `musahit/tts/synthesizer.py`'s exception handler now calls
   `traceback.print_exc(file=sys.stderr)` before the structured `log.warning`
   call. This guarantees manual / smoke-test invocations show the underlying
   error even when `configure_logging()` hasn't been called. Production runs
   (where the JSON log pipeline is wired) get both signals.

4 new tests: `test_encoder.py::TestCheckFfmpegAvailable` (3 tests: raises when
missing, passes when present, `wav_to_mp3` short-circuits before pydub) and
`test_synthesizer.py::TestPiperFailure::test_piper_crash_prints_traceback_to_stderr`
(1 test using `capsys` to verify stderr contains `Traceback`, exception type,
and message; while preserving placeholder + audio_path + flag behaviour). Full
suite: 517 passed, 2 skipped (was 513/2; +4 pass).

`scripts/smoke_tts.py` and `scripts/diagnose_tts.py` are operator-side
utilities created during the smoke-test investigation; both also got an
incidental ruff import-order fix.

Operator audio QA also flagged that "DEFCON" was being pronounced "De-Fe-Kon"
under Turkish phoneme rules instead of the English-style "Def-Kon" Turkish
speakers familiar with the term actually use. `_defcon_num_repl` in
`musahit/tts/preprocessor.py` now respells the TTS-bound text as `Defkon
{Turkish numeral}` (was `DEFCON {Turkish numeral}`). One-line change in the
replacement string; written briefing keeps "DEFCON" unchanged — only the
text that flows into `PiperVoice.synthesize_wav` gets the respelling.
Preprocessor tests expanded: per-level assertions for all five DEFCON levels
(replacing the prior two-level spot check), plus guards against silent
regression to the all-caps form and against false-positive respelling of bare
DEFCON prose (e.g., "DEFCON ölçeği"). Preprocessor test count 21 → 24 (+3
methods); full suite 520 passed, 2 skipped.

## Next

Step 15 — see BOOTSTRAP.md build order. The TTS module is wired and FILE-PROTECTED
adjacent (no changes to the protected list yet).
