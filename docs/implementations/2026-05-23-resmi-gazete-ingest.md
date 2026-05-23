# Implementation: Resmî Gazete PDF ingester · `musahit/ingest/resmi_gazete.py`

**Date** · 2026-05-23
**Author** · Claude Code (Mert Efe Şensoy directing)
**ADR refs** · ADR-003 · ADR-006 · ADR-012 · ADR-014 · ADR-015

---

## ❯ Problem / Motivation

Build step 6 of 20. The Resmî Gazete (T.C. Official Gazette) is the
operator's single most important primary source: every law, presidential
decree, regulation, communiqué, appointment, and court decision in Türkiye
appears here on the day it becomes effective. ADR-005's promotion ceiling
treats Gazette content as `PRIMARY_GOV` — it bypasses the cross-band
corroboration requirement and ships into the briefing directly.

The Gazette is also structurally unlike everything ingested so far. It is
**one PDF per day** that contains **many independent items** — the previous
RSS and HTML ingesters mapped one HTTP fetch to one row (or one listing to N
already-separated articles). Resmî Gazete maps one HTTP fetch to N parsed
sub-documents. The Ingester Protocol shape (single `fetch(source) → IngestResult`)
is preserved; the internals are PDF retrieval + text extraction + heuristic
section/item parsing.

Three more wrinkles motivated extra care:

1. **Date probing.** The Gazette is posted late evening Türkiye time for the
   *next day's* date. A pipeline starting at 01:00 TRT usually finds today's
   URL live, but delays/holidays mean we may need to fall back to yesterday's.
2. **Mükerrer supplements.** On rare days the Gazette publishes one or more
   numbered supplements (`YYYYMMDD-1.pdf`, `-2.pdf`, …) alongside the main
   edition. Each supplement carries additional items.
3. **No native URLs.** Items inside the PDF do not have individual URLs.
   The article id formula (ADR-014) needs a stable per-item input, so the
   ingester mints a synthetic URL — see "Synthetic URL convention" below.

---

## ❯ What Changed

| File | Description |
|---|---|
| `musahit/ingest/gazette_parsing.py` | New. Pure parser: `GazetteSection`, `GazetteItemType`, `GazetteItem`, `parse_gazette_pdf`, `parse_gazette_pages`. |
| `musahit/ingest/resmi_gazete.py` | New. `ResmiGazeteIngester` implementing the `Ingester` Protocol. |
| `tests/test_gazette_parsing.py` | 29 tests across 7 classes (pure-string parser + 2 PDF integration tests against the fixture). |
| `tests/test_resmi_gazete.py` | 9 tests across 7 classes (HTTP/dedup/parse-error/Mükerrer/idempotence + a parser-injection test). |
| `tests/fixtures/resmi_gazete/_generate.py` | One-shot fixture-generator script (uses `reportlab`; not a project dependency). |
| `tests/fixtures/resmi_gazete/sample_gazette.pdf` | Hand-crafted 2-page PDF with 4 items across 3 sections (KANUN, CUMHURBAŞKANLIĞI KARARNAMESİ, MAHKEME KARARI, TEBLİĞ). |
| `tests/fixtures/resmi_gazete/mukerrer_supplement.pdf` | Hand-crafted single-item supplement (YÖNETMELİK). |
| `tests/fixtures/resmi_gazete/corrupted.bin` | Garbage bytes pretending to be a PDF; pdfplumber rejects. |
| `docs/implementations/2026-05-23-resmi-gazete-ingest.md` | This document. |

No FILE-PROTECTED file (`sources.py`, `defcon.py`, `promotion.py`, any
ADR) was modified. `pyproject.toml` already declared `pdfplumber>=0.11`; no
runtime dependency change was needed. `reportlab` is **not** added to the
project dependencies — it is used once to generate the fixture binaries and
committed PDFs serve all subsequent test runs.

---

## ❯ Implementation Approach

### Synthetic URL convention

```
resmi-gazete://YYYY-MM-DD/<ITEM_TYPE_NAME>/<reference_or_synthetic_id>
```

Examples:

- `resmi-gazete://2026-05-23/LAW/7460`
- `resmi-gazete://2026-05-23/PRESIDENTIAL_DECREE/152`
- `resmi-gazete://2026-05-23/COURT_DECISION/2026/123`
- `resmi-gazete://2026-05-23/REGULATION/item-2-p2` (no reference; synthetic)

The article id is `article_id(source.id, synthetic_url)` per ADR-014. The
formula does not care about URL scheme; any string that uniquely identifies
the item is fine. The real HTTP URL of the PDF is preserved in
`headers.real_pdf_url`.

**Why this scheme:**

- *Stable.* Same date + type + reference → same id across reruns of the same
  Gazette edition. INSERT OR IGNORE handles dedup like every other ingester.
- *Hierarchical.* The synthetic URL doubles as a human-readable identifier
  when the operator inspects rows in the DB.
- *Disambiguating.* Two items with different types but the same reference
  (e.g. a KANUN with `7460` and a REGULATION with `7460`) get distinct ids
  because the type segment differs.

When `reference_number` is empty (parser failed to extract one), the ingester
falls back to `item-<index>-p<page_start>`. Index is the 1-based position
within the parser's output for that PDF; combined with `page_start` it is
both stable and human-readable. `feed_entry_id` is `NULL` in this case (no
*real* source-native id), in line with ADR-015's nullable contract.

### Section / item taxonomy

```python
class GazetteSection(StrEnum):
    EXECUTIVE     = "YÜRÜTME VE İDARE BÖLÜMÜ"
    JUDICIAL      = "YARGI BÖLÜMÜ"
    ANNOUNCEMENT  = "İLAN BÖLÜMÜ"

class GazetteItemType(StrEnum):
    LAW                 = "KANUN"
    PRESIDENTIAL_DECREE = "CUMHURBAŞKANLIĞI KARARNAMESİ"
    REGULATION          = "YÖNETMELİK"
    COMMUNIQUE          = "TEBLİĞ"
    APPOINTMENT         = "ATAMA KARARI"
    COURT_DECISION      = "MAHKEME KARARI"
    OTHER               = "DİĞER"
```

These are the *closed* sets that the parser detects. The build-plan tripwire
prohibits silent enum expansion — if real Gazette content surfaces a type
that does not fit, the operator surfaces the gap and a follow-up ADR
amends the enum. `OTHER` is reserved as the sentinel for future heuristics
that explicitly bucket unmatched markers; the current parser does not
emit `OTHER` (an unmatched header line is treated as ordinary content and
the parser keeps reading until the next known marker).

### Today/yesterday fallback

```python
target_date = explicit_arg or _tr_today()
for candidate_date in (target_date, target_date - timedelta(days=1)):
    response_or_error = await self._http_get(client, _build_pdf_url(candidate_date, 0))
    if response_or_error is a successful response:
        proceed with this date
        break
```

The Türkiye-local "today" is computed as `(utcnow() + timedelta(hours=3)).date()` —
Türkiye is UTC+3 year-round, no DST since 2016, so the offset is hard-coded.
Both candidates 404 → `IngestResult(status=HTTP_ERROR)`.

### Mükerrer probing

After the main edition succeeds, the ingester probes
`YYYYMMDD-1.pdf`, `-2.pdf`, … up to `_max_mukerrer` (default 5). The first
404 stops the probe. Each successful supplement is parsed independently;
the parse output's rows carry `mukerrer = N` in `headers` JSON so the
operator can distinguish them. A supplement parse failure is logged but
does **not** discard the main edition's rows.

### Parse-error policy

Three layers, three behaviors:

| Layer | Failure mode | Behavior |
|---|---|---|
| HTTP (listing-equivalent) | TimeoutException, HTTPError, status ≥ 400 | Probe next candidate date; if all candidates fail → `HTTP_ERROR` (or `TIMEOUT` if that was the last failure). |
| PDF (main edition) | pdfplumber raises | Translate to `PARSE_ERROR`. The whole source fails for this run. |
| PDF (Mükerrer supplement) | pdfplumber raises | Log, skip the supplement, keep the main edition's data. |

The main-PDF parse error is the "source failed" outcome because that's what
the operator cares about: today's Gazette could not be processed. The
supplement failure is per-supplement: one broken supplement should not
unwind a successful main edition.

### Persistence shape (per item)

| Column | Resmî Gazete value |
|---|---|
| `id` | `article_id("resmi_gazete", synthetic_url)` |
| `source_id` | `"resmi_gazete"` |
| `url` | the synthetic URL |
| `fetched_at` | naive UTC (`utcnow()`) — single value across all items of one run |
| `raw_content` | the PDF bytes — yes, duplicated across items of the same PDF; ADR-006 stores per-row blobs and that is what we have |
| `content_type` | `"application/pdf"` |
| `headers` | JSON: `{section, item_type, page_start, page_end, real_pdf_url, reference_number, mukerrer, title}` |
| `fetch_status_code` | `200` |
| `feed_entry_id` | the parsed reference number, or `NULL` if empty |
| `canonical_timestamp` | midnight UTC of the publication date, normalized through `to_utc_naive` |

The `raw_content` duplication across rows is a known footprint cost
(N copies of the PDF). Acceptable for v0.1 at the Gazette's typical size
(~5-30 MB). If it becomes a problem the right fix is a future
`raw_articles_blobs` table indexed by content hash; **not** changing the
per-item row shape.

---

## ❯ Mathematical / Statistical Details

None. The parser is a string-matching state machine; the date math is
simple subtraction.

---

## ❯ Design Decisions

**Two-layer parser API (`parse_gazette_pdf` vs. `parse_gazette_pages`).**
The public PDF entry point is a thin wrapper that opens bytes with
pdfplumber, extracts text per page, and delegates structural parsing to
`parse_gazette_pages(pages, publication_date)`. Tests drive the latter
with synthetic strings — 27 of 29 parser tests need no PDF at all. The
two integration tests against the fixture prove the pdfplumber wrapper
works.

**Inject `parse_pdf` into the ingester constructor.**
Matches the html.py pattern. Tests can pass a mock parser that returns
canned `GazetteItem` lists; the HTTP/dedup/persistence logic is exercised
without depending on a real PDF for every scenario. The real
`parse_gazette_pdf` is the production default.

**Synthetic URL instead of an extra column.**
The build-plan tripwire warned: "synthetic URL scheme conflicts with the
article_id helper or URL handling elsewhere". The chosen scheme (a custom
`resmi-gazete://` URI) does not conflict because:

- `article_id(source_id, url)` treats `url` as an opaque string — no URL
  validation.
- `raw_articles.url` is `TEXT NOT NULL` with no format constraint.
- No other code in the project parses the URL value out of `raw_articles`
  for protocol/scheme; the normalize stage reads it only when fetching
  the *real* page, and Gazette items have their real URL in
  `headers.real_pdf_url` (the normalize stage's contract is per-source
  anyway).

No schema additions were needed (tripwire negative).

**Items default to `EXECUTIVE` if seen before any section marker.**
Real Gazette PDFs always open with the executive section header on page 1,
but the operator's first run may surface a PDF whose first page is a
cover sheet (no section marker until page 2). Defaulting to EXECUTIVE
keeps a too-early item in the right section in practice and is documented
in the dataclass docstring.

**`reference_number` is empty string, not `None`, in `GazetteItem`.**
Frozen dataclass with `str` field; empty is the empty case. The ingester
maps `""` to `NULL` for `feed_entry_id` at write time. Keeping the
parser's data model `str`-only avoids `Optional` everywhere; the
nullable boundary lives at the storage layer.

**`reportlab` for fixture generation, not for tests.**
The fixture PDFs are committed binaries. `reportlab` only needs to be
installed when an operator wants to regenerate them. Adding `reportlab`
to `[project.optional-dependencies] dev` was considered but rejected —
test runs do not need it, and a checked-in 4-5 KB PDF is more reliable
than depending on a heavy library to produce identical bytes on every
machine.

---

## ❯ Heuristic edge cases the parser handles

The parser is heuristic; the test suite pins specific behaviors but real
Gazettes will surface new edge cases over time. Caught here:

- **Composite court references.** `Esas No: 2026/45 Karar No: 2026/123` →
  parser prefers `Karar No` (the decision number, which is what dedups
  re-published court rulings). The regex for `Karar No` is checked
  before `Karar Sayısı` so the composite case is handled.
- **Marker substring collisions.** `CUMHURBAŞKANLIĞI KARARNAMESİ` and
  `MAHKEME KARARI` share no characters in problematic positions; the
  item-type list is ordered so longer-marker membership is irrelevant
  for current types. If a future Gazette uses a marker that is a prefix
  of another, the order needs updating.
- **Cover-page noise.** Text before the first section/item marker is
  silently dropped — the parser only starts collecting content after an
  item marker fires.
- **Mid-item page breaks.** `page_end` tracks the highest page the item's
  content was last seen on; multi-page items have `page_start < page_end`.

---

## ❯ Operator caveats

- **Selectors WILL miss.** First-night operation will produce some items
  that hit `OTHER` (currently unreachable as designed; will become
  reachable if a future tuning bucket unmatched markers). It will also
  produce items with empty `reference_number` (synthetic id used instead).
  Both are recoverable; neither aborts the run.
- **Mid-month edge cases.** Gazette numbering occasionally resets (e.g.,
  the Karar Sayısı sequence for one decree type may differ from another).
  The article id is `(source_id, synthetic_url)` so as long as
  `(date, item_type, reference)` is unique within the day, dedup works.
  If two items share `(date, item_type, reference)` (extremely rare; would
  indicate a Gazette typo), one of them is silently dropped by INSERT OR
  IGNORE. The operator can spot this in `ingest_log`: `count` will be
  N-1 instead of N for that day.
- **Mükerrer cap.** `max_mukerrer=5` is the default; the operator can
  raise it via the constructor if a future edition needs more.
- **Real-URL traceability.** Every row's `headers.real_pdf_url` carries
  the exact HTTP URL used to fetch the PDF. The operator can paste it
  into a browser to verify what was processed.

---

## ❯ Verification

```powershell
# Lint
python -m ruff check .                # All checks passed!

# Step-6 tests
python -m pytest tests/test_gazette_parsing.py tests/test_resmi_gazete.py -v
# Expected: 38 passed

# Full suite — no regressions
python -m pytest tests/ -q
# Expected: 205 passed, 1 skipped
```

Goal-criteria mapping:

| Criterion | Verification |
|---|---|
| (1) `resmi_gazete.py` implements Protocol, URL construction, today/yesterday + Mükerrer probing, pdfplumber, per-item rows with synthetic URL, INSERT OR IGNORE | `musahit/ingest/resmi_gazete.py`; `TestSuccessfulFetchParse`, `TestDateFallback`, `TestMukerrerSupplement`, `TestRerunIdempotent`. |
| (2) `gazette_parsing.py` pure module with enums, dataclass, `parse_gazette_pdf`, section/item detection | `musahit/ingest/gazette_parsing.py`; `tests/test_gazette_parsing.py` |
| (3) Ingester test scenarios | All 8 scenarios covered across `tests/test_resmi_gazete.py` |
| (4) Pure-parser test scenarios | 6 scenarios + parametrized cases in `tests/test_gazette_parsing.py` |
| (5) Fixture PDFs | `tests/fixtures/resmi_gazete/sample_gazette.pdf`, `mukerrer_supplement.pdf`, `corrupted.bin` |
| (6) Zero network | All HTTP via `httpx.MockTransport`; parser is pure |
| (7) ruff clean + pytest zero | confirmed (205 passed, 1 skipped) |
| (8) FILE-PROTECTED untouched | confirmed (no edits to sources.py, defcon.py, promotion.py, ADRs) |
| (9) This document | ✓ |

No tripwires fired: pdfplumber handled the hand-crafted fixture, the
taxonomy fits the closed enum set, the synthetic URL scheme does not
collide with any existing helper, no schema change was needed, and the
ADRs remain internally consistent.

---

## ❯ Related Docs

- BOOTSTRAP.md — build step 6 of 20
- ADR-003 — source registry; Gazette URL convention
- ADR-006 — storage; `raw_articles` schema this code writes to
- ADR-012 — failure isolation; informs the parse-error policy
- ADR-014 — article id formula (shared helper used here)
- ADR-015 — typed metadata columns (`feed_entry_id`, `canonical_timestamp`)
- `docs/implementations/2026-05-23-rss-ingest.md` — RSS reference impl
- `docs/implementations/2026-05-23-html-ingest.md` — HTML reference impl
