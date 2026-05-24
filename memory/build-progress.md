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
(replacing the prior two-level spot check), plus a guard against silent
regression to the all-caps form. Preprocessor test count 21 → 24 (+3
methods); full suite reached 520 passed at this point.

Follow-up the same day: audio QA surfaced two patterns the narrow regex
missed — `Zirve DEFCON · 3` (numeral after a middle-dot or colon/hyphen
separator with optional spaces) and standalone `Zirve DEFCON` (no trailing
numeral). The regex was loosened from `\bDEFCON\s+([1-5])\b` to
`\bDEFCON\b(?:\s*[·:-]?\s*([1-5]))?`; `_defcon_num_repl` handles the
None-group case by emitting bare `Defkon`. Standalone `DEFCON` is now also
respelled (semantic change from the initial work — Piper mispronounces bare
`DEFCON` the same way as the digit-bearing form, so the fix applies to both).
New tests: `test_middle_dot_separator`, `test_colon_separator`,
`test_hyphen_separator`, `test_separator_without_spaces`,
`test_standalone_defcon_becomes_defkon`, `test_standalone_defcon_in_prose`.
The synthesiser test `test_piper_called_per_chunk` was updated to assert on
the post-respelling header chunk (`Zirve Defkon İki`) rather than the
pre-respelling form. Preprocessor tests 24 → 30 (+6); full suite 525 passed,
2 skipped.

### Step 15 · `musahit/pipeline.py` + `orchestrator.py` + `stages.py` · 2026-05-23

Top-level pipeline orchestrator. Binds the seven stages (ingest · normalize ·
cluster · score · arc-link · write · tts) into one nightly run with checkpoint
resumability per ADR-007, per-stage timing budgets per ADR-007 § Pipeline
timing budget, Ollama model lifecycle per ADR-001 § Single Ollama instance,
and failure isolation per ADR-012 § Failure isolation by stage.

`Orchestrator(conn, settings, *, stage_factory, ollama, timing_budgets,
disk_check_path)`. Constructor-injected `stage_factory: Callable[[str], Stage]`
lets tests pass fakes — production uses `DefaultStageFactory` which lazily
constructs the seven real stages (IngestPoller, Normalizer, Clusterer,
Classifier, ArcLinker, Briefer, Synthesizer) with their real Ollama / Piper
clients on demand. `DryRunStageFactory` returns `_NoOpStage` stubs (no DB
writes, no I/O) for `--dry-run`.

Per stage: skip if in `stages_done` and not `--force`; skip if `--stage NAME`
filter and name doesn't match; load required Ollama models (cluster→bge-m3,
score→qwen2.5, write→trendyol; others none); construct stage; run with
`asyncio.wait_for(timeout=soft_minutes*2)`. On success append to stages_done.
On soft failure: `traceback.print_exc(sys.stderr)` (step-14 diagnostic pattern)
+ structured log + `failed_stages` JSON list, continue. After stage: unload
the model the stage held. Catastrophic conditions (KeyboardInterrupt /
DiskPressureError / duckdb.IOException) wrap as `_CatastrophicError`, mark
pipeline_runs.status=FAILED, preserve stages_done, re-raise so CLI exits 2
(SIGINT) or 1 (others). Pre-flight disk check via shutil.disk_usage against
Settings.min_free_disk_gb (default 5 GB) → raises DiskPressureError before
stage 1.

`OllamaModelManager.load(model)` POSTs `/api/generate` with empty prompt and
`keep_alive: "5m"` to warm-cache the model; `.unload(model)` posts same body
with `keep_alive: 0` for immediate eviction. Failures swallowed at WARN — the
stage that needs the model will fail with a specific error.

`Stage` Protocol: `async run(run_id: str) -> Any` (Any so dict-returning real
stages structurally match). `STAGE_ORDER` 7-tuple keyed off the canonical
stage names (arc-link with hyphen matches what step 12 already writes to
stages_done). `StageTimingBudget(soft_minutes: float)` with `soft_seconds`
and `timeout_seconds` properties; STAGE_BUDGETS pulled from ADR-007's
01:00→07:00 schedule (ingest 60, normalize 30, cluster 60, score 60,
arc-link 30, write 60, tts 30). Float type lets tests use fractional-minute
budgets (e.g., 0.0005 min = 30 ms) for timeout-path coverage.

CLI at `musahit/pipeline.py`: argparse subcommands `run`/`status`/`resume`.
`--date today|YYYY-MM-DD` (default today via `tr_local_date()`). `--stage NAME`
filter. `--force` re-runs completed stages. `--dry-run` uses NoOp stages and
skips DB writes. Exit codes: 0 COMPLETED, 1 FAILED, 2 SIGINT.
`configure_logging()` first thing in main().

Schema migration `003_add_failed_stages.sql` — additive: `ALTER TABLE
pipeline_runs ADD COLUMN IF NOT EXISTS failed_stages TEXT`. Backward-compatible
with the FILE-PROTECTED `IngestPoller` (no column reference there).
`test_init_db.py` updated: 6 occurrences of `migrations_applied == 2` →
`== 3`, plus the `schema_version` row-count assertion (`== 2` → `== 3`).

33 new tests: 16 in `test_orchestrator.py` (happy path, soft failure, timeout,
resume, --force, --stage, --dry-run, model lifecycle, disk pressure,
KeyboardInterrupt, stderr traceback) + 17 in `test_pipeline_cli.py`
(subcommand parsing, date resolution, flag plumbing, exit codes, output
summary, `configure_logging` ordering with Orchestrator stub). Full suite:
558 passed, 2 skipped (was 525/2; +33 pass, 0 new skips).

FILE-PROTECTED list untouched. The orchestrator instantiates `IngestPoller`
via the factory but does not modify the protected file.

**Phase 3 expands: TTS (step 14) and orchestrator (step 15) are in. Liveness
probe (step 18) and dashboard (step 19+) follow.**

### Step 16 prep · first smoke-run scaffolding · 2026-05-23

No code changes — three operator-facing artifacts to support the first
real end-to-end run against real Turkish sources and real Ollama / Piper
models:

- `memory/operator-tasks.md` — structured backlog for first-run findings.
  Three buckets: Pending (must address before step 17), First-month
  tuning (address during operation), Resolved (with date). Each entry is
  a one-liner; long context belongs in `docs/implementations/` or an ADR
  amendment. The file starts empty; the operator populates it as
  findings surface during the smoke run and the early operational weeks.
- `scripts/run_first_smoke.ps1` — one-command launcher. Five pre-flight
  checks: required Ollama models present (`qwen2.5:7b-instruct-q4_K_M`,
  `serkandyck/trendyol-llm-7b-chat-v1.8-gguf`, `bge-m3`); Piper voice
  ONNX exists; ≥ 5 GB free on the data drive; DuckDB migration version
  == 3; data directory writable. Each check is colour-coded PASS/FAIL.
  Runs `python -m musahit.pipeline run --date <date>` with
  `Tee-Object` capturing stdout to `logs/smoke-<timestamp>.jsonl`. On
  COMPLETED prints briefing artifact paths and next-step pointers; on
  FAILED prints last-50-line log tail + diagnostic suggestions (which
  stages completed, how to inspect failed_stages, how to retry single
  stages or resume).
- `docs/operator/first-smoke-run-guide.md` — operator runbook. TL;DR
  command, pre-flight checklist (URL + selector audit caveats for the
  7 unverified RSS sources and 9 placeholder HTML selectors, model
  pulls, Piper voice download, ffmpeg, Reddit creds, disk space, DB
  schema). Per-stage timing guesses (ingest 30-60min, normalize 5-10,
  cluster 10-20, score 30-90, arc 5-10, write 15-30, tts 5-10) against
  ADR-007 soft budgets. "What to expect from first run" framing —
  5-15 first-run findings is expected, not a failure mode; the
  always-ships invariant means the briefing still produces even when
  components fail. Inspection helpers: briefing artifact reading,
  `pipeline status` command, a python -c block for per-stage ingest
  counts / DEFCON distribution / arc states / failed_stages, structured-
  log greps. Retry shapes: `--resume`, `--force`, `--stage NAME`,
  backfill with explicit `--date`, `--dry-run`. Where to file each
  finding type.

Smoke run itself is operator-driven from here. Findings will accumulate
in `memory/operator-tasks.md` and feed step 17's scope. No tests added;
no code changed; ruff + pytest still pass (verified).

### Step 16 prep · smoke-script parse-error fix · 2026-05-23

First attempted smoke run surfaced two parse errors in
`scripts/run_first_smoke.ps1` before the pipeline could start:

1. `$_.Length / 1KB` inside a nested `$()` subexpression in a double-
   quoted string: the PowerShell byte-multiplier constant `1KB` is
   ambiguous in that context, parser fails with "Unexpected token 'KB'".
   Replaced with the literal integer `1024`.
2. Em dash (U+2014) on lines 68 and 220 was saved as multi-byte UTF-8
   bytes that PowerShell mis-decoded under Windows-1252 codepage,
   producing a second parse error. Em dash also violates the
   project-wide middle-dot convention (now formalised in MEMORY.md
   under "No em or en dashes").

Fix: replaced every em/en dash with middle dot in `scripts/` (5
files: `run_first_smoke.ps1`, `init_db.py`, `migrations/002_*.sql`,
`migrations/003_*.sql`) and `docs/operator/first-smoke-run-guide.md`
(32 dash occurrences across the runbook · ranges, prose, table cells).
`run_first_smoke.ps1` re-saved as UTF-8 with BOM (`utf-8-sig`) +
CRLF line endings so PowerShell reads non-ASCII correctly under the
default Windows-1252 console codepage.

Verified via `[System.Management.Automation.Language.Parser]::ParseFile`
which returned PARSE_OK with no errors. Smoke run can now begin.

MEMORY.md gained the "No em or en dashes" convention block: middle
dot (·) is the only permitted separator; .ps1 files must be saved as
UTF-8 with BOM. The convention is strict · no exceptions for numeric
ranges, prose pauses, or table cells.

No code module changes · no tests · no ADR amendments · 558 passed,
2 skipped stays.

### Step 16 follow-up · arc-link cascade fix · 2026-05-24

The first real smoke run (2026-05-23) reached the arc-link stage and
emitted 240 `arc_link_cluster_failed` warnings · all 240 clusters
tried to seed `arc_20260523_0001`. One orphan row landed in `arcs`
+ `arc_centroids` (the first attempt's INSERT succeeded · every
subsequent attempt hit duplicate-PK).

Root cause was a missed FK in the workaround in
`musahit/arcs/linker.py::_update_cluster_arc_id`. The workaround
snapshotted `cluster_articles` and `cluster_embeddings` and re-
inserted them around the cluster UPDATE · but `promotion_log`
(added in step 11 with `cluster_id REFERENCES clusters(id)`) was
never wired in. Every `UPDATE clusters SET arc_id = ?` raised; the
outer try/except caught it; BUT the arc-id counter was incremented
inside the success branch, so on failure it sat still. Next cluster
retried `arc_20260523_0001`. Cascade.

Fix scope (single commit on 2026-05-24):

- `musahit/arcs/linker.py::_update_cluster_arc_id` · snapshot ·
  DELETE · re-INSERT `promotion_log` alongside `cluster_articles`
  and `cluster_embeddings`.
- `musahit/arcs/linker.py::_update_cluster_arc_id_to_value` · same
  promotion_log extension; both helpers stay in lock-step.
- `musahit/arcs/linker.py::ArcLinker.run` · `seed_attempted` flag
  + counter advance moved to `finally`. Counter advances whether
  `_seed_arc` succeeds or raises · no more cascade even if a future
  child FK is missed.
- `musahit/arcs/linker.py::_seed_arc` · manual rollback. INSERT
  arc + arc_centroids first (required by `clusters.arc_id` FK),
  then attempt `_update_cluster_arc_id` inside try/except · on
  failure DELETE the just-inserted rows before re-raising. DuckDB
  auto-commits per statement so this is the manual equivalent of
  a transaction rollback.
- `tests/test_linker.py` · three regression tests:
  `TestPromotionLogPreservedAcrossArcUpdate` (round-trip),
  `TestSeedArcRollback` (orphan-free failure), and
  `TestCounterAdvancesOnSeedFailure` (no cascade on injected
  failures). Existing 10 tests stay green; new tests bring linker
  coverage to 13.
- `scripts/cleanup_orphan_arcs.py` · one-off operator script,
  targets `arc_20260523_0001` only, refuses to delete if the row
  has any linked clusters.
- `memory/MEMORY.md` · DuckDB FK Update Pattern entry extended
  with `promotion_log` in the "Applies to" list and a Maintenance
  rule (whenever a new child FK lands on a parent already in this
  list, every helper must be extended; counters in loops calling
  the helper must advance in `finally`, not inside the success
  branch).
- `docs/implementations/2026-05-24-arc-link-bug-fix.md` · standard
  Problem · Root cause · What changed · Why this shape · Verification ·
  Operator caveats per CLAUDE.md.

ADR-008 was NOT amended · the orphan-on-failure rollback is an
implementation safety measure for DuckDB's per-statement auto-commit,
not a semantic decision about the arc model. The /goal tripwire on
this was checked: the rollback semantics are not in ADR-008 scope.

Verified: `ruff check .` clean; `pytest tests/test_linker.py -q`
shows 13 passed (up from 10); full suite stays green.

Operator action before the next smoke run: `python scripts/cleanup_orphan_arcs.py`
to remove the one orphan from 2026-05-23.

### Step 16 follow-up · category normalization · 2026-05-24

The first smoke run also surfaced a Turkish-character-folding bug
in `WorkerResponse.category`. Qwen2.5 7B returns category strings
with Latin I in place of Turkish İ (e.g. `DIPLOMASİ` instead of
`DİPLOMASİ`) · pydantic's enum coercion rejects the value · the
classifier retries `max_retries` times · the cluster falls through
to the conservative `{UNCLASSIFIED, AMBIENT, low}` fallback. 2 of
139 clusters hit this path on 2026-05-23; the bug is systemic
across every Category value containing Turkish-specific characters
(`POLİTİKA`, `EKONOMİ`, `DİPLOMASİ`, `GÜVENLİK`).

Fix scope (one commit on 2026-05-24):

- `musahit/score/schema.py` · added `_tr_lower`,
  `_fold_for_matching`, `_CATEGORY_NORMALIZATION_MAP` (built
  programmatically from `Category` enum values, with import-time
  collision assertion), `_normalize_category` helper, and a
  `field_validator(mode="before")` on `WorkerResponse.category`
  that runs the fold lookup before enum coercion. Unknown values
  pass through unchanged so the classifier's retry path still
  fires for genuinely malformed input.
- `tests/test_score/__init__.py` + `tests/test_score/test_schema.py`
  · new test module with 27 tests: 8 canonical pass-throughs (all
  Category values), 1 exact bug reproduction (`DIPLOMASİ` →
  `DİPLOMASİ`), 4 ASCII folds, 5 lowercase folds, partial-match
  rejection, unknown rejection, empty rejection, fold-map
  invariant, helper round-trip, helper pass-through, and 3
  `parse_worker_response` integration cases that exercise the
  validator through the JSON-strip path.
- `memory/MEMORY.md` · "Turkish locale case folding" entry
  extended with the LLM-output normalization use case + a pointer
  to the canonical pattern in `_normalize_category` for any
  future Pydantic schema with Turkish-string-valued enum fields.
- `docs/implementations/2026-05-24-category-normalization.md` ·
  standard impl doc.

Scope discipline: WorkerResponse is currently the only pydantic
schema with a Turkish-enum field (verified via grep across
`musahit/`); WriterResponse, if it ever lands, would need the
same treatment. The /goal tripwire on "other model outputs need
similar normalization" was checked and cleared with that grep.

Pydantic 2.12.5 supports `field_validator(mode="before")` cleanly
· fold map has 8 distinct keys for 8 Category values, no
collisions (asserted at import + in tests). Verified ruff clean,
`pytest tests/ -q` shows 588 passed, 2 skipped (561 prior + 27
new).

### Step 16 follow-up · template placeholder echo fix · 2026-05-24

The 2026-05-23 smoke run revealed a third writer-stage bug: Trendyol
echoed the literal string `"[içerik buraya · şablon talimatlarına
bak]"` verbatim into AÇIK GELİŞMELER and AMBİYANS · DEFCON 5 instead
of rendering real content or the "(bugün öğe yok)" empty-state.
Three root causes, three fixes:

(a) `musahit/writer/prompt.py::_template_skeleton` used the same
literal placeholder string under every section · sections the model
understood got filled, ambiguous sections got the literal echoed.
Fix: extended `TemplateSection` dataclass with a
`prompt_instruction: str` field; populated all 8 sections with
Turkish guidance including the empty-state phrase
(`"(bugün öğe yok)"` or `"(bugün güncelleme yok)"` or
`"(bugün kapatılan yok)"`). Skeleton now renders each section's
prompt_instruction under the marker.

(b) `_clusters_data_block` had no AMBİYANS / DEFCON 5 bucket · the
section header always lacked data so the model had nothing to fold
in. Fix: added an AMBİYANS bucket using the already-existing
`BUCKET_AMBIENT = (5,)` from `payload.py`. Rendered as bullet list
of headlines with kaynak count, or the standard empty-state phrase
when no DEFCON 5 clusters are present.

(c) `musahit/writer/validator.py` didn't reject briefings containing
the literal placeholder · the bad output passed validation and got
written to disk. Fix: added a content check for the substring
`"[içerik buraya"` (the opening of the old placeholder · stable
against any future trailer tweak). On match: error
"briefing contains unfilled template placeholder · model echoed the
instruction text". The writer's existing retry loop will then
re-prompt with structured feedback.

Tests · 12 new across the three test files in `tests/test_writer/`:

- `test_template.py::TestPromptInstruction` (4) · every section has a
  non-empty, substantive prompt_instruction; no instruction contains
  the old placeholder; data-carrying sections include an empty-state
  phrase; section names remain unique.
- `test_prompt.py::TestTemplateSkeletonInstructions` (2) · skeleton
  contains each section's instruction; skeleton does NOT contain the
  old literal placeholder.
- `test_prompt.py::TestAmbientClusterRendering` (3) · AMBİYANS bucket
  renders bullet headlines; renders empty-state when no ambient
  clusters; full prompt with ambient payload stays under size budget.
- `test_validator.py::TestPlaceholderEchoRejected` (3) · briefings
  with the placeholder are rejected with the new error message;
  partial placeholder fragments are caught too; clean briefings
  (whose body text incidentally contains the word "içerik") still
  pass.

Tripwires checked:
- `BUCKET_AMBIENT` already exists in payload.py with the right value
  `(5,)`. No conflict. Just needed to use it.
- ADR-009 needs no amendment: `prompt_instruction` is implementation
  guidance to the LLM, not a semantic change to the template
  contract (sections, markers, ordering).
- The substring `"[içerik buraya"` appears only in the bug site
  (`musahit/writer/prompt.py`) and operator notes (`memory/operator-tasks.md`)
  · no test fixture uses it. Existing `_valid_skeleton()` in
  test_validator.py uses bare `"içerik"` which doesn't match.
- AMBİYANS bullet shape (headline + source count) matches ADR-009's
  "düşük öncelikli gündem" framing · no scope expansion needed.

Verified: `ruff check .` clean; `pytest tests/test_writer/ -q` shows
59 passed (47 prior + 12 new); full suite expected at 600 passed,
2 skipped (588 prior + 12 new).

### Step 16 follow-up · TTS silent-MP3 fix · 2026-05-24

The 2026-05-23 smoke run shipped a 44 KB silent placeholder MP3
instead of a real briefing.mp3. Root cause traced through two
compounding bugs:

(a) The fallback writer's `_render_open_arcs` dumped all 222 open
arcs into the AÇIK GELİŞMELER section with full `_render_arc`
blocks · 64,517 characters in one voiced chunk. Piper's 60s
per-chunk timeout (ADR-010) fired before that chunk finished
synthesizing.

(b) `Synthesizer._synthesise_chunks` had no per-chunk failure
isolation · the timeout exception bubbled all the way up through
the outer `try/except` in `run()`, the entire stage fell to the
placeholder path, and the real (synthesizable) chunks (header,
DEFCON 1-2, DEFCON 3, closing) never produced audio.

Two-layer fix (one commit):

(1) **ADR-009 amendment**: AÇIK GELİŞMELER · DEVAM EDEN TAKİP is
now split into two subsections. `### Öne Çıkanlar` carries the
top `VOICED_OPEN_ARCS_CAP = 10` arcs (sorted by
`(peak_defcon ASC, last_update_at DESC)`) as full `_render_arc`
blocks · this is the voiced subsection. `### Diğer Açık Hikayeler`
carries any remaining arcs as a one-line bullet list ·
visual-only, excluded from TTS scope. Target audio length drops
from 4-6 minutes to 3-4. When total arcs ≤ 10, only Öne Çıkanlar
renders (no Diğer subsection).

(2) **Per-chunk failure isolation in synthesizer**:
`_synthesise_chunks` now wraps each `piper.synthesize()` call in
try/except · per-chunk failures log a `tts_chunk_failed` warning
(index, chars, error type+message) and the next chunk is still
attempted. If at least one chunk succeeds the partial WAV list is
returned and a real MP3 ships. If every chunk fails the function
raises `ValueError("all chunks failed synthesis") from last_exc`
so the outer try/except still falls through to the silent
placeholder path (ADR-012 always-ships invariant). The `from
last_exc` chaining preserves the original exception in the
stderr traceback so smoke-run diagnostics remain useful.

Fix scope:

- `ADR-009-briefing-template.md` · added Amended block at top +
  full subsection-split rationale in the TTS scope section.
  `VOICED_OPEN_ARCS_CAP = 10` documented.
- `musahit/writer/fallback.py` · `_render_open_arcs` produces the
  Öne Çıkanlar / Diğer Açık Hikayeler split with the
  `(peak_defcon, -timestamp)` sort key. `_arc_sort_key` and
  `_render_arc_overflow_bullet` helpers. Falls back to
  `created_at` when `last_update_at` is None (ArcView has no
  `last_seen_at` field · /goal tripwire cleared with the
  fallback choice documented here).
- `musahit/writer/template.py` · AÇIK GELİŞMELER section's
  `prompt_instruction` rewritten to describe the split (in
  Turkish) so when Trendyol-LLM eventually produces the briefing
  it follows the same shape. Empty-state phrase preserved.
- `musahit/tts/extractor.py` · `_split_into_sections` tracks an
  `inside_open_arcs_overflow` flag · when current section is
  MARKER_OPEN_ARCS and we encounter `### Diğer Açık Hikayeler`,
  we stop appending lines to the voiced bucket for the rest of
  that section (Diğer marker itself dropped). Backward-compatible
  with old-shape briefings (no Diğer marker → behavior unchanged).
- `musahit/tts/synthesizer.py` · `_synthesise_chunks` wraps each
  call in try/except with per-chunk warning log; raises
  `ValueError(...) from last_exc` only when EVERY chunk fails.
- Tests · 6 new in `tests/test_writer/test_fallback.py`
  (TestOpenArcsSubsectionSplit covering split shape, sort order,
  None-date safety, overflow bullet format, validator
  preservation) + 5 new in `tests/test_tts/test_extractor.py`
  (TestOpenArcsSubsectionTruncation covering highlight kept,
  Diğer marker dropped, overflow excluded, decoy in other section
  doesn't truncate, old-shape backward compat) + 2 new in
  `tests/test_tts/test_synthesizer.py` (TestPerChunkResilience
  covering one-fail-other-succeed and per-chunk-loop-attempts-all
  semantics).

Tripwires checked:
- ArcView field is `last_update_at`, not `last_seen_at` · the
  /goal's "fall back to created_at" path is honored.
- ADR-009 amendment touches only the TTS scope subsection (plus
  the Amended block at the top) · no other sections of ADR-009
  modified.
- Current 2026-05-23 briefing.md's AÇIK GELİŞMELER shape
  (`### {headline} · {arc_id}` blocks, no Diğer marker) matches
  the legacy code path · extractor's backward compat test pins
  this so the existing briefing is still parseable.
- 2026-05-23 briefing.md is NOT being re-rendered · the operator
  audit trail is preserved (per the "leave bad output as-is for
  audit" discipline established in 2026-05-24 prior fixes).

Verified: `ruff check .` clean; `pytest tests/test_writer/
tests/test_tts/ -q` shows 152 passed, 1 skipped (139 prior + 13
new); full suite expected at 613 passed, 2 skipped (600 prior +
13 new). Diagnostic against `briefings/2026/05/23/briefing.md`:
old-shape extractor pulls all 222 arcs (64K chars in AÇIK
GELİŞMELER chunk · still fails Piper timeout under per-chunk
resilience because the bad chunk's failure is now isolated and
the other voiced chunks succeed · placeholder no longer ships).
The next smoke run produces the new split shape and AÇIK
GELİŞMELER stays well under 10K chars.

### Step 16 follow-up · date propagation fix · 2026-05-24

The 2026-05-23 smoke run produced its briefing under
`briefings/2026/05/23/` even though the operator launched the run
at 00:28 TR-local on 2026-05-24 (= 21:28 UTC on 2026-05-23). The
run_id was correctly `run_20260524` (CLI → `tr_local_date()`), but
the writer's `build_payload` derived the briefing date from
`pipeline_runs.started_at` (UTC stamped by `utcnow()` in the
orchestrator's `_upsert_run_row`). Two date-resolution paths that
agreed during the daytime UTC overlap but diverged across the
21:00-00:00 UTC window when TR is already on the next day.

Fix: make `target_date` a first-class parameter that flows from the
CLI through the orchestrator into the writer:

- `musahit/writer/payload.py::build_payload` now takes
  `target_date: date | None = None` (keyword-only). When provided,
  it directly drives `BriefingPayload.date`. When omitted, the
  legacy `started_at.date()` fallback fires · kept for backward
  compat with test fixtures that don't yet pass the kwarg.
- `musahit/writer/briefer.py::Briefer.__init__` accepts
  `target_date: date | None = None` (keyword-only). `Briefer.run`
  passes the stored value to `build_payload`. The Stage Protocol's
  `run(run_id)` signature stays unchanged · date is bound at
  construction time, not dispatch time. This avoids changing
  `stages.py` (tripwire cleared).
- `musahit/orchestrator.py::Orchestrator.run` accepts
  `target_date: date | None = None` (keyword-only). Stored on the
  orchestrator as `self._current_target_date` for the duration of
  the run. `DefaultStageFactory` is constructed with a closure
  (`get_target_date=lambda: self._current_target_date`) that the
  factory reads when building the Briefer · the indirection means
  a single orchestrator instance can be re-run with different
  dates without caching the wrong value.
- `musahit/pipeline.py::_cmd_run` passes the CLI-resolved
  `target_date` to `orchestrator.run`. CLI already computed it via
  `_resolve_date(args.date)` / `tr_local_date()`.

TTS synthesizer needs no change: its `_resolve_briefing_date`
queries `SELECT date FROM briefings ORDER BY generated_at DESC
LIMIT 1`. After the writer fix that row carries the correct
target_date, so TTS picks it up automatically (tripwire cleared).

Cluster IDs (`cl_YYYYMMDD_NNNN`) intentionally use the article's
news date · NOT the briefing date. Per the /goal tripwire, this is
expected behavior: a cluster about an event from 2026-05-23 should
keep its `cl_20260523_*` id even when surfaced in the
2026-05-24 briefing. Cluster ID logic untouched.

Tests · 9 new:

- `tests/test_writer/test_payload.py::TestTargetDatePropagation`
  (3) · target_date overrides started_at; legacy fallback
  preserved; midnight-crossing simulation (23:59 UTC → next-TR-day
  briefing).
- `tests/test_writer/test_briefer.py::TestTargetDateInBriefer`
  (3) · target_date drives markdown directory; briefings row date
  uses target_date; legacy fallback preserved.
- `tests/test_orchestrator.py::TestTargetDatePropagation`
  (3) · `Orchestrator.run` stores target_date for factory readback;
  `DefaultStageFactory` constructs Briefer with current target_date
  via the closure; omitting target_date leaves it None.

Verified: `ruff check .` clean; `pytest tests/test_writer/
tests/test_orchestrator.py -q` shows 90 passed (81 prior + 9 new);
full suite expected at 622 passed, 2 skipped (613 prior + 9 new).

Operator caveats: the 2026-05-23 briefing on disk is NOT
regenerated · the audit trail of the smoke-run bug stays intact.
The next smoke run will produce a briefing at the correct TR-local
date even when launched after 21:00 UTC.
