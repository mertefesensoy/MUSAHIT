# Implementation: RSS ingester · `musahit/ingest/rss.py`

**Date** · 2026-05-23
**Author** · Claude Code (Mert Efe Şensoy directing)
**ADR refs** · ADR-003 · ADR-006 · ADR-012 · ADR-013

---

## ❯ Problem / Motivation

Build step 4 of 20 per BOOTSTRAP.md. Twenty of the thirty-seven sources in the locked
ADR-003 registry are `SourceKind.RSS`; without an RSS implementation roughly half of the
nightly news intake has no fetch path. The ingest stage feeds every downstream stage —
normalize, embed, cluster, score, arc-link, write — so the project cannot progress until
RSS is in place.

Three properties were non-negotiable:

1. **Failure isolation per ADR-012.** A single broken feed (timeout, 503, malformed XML)
   must not abort the run. The ingester returns a structured `IngestResult`; it never
   raises for expected failures.
2. **Deterministic dedup per ADR-006.** Re-running the same fetch must produce zero
   duplicate `raw_articles` rows. A feed that lists the same entry twice in one response
   (a real RSS pathology) must collapse to one row.
3. **Polite, async I/O per ADR-003.** `feedparser`'s built-in `urllib` fetcher would
   block the event loop, ignore our per-source `rate_limit_seconds`, and expose only a
   subset of the error modes we want to record. `httpx` handles HTTP; `feedparser`
   parses bytes.

---

## ❯ What Changed

| File | Description |
|---|---|
| `musahit/ingest/__init__.py` | Added `IngestResult` dataclass, `Ingester` `typing.Protocol`, and module-level `USER_AGENT` constant. Re-exports `Source` for convenience. |
| `musahit/ingest/rss.py` | New file. `RssIngester` class implementing `Ingester`, plus pure helpers `_article_id`, `_entry_url`, `_entry_feed_id`, `_canonical_timestamp`. |
| `tests/test_rss.py` | 14 tests across 8 classes covering the 9 required scenarios plus protocol compliance, User-Agent verification, deterministic id, persisted-row shape, and the canonical-timestamp tie-breaker. |
| `musahit/common/migrations.py` | Tiny incidental fix: replaced `try/except/pass` with `contextlib.suppress` so project-wide `ruff check` is clean (existing SIM105 violation surfaced by step-4's broader ruff run). |
| `docs/implementations/2026-05-23-rss-ingest.md` | This document. |

No FILE-PROTECTED file (`musahit/ingest/sources.py`, `musahit/score/defcon.py`,
`musahit/score/promotion.py`, any ADR) was modified.

---

## ❯ Implementation Approach

### `Ingester` `typing.Protocol`

The protocol is the seam between the orchestrator (which iterates `SOURCES` and dispatches
by `source.kind`) and the per-kind implementations (`rss`, `html`, `pdf`, `api`). It is
intentionally minimal:

```python
class Ingester(Protocol):
    async def fetch(self, source: Source) -> IngestResult: ...
```

A protocol — rather than an abstract base class — was chosen because:

- New ingester implementations (PDF, HTML, API) will live in separate files. ABC inheritance
  would force each to import a base class and accept its constructor signature, even when
  the dependencies differ (PDF needs `pdfplumber`, HTML needs `selectolax`, etc.).
- The orchestrator does duck typing already; an explicit `Protocol` documents the contract
  without imposing inheritance.
- Type-checkers can statically verify protocol conformance at the assignment sites
  (`ingester: Ingester = RssIngester(...)`).

### `IngestResult` dataclass

Frozen dataclass with three fields: `status`, `count`, and optional `error`. Defined in
`musahit/ingest/__init__.py` because it is the per-kind return type, not a domain enum;
it depends on `IngestStatus` from `musahit/common/types.py` but does not belong there.

### Fetch pipeline

```text
fetch(source)
  ├─ acquire httpx.AsyncClient (injected for tests, ephemeral for production)
  ├─ await client.get(url) with timeout + User-Agent
  │     ├─ httpx.TimeoutException  → IngestResult(status=TIMEOUT, error=…)
  │     └─ httpx.HTTPError         → IngestResult(status=HTTP_ERROR, error=…)
  ├─ response.status_code ≥ 400    → IngestResult(status=HTTP_ERROR, error=f"HTTP {code}")
  ├─ feedparser.parse(response.content)   ← sync, no network
  ├─ bozo=1 AND entries=[]          → IngestResult(status=PARSE_ERROR, error=…)
  ├─ persist(entries, fetched_at)   ← intra-fetch dedup + ON CONFLICT DO NOTHING
  └─ IngestResult(status=OK, count=rows_inserted)
```

### Persistence

Two dedup layers run inside `_persist`:

1. **Intra-fetch.** A Python `set` collects `feed_entry_id` values (Atom `<id>`, RSS 2.0
   `<guid>`, or `<link>` as fallback). The second occurrence of a given id is dropped
   before the row is appended to the insert batch. This matches real RSS pathologies
   where category-mirroring duplicates the same entry under multiple sections of one
   feed dump.
2. **Inter-fetch.** `ON CONFLICT (id) DO NOTHING` on the `raw_articles.id` primary key.
   This is DuckDB's spelling of SQLite's `INSERT OR IGNORE`. Re-running the ingester
   with the same response bytes yields zero new rows because the article id (see below)
   is deterministic.

The number of new rows reported back as `IngestResult.count` is computed as the delta of
`SELECT COUNT(*) FROM raw_articles` around the `executemany`. This is simpler and more
reliable than tracking the executemany return code (which DuckDB does not expose as
"rows affected" in the same shape SQLite does).

### Article-id design (deviation from ADR-006 comment)

ADR-006 schema comment: `id = hash(source_id, url, fetched_at)`.

Implementation: `id = sha256(source_id + "|" + url)`.

The comment includes `fetched_at`. If we followed it literally, every re-fetch would
generate a fresh id and `ON CONFLICT DO NOTHING` would never fire, violating goal
criterion (3): "Rerun against same feed bytes produces no duplicate article rows." The
inter-fetch dedup is a non-negotiable property of the build plan, so the `fetched_at`
ingredient is dropped.

Functionally `(source_id, url)` is the natural unique key for a feed entry — the same
entry from the same source has the same URL on every re-fetch. The hash exists to
produce a compact, opaque primary key value; the ingredients are what give the key its
semantics.

The ADR-006 comment is treated as descriptive prose, not a contract. A future ADR
amendment can update the comment when ADR-006 is next revised; no code or migration
change is required.

### Feed-provided entry id (intra-fetch dedup)

The goal asks the ingester to "store feed-provided entry id for intra-fetch dedup."
The interpretation: the feed entry id is used for the in-memory dedup decision during
one fetch; it is also serialized into the `headers` JSON column so the normalize stage
can audit it later. The dedup key itself does not need a dedicated column — the entry
id appears only in JSON to avoid a schema change. (Stop condition "ADR-006 schema needs
an additive column" was deliberately avoided this way.)

### Canonical timestamp

`_canonical_timestamp` takes `min(published_parsed, updated_parsed)`. Both are
`time.struct_time` tuples produced by feedparser; we coerce to UTC `datetime` and
ISO-8601-stringify. The result is stored in the `headers` JSON under
`canonical_published_at`; the normalize stage will read it to populate
`articles.published_at`.

"Earlier wins" matches the arc-linking intent in ADR-008: an arc cares about when an
article first became visible. `updated_at` can drift forward as a publisher silently
edits a piece, and treating those edits as "new content" would double-count the
article in arc windows. The earlier timestamp is also stable under future edits.

### HTTP error taxonomy

| `httpx` exception / response | Mapped status |
|---|---|
| `httpx.TimeoutException` (any subclass: connect/read/write/pool) | `TIMEOUT` |
| Other `httpx.HTTPError` (connect refused, DNS failure, protocol error, …) | `HTTP_ERROR` |
| `response.status_code ≥ 400` (after a successful round-trip) | `HTTP_ERROR` |
| `feedparser.bozo == 1` AND `entries == []` | `PARSE_ERROR` |
| `feedparser.bozo == 0` AND `entries == []` | `OK` with `count=0` |
| `feedparser.bozo == 1` AND `entries != []` | `OK` (partial parse with usable entries) |

The `bozo` semantics deserve a note: feedparser sets `bozo=1` for *any* well-formedness
issue — sometimes a single stray namespace, sometimes garbage HTML. Many production
feeds are technically non-well-formed but still produce usable entries. Treating
`bozo=1 with entries` as a hard failure would lose real signal; treating
`bozo=1 with no entries` as a parse error correctly captures "we got something but it
yielded nothing usable."

---

## ❯ Mathematical / Statistical Details

None. RSS ingest is a structural data-plumbing change with one helper (canonical
timestamp) that takes a `min()` over a two-element set.

---

## ❯ Design Decisions

**`httpx` for HTTP, `feedparser` for parsing.**
feedparser's bundled fetcher (`feedparser.parse(url)`) is synchronous, blocks the event
loop, ignores our per-source rate limits, and reports only a thin slice of HTTP errors.
Splitting fetch from parse gives us first-class control over timeouts, the User-Agent
header, retry logic (later, in the orchestrator), and structured error mapping.

**Stable article id (excludes `fetched_at`).**
See "Article-id design" above. The alternative — adding `fetched_at` per the ADR-006
comment — was rejected because it makes inter-fetch dedup structurally impossible.

**Headers JSON as the carrier for entry metadata.**
Adding columns to `raw_articles` for `feed_entry_id`, `canonical_published_at`,
`title`, etc., would require an ADR-006 amendment and a migration. Storing them in the
existing `headers TEXT` (JSON) column kept the schema untouched and let step 4 ship
without an ADR change. If the normalize stage later needs a column for query
performance, a focused additive migration can promote one field.

**Inject `httpx.AsyncClient` for tests; create ephemeral client in production.**
`RssIngester(conn, client=…)` accepts a pre-built client so `httpx.MockTransport` can
intercept every request without monkey-patching. In production the client is
constructed inside `fetch` per call, which is the right granularity for a nightly job
where each source fetches independently; the orchestrator can pass a pooled client
later if profiling justifies it.

**Two-statement persistence (count delta).**
DuckDB's `executemany` does not return a rows-affected count we can rely on for
`ON CONFLICT DO NOTHING`. Computing `COUNT(*)` before and after is one extra cheap
query in exchange for an unambiguous answer. The cost is negligible at our volume
(thousands of rows nightly, not millions).

**Skip entries without a `link`.**
Defensive; an entry with no URL cannot be re-fetched, deduplicated, or normalized.
Such entries are silently skipped. They are extremely rare in practice and logging
each one would be noise.

**Tests use a file-backed DuckDB in `tmp_path`, not `:memory:`.**
`init_db()` opens its own connection. To share state with the test, the migration
must persist somewhere both processes can see. A temp file is the simplest answer;
the test cleanup is implicit because `pytest.tmp_path` is per-test.

**Incidental ruff fix to `musahit/common/migrations.py`.**
Project-wide `ruff check` surfaced one pre-existing SIM105 violation. The file is not
FILE-PROTECTED, so the fix (try/except/pass → `contextlib.suppress`) is included to
keep the build green for future sessions. The behavior is identical.

---

## ❯ Verification

```powershell
# Lint
python -m ruff check .
# Expected: All checks passed!

# Tests
python -m pytest tests/ -q
# Expected: 136 passed, 1 skipped

# RSS-only tests
python -m pytest tests/test_rss.py -v
# Expected: 14 passed
```

Goal-criteria mapping:

| Criterion | Verification |
|---|---|
| (1) `Ingester` Protocol with `async fetch(source) -> IngestResult` | `musahit/ingest/__init__.py`; `TestProtocolCompliance` |
| (2) RssIngester uses httpx + feedparser, validates bozo/entries/status, writes raw bytes, hash id, intra-fetch dedup on feed entry id, ON CONFLICT DO NOTHING, earlier-of-published/updated, returns `IngestResult` | `musahit/ingest/rss.py`; `TestFetchOk`, `TestCanonicalTimestamp` |
| (3) All 9 required test cases | `tests/test_rss.py` (14 tests; the 9 required + protocol/UA/id/row-shape) |
| (4) Zero network calls in tests | All tests use `httpx.MockTransport`; no `httpx.AsyncClient()` is created without a transport |
| (5) `ruff check` zero violations | `python -m ruff check .` passes |
| (6) `pytest` exit zero | 136 passed, 1 skipped |
| (7) No FILE-PROTECTED files modified | `git status` (or equivalent) confirms; `sources.py`, `defcon.py`, `promotion.py`, ADRs untouched |
| (8) Implementation doc | This file |

---

## ❯ Operator notes

- **Pending-URL sources.** Seven sources in the registry have unverified URLs
  (`anadolu`, `t24`, `medyascope`, `dw_tr`, `voa_tr`, `reuters_tr`, `kap`). They will
  return `HTTP_ERROR` on the first nightly run; expected, not a regression. Verify and
  update URLs in `sources.py` before treating their failure as a real outage.
- **`reuters_tr` filtering.** The configured URL is the global Reuters world-news feed,
  not Turkey-specific. The normalize stage (step 5) must filter for Turkey-topic
  articles; the ingester writes everything as it arrives.
- **`bloomberg_ht` band.** ADR-013 reclassified this source from `INTERNATIONAL` to
  `CENTRIST`. The ingester does not care; the band is recorded only in the `sources`
  table and read by the promotion stage.
- **Atom vs RSS 2.0.** Both work via the same code path; feedparser normalizes them.
  NTV's feed is Atom (per the comment in `sources.py`); no special-casing needed.
- **HNSW indices.** Unrelated to ingest, but worth knowing: the test fixture runs with
  `load_vss=False`, so HNSW indices are not created in tests. Production must run with
  `load_vss=True` (the default) for the embed stage to perform.
- **DuckDB `INSERT OR IGNORE` syntax.** The implementation uses `ON CONFLICT (id) DO
  NOTHING`. DuckDB also accepts `INSERT OR IGNORE` as a synonym; the explicit
  `ON CONFLICT` spelling matches the existing upsert pattern in `sources.py` and is
  unambiguous about which key triggers the conflict.

---

## ❯ Related Docs

- BOOTSTRAP.md — build step 4 of 20
- ADR-003 — source registry; defines `Source` dataclass, bands, RSS as the preferred
  fetch method for `Tier.NEWS`
- ADR-006 — storage; defines the `raw_articles` schema this code writes to
- ADR-012 — failure isolation; defines the `IngestResult` contract and the per-source
  isolation model
- ADR-013 — source registry amendments (`bloomberg_ht`, `x_stub`)
- `docs/implementations/2026-05-22-init-db.md` — step 2, schema + migration runner
- `docs/implementations/2026-05-22-common-layer.md` — step 1, shared types and config
