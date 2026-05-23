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

## Next

Step 8 · `musahit/ingest/poller.py` (FILE-PROTECTED at commit time) — the ingest
orchestrator. Iterates `SOURCES`, dispatches by `source.kind`, aggregates per-source
`IngestResult` into the `ingest_log` table. Uses the shared helpers; no new
abstractions beyond what steps 4-7 establish.
