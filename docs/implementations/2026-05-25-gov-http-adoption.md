# Implementation: curl_cffi adoption for government source HTTP

**Date** · 2026-05-25
**Author** · MERT EFE ŞENSOY
**ADR refs** · ADR-003 (source registry · operator override for danistay drop), ADR-006 (raw_articles), ADR-012 (failure isolation), ADR-013 (source amendments precedent)

---

## ❯ Problem / Motivation

The first nightly smoke runs surfaced a systematic failure mode on every
``*.gov.tr`` source: standard ``httpx`` requests returned 403, 503, or
plain connection resets even with a realistic User-Agent. The cause is
TLS fingerprinting — every gov.tr origin sits behind an Akamai-style CDN
that compares the ClientHello against a known-browser table and rejects
non-browser clients before the HTTP layer ever runs. The 2026-05-25
triage spike (``scripts/triage/spike_curl_cffi.py``) confirmed the
diagnosis:

* ``httpx``: all 5 gov sources fail.
* ``curl_cffi`` with ``firefox133`` impersonation: 4 of 5 succeed; the
  Resmî Gazete PDF endpoint still returns junk because deep-link PDF
  requests need session cookies + a Referer header.
* Follow-up spike (``spike_session_pdf.py``): visit the homepage first
  to seed cookies, then fetch the PDF with ``Referer:
  https://www.resmigazete.gov.tr/`` — works. Full 19 MB PDF lands.
* The fifth source (``danistay``) is architecturally unreachable with
  the toolkit we ship: its listing renders entirely in JavaScript, so
  even when the TLS layer accepts our request the HTML body contains
  no article links until the page's JS runs in a browser. Out of scope
  for this change.

Without the fix, the pipeline produces empty briefings on the
gov-source side every night. With the fix, all four reachable gov
sources flow data through normalize → cluster → score → write.

---

## ❯ What Changed

| File | Description |
|---|---|
| `pyproject.toml` | Added `curl-cffi>=0.7` + `certifi>=2024.7` to `dependencies`. |
| `musahit/ingest/gov_http.py` | New module · `GovHttpResponse`, `GovHttpFetcher` Protocol, `CurlCffiGovHttpFetcher` (production), `FakeGovHttpFetcher` (tests), `SOURCE_IDS_USING_GOV_HTTP`, `GOV_BOOTSTRAP_URL`, `GOV_REFERER`, `make_gov_http_fetcher_for`, `referer_for`. |
| `musahit/ingest/html.py` | New `gov_http` constructor parameter; `fetch` branches on `source.id in SOURCE_IDS_USING_GOV_HTTP`; new `_fetch_with_gov` + `_gov_http_get` methods mirror the existing httpx flow. `_persist_article` type annotation accepts both response shapes via duck typing. |
| `musahit/ingest/resmi_gazete.py` | Same `gov_http` branching pattern. `_process_pdf` now validates the response body starts with the `%PDF` magic bytes (raises `ValueError` → caller maps to `PARSE_ERROR`). |
| `musahit/ingest/sources.py` | **FILE-PROTECTED edit** · operator override per the goal. `danistay` Source entry removed from `_GOV`; total count comment 37→36; in-file rationale block added. |
| `musahit/ingest/html_selectors.py` | `danistay` SelectorConfig removed; placeholder comment notes the architecturally-unreachable status. |
| `tests/test_gov_http.py` | New file · 26 tests for response shape, fake fetcher, factory, config maps, and `CurlCffiGovHttpFetcher` behavioral surface (no real network). |
| `tests/test_html.py` | 4 new tests for the gov-source routing path (FakeGovHttpFetcher injection, Referer plumbing, per-article exception isolation, listing 5xx). |
| `tests/test_resmi_gazete.py` | 7 new tests covering `%PDF` magic-byte rejection (Akamai HTML challenge), gov path happy & fallback paths, bootstrap+referer config pin, supplement isolation. |
| `tests/test_sources.py` | Bumped total count assertion 37→36 and GOV tier assertion 6→5 (two places). |

---

## ❯ Implementation Approach

### `gov_http.py` module shape

A small Protocol + two implementations + per-source config maps:

* `GovHttpResponse` — minimal `(.content, .status_code, .headers)`
  surface compatible with `httpx.Response`. Headers normalised to
  lowercase keys (the ingester consumers use lowercase lookups). Built
  via `from_raw` factory so callers don't need to think about
  normalisation.
* `GovHttpFetcher` — `runtime_checkable` Protocol with `bootstrap(url)`,
  `fetch(url, *, referer=None)`, and `close()`. All async so tests and
  production share the same await pattern.
* `CurlCffiGovHttpFetcher` — production. Wraps a sync
  `curl_cffi.requests.Session` and wraps each `Session.get` in
  `asyncio.to_thread` to stay async-clean. Session is constructed lazily
  on first use (so the module imports cleanly without curl_cffi
  installed). Bootstrap visits are de-duplicated via a
  `_bootstrapped_urls` set; if the fetcher was configured with a
  `bootstrap_url`, the first `fetch` triggers the bootstrap visit
  automatically before the real request. CA verification defaults to
  `certifi.where()` so Windows trust-store lag on freshly-renewed gov
  certificates doesn't bite the operator.
* `FakeGovHttpFetcher` — in-memory route table for tests. Records every
  `(url, referer)` pair so tests can assert ordering and Referer values
  without touching the network.

The per-source config maps (`SOURCE_IDS_USING_GOV_HTTP`,
`GOV_BOOTSTRAP_URL`, `GOV_REFERER`) live in this module rather than on
the `Source` dataclass because `musahit/ingest/sources.py` is
FILE-PROTECTED — extending the dataclass would have triggered a much
bigger ADR amendment than the operator override the goal authorised.
Tests pin a parity invariant: every source id in
`SOURCE_IDS_USING_GOV_HTTP` MUST appear in both `GOV_BOOTSTRAP_URL` and
`GOV_REFERER`.

### Ingester refactor (HTML + PDF)

Both ingesters gain a new optional constructor parameter
`gov_http: GovHttpFetcher | None = None` and a branch at the top of
`fetch`:

```text
if source.id ∈ SOURCE_IDS_USING_GOV_HTTP:
    gov_http := injected_fetcher OR make_gov_http_fetcher_for(source.id)
    try: return await _fetch_with_gov(gov_http, source)
    finally: if we constructed it, close it
else:
    use existing httpx path
```

`_fetch_with_gov` mirrors the existing `_fetch_with` flow per ingester
but every HTTP call goes through `gov_http.fetch(url, referer=…)`. The
persistence helper (`_persist_article` / `_persist_item`) is shared
between paths because both response shapes expose the same surface
(content/status_code/headers); the annotation is widened to
`httpx.Response | GovHttpResponse` to make the intent explicit.

ResmiGazeteIngester has one extra wrinkle: when a caller injects an
httpx `client` (the test pattern using `MockTransport`), we honor that
and skip the gov_http branch even for the gov-tagged `resmi_gazete`
source. This preserves all existing test fixtures that exercise the
httpx flow's logic without forcing them to learn curl_cffi.

### `%PDF` magic-byte check

Added at the top of `ResmiGazeteIngester._process_pdf`:

```python
if not response.content.startswith(PDF_MAGIC_BYTES):
    raise ValueError("resmi_gazete response is not a PDF ...")
```

Two failure modes this defends against:

1. The Akamai CDN occasionally returns an HTML JS-challenge page with
   `content-type: application/pdf` when the curl_cffi session is dirty
   (cookies expired, impersonation drift). Without the check, those
   bytes hit pdfplumber and raise an opaque `PDFSyntaxError`.
2. The normalize stage later reads `raw_articles.raw_content` and
   re-extracts the PDF. Persisting non-PDF bytes there masquerading as
   `application/pdf` would surface as a normalize-stage failure days
   after the bad fetch. Fail-fast here keeps the diagnostic close to
   the cause.

The existing `corrupted.bin` fixture happens to start with `%PDF-` so
the magic-byte check passes on it (the existing TestCorruptedPdf path
through pdfplumber still fires). New tests (`TestPdfMagicByteCheck`)
drive the check with payloads that start with `<html>` or other
non-PDF content.

### danistay drop

`musahit/ingest/sources.py` is FILE-PROTECTED. The goal text provides
the explicit operator override; the file header now records that
override:

```text
# Operator override 2026-05-25 · danistay dropped (architecturally
# unreachable · see _GOV comment block below).
```

The dropped entry is replaced with a block-comment marker so the next
operator reading the registry sees the deletion was deliberate (not
"oh someone forgot to add it") and understands the architectural
reason: Danıştay's press-release listing is JS-rendered. selectolax
cannot parse it without a headless-browser dependency the project
doesn't ship; curl_cffi solved TLS but not JS. Re-adding the source
is a future-Playwright work item.

The matching SelectorConfig in `musahit/ingest/html_selectors.py` was
removed alongside the Source so the maps stay in sync.

---

## ❯ Mathematical / Statistical Details

No statistical algorithm. One trivial invariant: the magic-byte check
compares `response.content[:4]` against the 4-byte literal `b"%PDF"`
(ISO 32000 PDF specification, first-bytes requirement). The check is
O(1) and side-effect free.

---

## ❯ Design Decisions

**Why curl_cffi over a Playwright back-end?** Playwright would solve
both the TLS-fingerprint problem AND the JS-rendering problem (which
would let us re-include danistay). But it requires a browser runtime
on disk (~250-500 MB), a separate install step, and async resource
management substantially heavier than `asyncio.to_thread` around a
sync Session. curl_cffi handles the TLS issue with zero additional
runtime cost and re-uses the existing libcurl native bindings.
Playwright remains a future option if more *.gov.tr sources turn out
to be JS-rendered.

**Why a separate module instead of extending httpx?** curl_cffi's
Session API is sync-only and uses keyword arguments (`impersonate`,
`verify`) that don't slot cleanly into `httpx.AsyncClient`. A
side-by-side adapter keeps the curl_cffi type surface from leaking
into the rest of the ingester layer; non-gov ingesters keep their
existing httpx path verbatim.

**Why `asyncio.to_thread` around the sync Session instead of
`curl_cffi.AsyncSession`?** The async Session in curl_cffi is newer
and has had stability issues across recent versions. The sync Session
+ `asyncio.to_thread` pattern is already the project convention for
PRAW (`musahit/ingest/reddit.py`), so reusing it minimises new
surface area. If `AsyncSession` stabilises, switching is a one-file
change.

**Why a Protocol + two implementations instead of a single fetcher
with a "fake" mode?** Test fakes that share code with the production
implementation tend to drift — a "test mode" branch that conditionally
skips HTTP work can hide bugs that only fire under real curl_cffi
behavior. Separating the protocol from both implementations makes the
fake's API surface explicit and lets tests assert call ordering
(`calls` + `bootstrap_calls` lists) without instrumenting the
production code.

**Why mark `danistay` as removed via comment block rather than just
deleting the dataclass entry cleanly?** Future readers of
`sources.py` will see a 5-source GOV section and ask "why not the
sixth?" Without the comment block, that's a 30-minute archaeology
project. With the comment block, it's a 30-second read. The
sources.py file is FILE-PROTECTED *because* its history matters; the
comment maintains that historical signal.

**Why a generous 60 s default timeout?** The Resmî Gazete PDF runs
15-20 MB and the operator's spike confirmed 180 s is sometimes needed
for the full download. 60 s is the per-call default for listings and
small responses; the ingester (or future per-source override) can
raise it. ADR-007's per-stage timing budgets already accommodate this
(60-minute ingest budget).

**Why include `tbmm` in `SOURCE_IDS_USING_GOV_HTTP` without spike
validation?** It shares the `*.gov.tr` domain pattern with the four
spike-validated sources. Including it pre-emptively means the next
smoke run exercises the gov_http path against it; if it turns out to
work fine on plain httpx, removing it later is trivial. The cost of
a false-positive inclusion (slightly slower curl_cffi fetch) is much
smaller than the cost of a false-negative (silent failure on the next
run).

---

## ❯ Verification

```powershell
# Targeted suite
python -m pytest tests/test_gov_http.py tests/test_html.py tests/test_resmi_gazete.py tests/test_sources.py -q
# Expect 101 passed

# Full suite
python -m pytest tests/ -q
# Expect 645 prior + 37 new = 682 passed, 2 skipped (zero regressions)

# Linter clean on changed Python files
python -m ruff check musahit/ingest/gov_http.py musahit/ingest/html.py `
  musahit/ingest/resmi_gazete.py musahit/ingest/sources.py `
  musahit/ingest/html_selectors.py tests/test_gov_http.py `
  tests/test_html.py tests/test_resmi_gazete.py tests/test_sources.py
# Expect "All checks passed!"
```

Operator-side validation (post-merge, next smoke run):

1. Re-run a single source via the pipeline CLI with `--stage ingest
   --source resmi_gazete` (or the equivalent operator script). Confirm
   `ingest_log` reports `OK` status and the row count matches the
   number of items in the day's gazette.
2. Inspect `raw_articles` for the resmi_gazete rows; confirm
   `raw_content` is real PDF bytes (`head -c 4 raw_content` returns
   `%PDF`).
3. Run the spike script in dry mode to confirm impersonation defaults
   still match upstream library behavior:
   `python scripts/triage/spike_curl_cffi.py` (operator's local terminal,
   not CI).
4. If any *.gov.tr source returns the Akamai challenge HTML page (rare,
   indicates session drift), the new magic-byte check now surfaces a
   `PARSE_ERROR` with `"not a PDF"` in the message rather than a
   silent persistence followed by a normalize-stage failure.

---

## ❯ Related Docs

- ADR-003 · source registry (operator override authorising danistay drop)
- ADR-006 · raw_articles schema (unchanged)
- ADR-012 · failure isolation (unchanged · new check fits PARSE_ERROR shape)
- `scripts/triage/spike_curl_cffi.py` · TLS-fingerprint probe
- `scripts/triage/spike_session_pdf.py` · session + Referer validation
- `scripts/triage/spike_session_pdf_full.py` · 180 s timeout demonstration
- `docs/milestones/musahit-roadmap-v3.md` · Government source coverage entry
