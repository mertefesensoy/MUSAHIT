# Implementation: HTML scrape ingester · `musahit/ingest/html.py`

**Date** · 2026-05-23
**Author** · Claude Code (Mert Efe Şensoy directing)
**ADR refs** · ADR-003 · ADR-006 · ADR-012 · ADR-013 · ADR-014 · ADR-015

---

## ❯ Problem / Motivation

Build step 5 of 20. Nine sources in the locked registry are `SourceKind.HTML`
(ap_tr, tcmb, bist, tuik, tbmm, cumhurbaskanligi, anayasa_mahkemesi, yargitay,
danistay) — primary-feed government and judicial sources plus AP's Turkey hub.
None of them publish RSS, so without an HTML ingester their nightly intake is zero
and the primary-source override path in ADR-005 starves.

Three properties carry over from step 4 (RSS) without exception:

1. **Failure isolation per ADR-012.** A listing-page outage must not cascade into
   the rest of the run; a single broken article page must not abort the source.
2. **Cross-fetch dedup per ADR-014.** Same `article_id` formula as RSS — the
   shared `musahit.common.ids.article_id` helper.
3. **Typed metadata per ADR-015.** `canonical_timestamp` to the typed column;
   HTML-specific knobs (which selector matched, which timestamp-extraction
   method fired) stay in `headers` JSON. `feed_entry_id` is always `NULL` for
   HTML — there is no source-native id to record.

Two properties are new to HTML:

- **Two-phase fetch.** Listing page first, then a polite per-article fetch
  with `source.rate_limit_seconds` between hits.
- **No author of canonical_timestamp.** Most pages do not put a clean
  ISO timestamp in `<meta>`; the four-step extraction chain trades coverage
  for predictability.

---

## ❯ What Changed

| File | Description |
|---|---|
| `musahit/ingest/html_selectors.py` | New file. `SelectorConfig` frozen dataclass + `SELECTORS` dict with 9 placeholder entries (all kind=HTML sources in the registry). |
| `musahit/ingest/html.py` | New file. `HtmlIngester` implementing `Ingester`, plus the pure helpers `_try_jsonld` / `_try_meta` / `_try_turkish_regex` and the public `extract_canonical_timestamp`. |
| `tests/test_html.py` | 16 tests across 9 classes covering all 7 required scenarios plus four chain-specific timestamp tests and an empty-listing edge case. |
| `memory/build-progress.md` | Step-5 entry; "Next" now points at step 6. |
| `docs/implementations/2026-05-23-html-ingest.md` | This document. |

No FILE-PROTECTED file (`musahit/ingest/sources.py`, `musahit/score/defcon.py`,
`musahit/score/promotion.py`, any ADR) was modified. `pyproject.toml` already
declared `selectolax>=0.3`; no dependency changes were needed.

---

## ❯ Implementation Approach

### The `SelectorConfig` pattern

Each HTML source is described by one frozen dataclass:

```python
@dataclass(frozen=True)
class SelectorConfig:
    listing_selector: str           # scope for article-link search
    article_link_selector: str      # <a> elements within the scope
    title_selector: str | None = None
    body_selector: str | None = None
    published_selector: str | None = None
```

Required fields (`listing_selector`, `article_link_selector`) are everything the
ingester needs in phase 2 (URL extraction). The optionals separate three
concerns:

- `title_selector` — ingester uses it to put a display title into `headers` JSON;
  falls back to `<title>` if absent.
- `body_selector` — **reserved for the normalize stage**; the ingester does not
  read it. Lives here so per-source tuning has one home.
- `published_selector` — narrows the Turkish-regex step of the
  canonical-timestamp chain to text inside the matched element. Keeps the chain
  at four steps (per the build-plan tripwire) by *tuning* step 3 rather than
  *adding* a step.

The `SELECTORS` dict maps `source.id` to `SelectorConfig`. The ingester accepts a
`selectors=…` constructor argument so tests inject a known-good config without
mutating module state. Production code uses the module-level dict.

### Phase 1: listing fetch

`httpx.AsyncClient.get` with the project-wide User-Agent `MUSAHIT/0.1` and the
configurable `timeout_seconds`. The mapping from `httpx` failure → IngestResult
status is identical to RSS:

| `httpx` exception / response | Mapped status |
|---|---|
| `httpx.TimeoutException` (any subclass) | `TIMEOUT` |
| Other `httpx.HTTPError` (connect refused, DNS fail, …) | `HTTP_ERROR` |
| `response.status_code ≥ 400` | `HTTP_ERROR` |
| Listing parse exception (selectolax/Python error) | `PARSE_ERROR` |

A listing-phase failure returns immediately; **no per-article fetches are
attempted**. The `TestListingFailure` test pins this — it observes that exactly
one URL (the listing URL) is dispatched to the mock transport.

### Phase 2: URL extraction and dedup

`selectolax.parser.HTMLParser` parses the listing bytes. The ingester walks
`tree.css(listing_selector)` (scopes) and then `scope.css(article_link_selector)`
(links within each scope), reading `href` attributes. URLs are resolved
against the listing URL with `urllib.parse.urljoin` so relative hrefs work.

Order-preserving dedup (`list(dict.fromkeys(urls))`) collapses duplicates that
appear in the listing (often the same story shows in two listing rows because of
category mirroring). The test `TestUrlDedup` pins this by feeding a listing with
the same URL twice and asserting exactly one fetch for that URL.

### Phase 3: per-article fetch + persistence (failure isolation)

A loop over the deduplicated URL list, with these properties:

- **Rate limiting.** `await self._sleep(source.rate_limit_seconds)` *between*
  fetches (not before the first). N URLs → N-1 sleeps. The sleep callable is
  injected so `TestRateLimit` asserts on call count and arguments without
  blocking the test.
- **Per-article HTTP failure.** Timeouts, transport errors, and `status >= 400`
  log a warning and `continue` to the next URL.
- **Per-article parse failure.** `_persist_article` wraps the parse + insert; if
  anything raises (selectolax, JSON decoder, DuckDB constraint surprise) the
  caller logs and continues. The `TestPerArticleParseError` test monkeypatches
  `HTMLParser` to raise on a known-bad article and verifies the others still land.
- **Inserted count.** `IngestResult.count` is `COUNT(*) FROM raw_articles`
  delta around the loop — matches RSS's pattern. This naturally excludes both
  failures (no row written) and re-fetches (suppressed by `ON CONFLICT DO NOTHING`).

### The canonical-timestamp chain

Four steps, in order, the first success wins:

1. **JSON-LD.** Scan every `<script type="application/ld+json">`, JSON-decode,
   recursively walk the structure for `datePublished` / `dateCreated` /
   `dateModified` keys, ISO-parse the first usable value.
2. **Meta tags.** Try ten well-known meta-tag attribute/value pairs (e.g.
   `property=article:published_time`, `name=datePublished`,
   `itemprop=datePublished`); take the first `content` that ISO-parses.
3. **Turkish regex.** Search either the whole `<body>` text or — if
   `published_selector` is set — only text inside the matched element(s) for
   `DD MonthName YYYY` (Turkish month names) or `DD.MM.YYYY`.
4. **`fetched_at` fallback.** When nothing else matched.

The chain returns `(datetime, method_name)`. Every non-fallback datetime passes
through `musahit.common.time.to_utc_naive` so tz-aware inputs become naive UTC
without silent local-time shift. The fallback uses `fetched_at` directly, which
is already naive UTC (provided by `musahit.common.time.utcnow`).

The method name (`"json-ld"` / `"meta"` / `"turkish-regex"` / `"fetched-at"`) is
written into the row's `headers` JSON under `canonical_timestamp_method`. The
operator can audit how each source's timestamps are being derived without
opening the raw blob.

### Persistence shape

Same `raw_articles` columns RSS writes, with HTML-specific differences:

| Column | HTML value |
|---|---|
| `id` | `article_id(source.id, article_url)` via the shared helper |
| `feed_entry_id` | `NULL` — no source-native id for HTML pages |
| `canonical_timestamp` | output of the four-step chain |
| `headers` | `{title, canonical_timestamp_method, etag, last_modified, selector_listing, selector_article_link}` |
| `raw_content` | the article page bytes (not the listing bytes) |

Each article is INSERT-OR-IGNORE'd individually rather than batched, because
HTML failures are per-article — batching would force an all-or-nothing
transaction or a complex partial-rollback dance. The cost is N executemany-less
inserts per source; at this volume it does not matter.

---

## ❯ Mathematical / Statistical Details

None. The chain logic is a straight cascade of optional parsers; the only
ordering decision is "earlier step wins," which is documented above.

---

## ❯ Design Decisions

**Per-source selectors in one file vs per-source extractor functions.**
A function-per-source pattern (`extract_ap_tr(html) -> ...`) was the alternative.
Selectors-as-data is more honest about how uniform the work actually is — every
source needs the same shape of CSS-selector tuning, just with different
strings — and lets the operator edit selectors without touching Python. If a
source's structure genuinely deviates from the SelectorConfig model, that's a
build-plan tripwire and an ADR amendment.

**`published_selector` tunes step 3, does not add a step.**
The build plan caps the canonical-timestamp chain at four universal steps. A
per-source preempt step would be a fifth. Keeping per-source preference as a
*scope hint* inside step 3 preserves the cap and concentrates per-source
weirdness in one place.

**Dependency injection over module-level patching for tests.**
`HtmlIngester` accepts `client`, `selectors`, and `sleep` constructor arguments.
Production callers ignore them; tests get clean knobs without monkeypatching
module state across test files. The pattern matches `RssIngester` from step 4.

**Per-article INSERT instead of executemany.**
RSS does a single `executemany` because all entries are parsed in-process
before persistence. HTML cannot — each article is a separate HTTP fetch and
each can fail independently. Streaming inserts give the simplest failure-
isolation story; the perf cost is negligible at sub-100-article-per-source
volumes.

**`feed_entry_id` always NULL for HTML.**
ADR-015 made `feed_entry_id` nullable specifically for this case. We do not
synthesize a "feed entry id" from the article URL because the article id
already encodes the URL — duplicating would just mean carrying the same data
twice with the same semantics.

**`SKIPPED` status for "no SelectorConfig found".**
If a `SourceKind.HTML` source has no entry in `SELECTORS` (which should not
happen in production but is possible during step-5 incremental rollout), the
ingester returns `IngestStatus.SKIPPED` with a clear error message instead of
silently doing nothing or raising. Matches the ADR-012 enum's intent for
"intentionally not run."

---

## ❯ Verification

```powershell
# Lint (project-wide)
python -m ruff check .

# HTML-only tests
python -m pytest tests/test_html.py -v

# Full suite, no regressions
python -m pytest tests/ -q
# Expected: 167 passed, 1 skipped
```

Goal-criteria mapping:

| Criterion | Verification |
|---|---|
| (1) Two-phase fetch, async httpx + UA, selectolax, per-source selectors, 4-step chain, URL dedup, shared article_id, INSERT OR IGNORE, NULL feed_entry_id | `musahit/ingest/html.py`; `TestSuccessfulTwoPhase`, `TestUrlDedup`, `TestCanonicalTimestamp.*` |
| (2) `html_selectors.py` with required dataclass and ≥ 8 placeholder entries | `musahit/ingest/html_selectors.py`; nine entries (all HTML sources in registry) |
| (3) 7 specific test scenarios | `tests/test_html.py` — 16 tests across 9 classes |
| (4) Zero network in tests | All `httpx.AsyncClient` instances built via `_make_client` use `httpx.MockTransport` |
| (5) ruff clean | `python -m ruff check .` passes |
| (6) pytest exit zero | 167 passed, 1 skipped — no regressions |
| (7) FILE-PROTECTED untouched | `sources.py`, `defcon.py`, `promotion.py`, ADRs unchanged |
| (8) This document | ✓ |

---

## ❯ Selector verification status

All nine entries in `SELECTORS` are first-pass placeholders. None were
verified against a live fetch during step 5.

| Source ID | URL | Selector status |
|---|---|---|
| ap_tr | apnews.com/hub/turkey | TODO — placeholder (`main` + `a.PagePromo-title, a[data-key='card-headline']`) |
| tcmb | tcmb.gov.tr | TODO — placeholder |
| bist | borsaistanbul.com | TODO — placeholder |
| tuik | tuik.gov.tr | TODO — placeholder |
| tbmm | tbmm.gov.tr | TODO — placeholder |
| cumhurbaskanligi | tccb.gov.tr | TODO — placeholder |
| anayasa_mahkemesi | anayasa.gov.tr | TODO — placeholder |
| yargitay | yargitay.gov.tr | TODO — placeholder |
| danistay | danistay.gov.tr | TODO — placeholder |

The placeholders represent defensible guesses from a brief landing-page
inspection. The operator's first nightly run will surface selectors that miss;
each is a single-line edit. Leaving the dict empty was rejected because it
would block step 5 from shipping and leave step 6+ (PDF, Reddit) waiting for
operator verification of nine unrelated sites.

---

## ❯ Operator notes

- **First-night failures are expected.** The placeholder selectors will surface
  as `IngestStatus.OK` with `count=0` (listing parsed, no links matched) or as
  `PARSE_ERROR` (listing selector didn't match anything). Both are diagnosable
  from `ingest_log.error_detail`; neither aborts the nightly run.
- **Selector tuning is a one-line edit per source.** No code change, no migration,
  no test update needed. The dict structure is stable.
- **`SelectorConfig.body_selector` is reserved for step 9 (normalize).** The
  ingester does not read it. Listing it in this file keeps per-source tuning
  in one place; future readers should not be confused by its presence.
- **Bist `kind=HTML`** in the registry, but ADR-003 left a note that BIST's
  market-data section has no public RSS. The selector entry is a placeholder
  pointing at the announcements page. If BIST's actual structure differs
  fundamentally (e.g. an SPA where listing is JavaScript-rendered) this is a
  build-plan tripwire — surface it; do not silently switch to a headless
  browser.
- **No `kind=HTML` source is FILE-PROTECTED** in `sources.py`. Adding or
  removing HTML sources is an `sources.py` edit which already goes through ADR
  amendment.

---

## ❯ Deviations from the goal spec

None. The chain stayed at four steps; URL dedup happened before per-article
fetches; `feed_entry_id` is NULL for HTML; the helpers `article_id`,
`utcnow`, `to_utc_naive` are imported from their shared modules; the
INSERT OR IGNORE pattern matches RSS; all 7 required test scenarios are
covered with at least one test each.

---

## ❯ Related Docs

- BOOTSTRAP.md — build step 5 of 20
- ADR-003 — source registry; HTML scrape strategy and rate-limit convention
- ADR-006 — storage; defines `raw_articles` schema this code writes to
- ADR-012 — failure isolation; defines `IngestResult` contract per-stage
- ADR-013 — source registry amendments (irrelevant to HTML sources but cited
  for completeness per the goal references)
- ADR-014 — article id formula (shared helper used here)
- ADR-015 — typed metadata columns (this code writes the typed columns and
  the JSON `headers`)
- `docs/implementations/2026-05-23-rss-ingest.md` — step 4 (same-pattern
  reference implementation; same-day ADR-014/015 amendment notes apply)
- `docs/implementations/2026-05-22-init-db.md` — step 2; schema + migration runner
- `docs/implementations/2026-05-22-common-layer.md` — step 1; shared types/config
