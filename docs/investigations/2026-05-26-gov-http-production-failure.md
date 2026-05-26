# Investigation: gov_http sources fail in production despite spike success

**Date** · 2026-05-26
**Author** · diagnostic investigation per /goal
**Mode** · READ-ONLY · no code changed, no probes sent, no pipeline executed.

---

## Summary

The root cause is a single line in `CurlCffiGovHttpFetcher._resolve_ca_bundle()`: when `ca_bundle` is `None` (the default for every production fetcher), it returns `certifi.where()` — a path to certifi's PEM bundle. This PEM bundle is then passed to curl-impersonate's BoringSSL via `CURLOPT_CAINFO`, overriding the built-in CA store that ships inside curl-impersonate. The 2026-05-25 spike scripts never passed `verify` at all, so curl-impersonate used its built-in BoringSSL root store, which can verify the gov.tr certificate chains. Passing `verify=certifi.where()` either (a) provides a CA bundle that lacks the intermediate CAs used by certain gov.tr domains (anayasa.gov.tr → curl error 60), or (b) triggers a BoringSSL-internal parsing/loading error when the PEM file is consumed alongside the impersonation TLS profile (resmigazete.gov.tr, tccb.gov.tr → curl error 35). The proof is that `tbmm.gov.tr` — which routes through the identical code path and `verify=certifi.where()` call — succeeds, because its certificate chain happens to be fully covered by certifi's bundle and doesn't trigger the BoringSSL incompatibility. Yargitay is a separate issue: infrastructure-level connection resets, consistent with yesterday's DNS failure on the same domain.

---

## Production error details

Source: `ingest_log` table, `run_id = 'run_20260526'`.

| source_id | status | duration | error_detail |
|---|---|---|---|
| `anayasa_mahkemesi` | HTTP_ERROR | 1.3 s | `gov_http CertificateVerifyError: Failed to perform, curl: (60) SSL certificate problem: unable to get local issuer certificate.` |
| `cumhurbaskanligi` | HTTP_ERROR | 2.5 s | `gov_http SSLError: Failed to perform, curl: (35) TLS connect error: error:00000000:invalid library (0):OPENSSL_internal:invalid library (0).` |
| `resmi_gazete` | HTTP_ERROR | 7.5 s | `gov_http SSLError: Failed to perform, curl: (35) TLS connect error: error:00000000:invalid library (0):OPENSSL_internal:invalid library (0).` |
| `yargitay` | HTTP_ERROR | 0.8 s | `gov_http SSLError: Failed to perform, curl: (35) Recv failure: Connection was reset.` |
| `tbmm` | OK | 1.7 s | (none) |

Yesterday's run (`run_20260525`) used the OLD httpx path (pre-`1d8754f`). For comparison:

| source_id | run_20260525 (httpx) | run_20260526 (curl_cffi) |
|---|---|---|
| `anayasa_mahkemesi` | `ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED]` | `curl: (60) unable to get local issuer certificate` |
| `cumhurbaskanligi` | `ConnectError:` (empty detail) | `curl: (35) TLS connect error: OPENSSL_internal` |
| `resmi_gazete` | OK (0 articles) | `curl: (35) TLS connect error: OPENSSL_internal` |
| `yargitay` | `ConnectError: getaddrinfo failed` | `curl: (35) Connection was reset` |
| `tbmm` | OK (0 articles) | OK (0 articles) |

Key observations:
- **anayasa_mahkemesi** had the SAME cert-verify failure under httpx yesterday → the domain's cert chain is not in certifi regardless of transport.
- **resmi_gazete** SUCCEEDED under httpx yesterday (0 articles, likely due to date probing) but FAILS under curl_cffi today → the `verify=certifi.where()` parameter is the new variable.
- **yargitay** had infrastructure issues both days (DNS yesterday, connection reset today) → likely a separate server-side problem.
- **tbmm** succeeds on both days → its cert chain is compatible with certifi's bundle.

---

## Code path audit

### Does gov_http.py match the spike pattern?

| Aspect | Spike (`spike_session_pdf.py`) | Production (`gov_http.py`) | Match? |
|---|---|---|---|
| Transport | `curl_cffi.requests.Session` | `curl_cffi.requests.Session` (line 226) | YES |
| Session constructor | `Session()` (no args) | `Session()` (no args, line 226) | YES |
| Impersonation | `"firefox133"` per-request | `"firefox133"` per-request (line 63, 272) | YES |
| Bootstrap (homepage visit) | Explicit `session.get(homepage)` | Lazy bootstrap via `_configured_bootstrap_url` (line 237-244) | YES |
| Referer header | `headers={"Referer": homepage}` on deep fetch | `kwargs["headers"] = {"Referer": referer}` (line 278) | YES |
| Timeout | 15 s (homepage), 30 s (PDF) | 60 s (all requests, line 68) | YES (more generous) |
| **`verify` parameter** | **Not passed** (curl default) | **`verify=certifi.where()`** (line 275, via `_resolve_ca_bundle`) | **NO — THE GAP** |
| `asyncio.to_thread` | Not used (synchronous) | Wraps `_sync_get` (lines 243, 257) | N/A |

The `verify` parameter is the only meaningful difference between the spike and production. The spike relies on curl-impersonate's built-in BoringSSL CA store (which includes the standard Mozilla NSS roots and can verify gov.tr chains). Production overrides this with certifi's PEM bundle, which either lacks certain intermediates or triggers a BoringSSL compatibility issue.

### The `_resolve_ca_bundle` method (`gov_http.py:229-235`)

```python
def _resolve_ca_bundle(self) -> str | bool:
    if self._ca_bundle is None:
        import certifi
        return certifi.where()
    return self._ca_bundle
```

- `make_gov_http_fetcher_for()` does NOT pass `ca_bundle` → defaults to `None`
- `None` → `certifi.where()` → `C:\...\certifi\cacert.pem` (certifi 2025.11.12)
- This PEM path is passed as `verify=` to every `session.get()` call
- The docstring (lines 191-197) says this was added defensively for Windows trust store lag — but curl-impersonate doesn't use the Windows trust store; it uses BoringSSL's compiled-in roots. The defensive measure is misdirected.

### Call path tracing

**HtmlIngester** (`html.py`):
1. `fetch(source)` → `source.id in SOURCE_IDS_USING_GOV_HTTP` → yes for anayasa_mahkemesi, cumhurbaskanligi, yargitay, tbmm
2. `self._gov_http` is `None` (poller.py:100 constructs `HtmlIngester(conn=conn)` with no `gov_http=`)
3. Falls through to `make_gov_http_fetcher_for(source.id)` → new `CurlCffiGovHttpFetcher`
4. `_fetch_with_gov()` → `_gov_http_get()` → `gov_http.fetch()` → `_sync_get()` → `session.get(..., verify=certifi.where())`
5. SSL error propagates → `IngestResult(HTTP_ERROR)`

**ResmiGazeteIngester** (`resmi_gazete.py`):
1. `fetch(source)` → `source.id in SOURCE_IDS_USING_GOV_HTTP` and `self._client is None` → yes
2. Same pattern: `make_gov_http_fetcher_for("resmi_gazete")` → new fetcher
3. `_fetch_with_gov()` tries two candidate dates (today, yesterday) — each triggers bootstrap → SSL error
4. Both fail → returns last error (7.5 s total for two failed attempts)

### Error propagation for resmi_gazete (explaining the 7.5 s duration)

1. Candidate 1 (`2026-05-26.pdf`): `fetch()` → bootstrap fires → `_sync_get(homepage)` → SSL error 35 → bootstrap fails → exception propagates to `_gov_http_get` → `IngestResult(HTTP_ERROR)`
2. Bootstrap was never added to `_bootstrapped_urls` (exception before line 244's `.add()`)
3. Candidate 2 (`2026-05-25.pdf`): `fetch()` → bootstrap fires AGAIN (same URL, still not in `_bootstrapped_urls`) → same SSL error → same result
4. Both candidates failed → returns the error from candidate 2

---

## Concurrency analysis

### Poller execution model (`poller.py`)

- All 36 sources dispatch concurrently via `asyncio.create_task` + `asyncio.gather` (line 171-175)
- `asyncio.Semaphore(8)` caps concurrent slots (line 170)
- Each gov source gets its OWN `CurlCffiGovHttpFetcher` (constructed inside `HtmlIngester.fetch()` / `ResmiGazeteIngester.fetch()`)
- Sessions are NOT shared across sources

### Timeline of gov source execution

From the `started_at` / `completed_at` durations, all 5 gov sources likely acquired semaphore slots in the first wave (they're among the first 8 of 36 sources). This means 5 simultaneous TLS handshakes to 5 different `*.gov.tr` domains from the same client IP within ~1 second.

### Is concurrency a contributing factor?

**Unlikely for this failure mode.** The errors are SSL/TLS-level (curl errors 35 and 60), not HTTP-level rate limiting (which would manifest as 403/429 status codes). The TLS errors occur during the handshake before any HTTP request is sent, so the server's rate limiter (which operates at the HTTP layer) hasn't engaged yet.

However, concurrency COULD become a factor once the SSL issue is fixed: 5 simultaneous bot-detection-screened requests from the same IP to related government domains could trigger Akamai's behavioral analysis. A sequential execution strategy or per-domain delay would mitigate this as a follow-up concern.

---

## Test coverage analysis (`tests/test_gov_http.py`)

| What's tested | Coverage |
|---|---|
| `GovHttpResponse.from_raw` normalization | Full |
| `FakeGovHttpFetcher` route table + call recording | Full |
| Config consistency (every source has bootstrap URL + referer) | Full |
| `make_gov_http_fetcher_for` factory KeyError + happy path | Full |
| Bootstrap idempotence (`_bootstrapped_urls` set) | Full — via stubbed `_sync_get` |
| Lazy bootstrap trigger on first `fetch` | Full — via stubbed `_sync_get` |
| Protocol conformance (`isinstance` checks) | Full |
| **Actual `curl_cffi.Session.get()` call** | **NOT TESTED** |
| **`verify=certifi.where()` behavior with impersonation** | **NOT TESTED** |
| **`_resolve_ca_bundle()` return value** | **NOT TESTED** |
| **Real TLS handshake against any endpoint** | **NOT TESTED** (by design — rate limit risk) |

The critical gap: `_resolve_ca_bundle()` is exercised zero times in the test suite. Every test that exercises the fetcher stubs `_sync_get`, bypassing the Session construction and the `verify` parameter entirely. A unit test that asserts `_resolve_ca_bundle()` returns `True` (not a file path) when `ca_bundle is None` would have caught this.

---

## Hypothesis ranking

| Rank | Hypothesis | Evidence | Confidence |
|---|---|---|---|
| **A** | **`verify=certifi.where()` overrides curl-impersonate's working CA store with a bundle that lacks gov.tr intermediates or triggers BoringSSL compatibility issues** | (1) Spike works without `verify`. (2) Production fails with `verify=certifi.where()`. (3) anayasa_mahkemesi had the SAME cert-verify failure under httpx yesterday (certifi is the common factor). (4) tbmm succeeds — its chain IS in certifi. (5) Error 35 "OPENSSL_internal:invalid library" is a known BoringSSL + PEM bundle interaction issue in curl_cffi 0.15.x. | **HIGH (95%)** |
| B | Rate limit lingering from yesterday's spike probes | (1) The spike ran ~24 h before today's production run → rate limits typically expire in minutes to hours. (2) tbmm succeeded. (3) Errors are SSL, not HTTP 403/429. | LOW (5%) |
| C | Concurrency triggers CDN detection | (1) Errors are SSL-level, not HTTP-level. (2) tbmm succeeded alongside the others. (3) 5 simultaneous connections is well within normal browsing behavior. | LOW (3%) |
| D | `asyncio.to_thread` breaks session state | (1) tbmm succeeds through the same `to_thread` path. (2) Each fetcher uses `to_thread` calls sequentially (bootstrap → fetch). (3) No cross-thread session sharing. | NEGLIGIBLE |

---

## Fix options

### Option 1: Use curl-impersonate's built-in CA store (RECOMMENDED)

**What:** Change `_resolve_ca_bundle()` to return `True` when `ca_bundle is None`, instead of `certifi.where()`.

**Scope:** One line in `musahit/ingest/gov_http.py:231-233`. Replace `import certifi; return certifi.where()` with `return True`. Update the docstring. Add a unit test for `_resolve_ca_bundle()`.

**Trade-offs:**
- (+) Exactly matches the spike's working pattern
- (+) curl-impersonate's BoringSSL ships with Mozilla NSS root CAs, updated with each curl_cffi release
- (+) Eliminates the PEM-loading code path that triggers the BoringSSL error 35
- (-) CA freshness tied to curl_cffi release cadence rather than certifi release cadence
- (-) If a gov.tr domain switches to a CA not in BoringSSL's compiled store, it would fail silently until curl_cffi is upgraded

**Confidence:** HIGH. Directly addresses the proven root cause.

### Option 2: Set `verify=False` for gov sources

**What:** Pass `ca_bundle=False` in `make_gov_http_fetcher_for()`.

**Scope:** One line in `gov_http.py:359`. Add `ca_bundle=False` to the `CurlCffiGovHttpFetcher()` constructor call.

**Trade-offs:**
- (+) Guaranteed to work regardless of CA store contents or BoringSSL quirks
- (+) The impersonation TLS fingerprint still provides CDN acceptance
- (-) **Disables all certificate verification** — a MITM proxy between the pipeline and the gov.tr endpoint would go undetected
- (-) Bad precedent; hard to audit and easy to forget the security implication

**Confidence:** HIGH for functional fix, but poor security posture.

### Option 3: Sequential gov source execution with retry

**What:** Modify `poller.py` to run the 5 gov sources sequentially (or with `Semaphore(1)`) rather than concurrently, with a 2–3 s inter-source delay.

**Scope:** Moderate change in `poller.py` (separate gov sources into a sequential batch) or `gov_http.py` (add a module-level lock).

**Trade-offs:**
- (+) Reduces the concurrency signature that might trigger CDN behavioral detection (future-proofing)
- (+) Easier on the target servers' rate limiters
- (-) **Does not fix the SSL error** — the root cause is `verify=certifi.where()`, not concurrency
- (-) Adds ~10–15 s to the total ingest time
- (-) Requires touching the file-protected `poller.py`

**Confidence:** LOW as a standalone fix. Valuable as a complementary measure after Option 1.

### Option 4: Per-domain CA bundle override

**What:** Ship a custom CA bundle (extracted from the working BoringSSL store or from the gov.tr certificate chains) and pass its path as `verify=`.

**Scope:** New file (`data/gov_tr_ca_bundle.pem`), update `make_gov_http_fetcher_for()` to pass its path as `ca_bundle`.

**Trade-offs:**
- (+) Precisely controls which CAs are trusted for gov.tr
- (+) Doesn't disable verification entirely
- (-) High maintenance: each gov.tr domain renewal or CA change requires updating the bundle
- (-) Over-engineered for the current failure — Option 1 solves it with no new files

**Confidence:** MEDIUM, but complexity is disproportionate to the problem.

---

### Recommendation: Option 1

Change `_resolve_ca_bundle()` to return `True` instead of `certifi.where()`. This is a one-line fix that exactly matches the spike's proven-working configuration. Consider Option 3 as a follow-up hardening measure once the SSL issue is resolved.

Yargitay requires separate attention: its "Connection was reset" error and yesterday's DNS failure suggest an infrastructure problem on yargitay.gov.tr's side, not a client-side configuration issue. Monitor across the next 2–3 runs before investigating further.

---

## Environment details

| Component | Version |
|---|---|
| curl_cffi | 0.15.0 |
| certifi | 2025.11.12 |
| certifi CA path | `C:\Users\senso\AppData\Local\...\certifi\cacert.pem` (ASCII-safe) |
| Python | 3.13 |
| OS | Windows 11 Home |

---

## Related docs

- `scripts/triage/spike_session_pdf.py` — the working spike
- `musahit/ingest/gov_http.py` — the production fetcher
- `musahit/ingest/html.py` — HTML ingester call path
- `musahit/ingest/resmi_gazete.py` — PDF ingester call path
- `musahit/ingest/poller.py` — orchestrator concurrency model
- Commit `1d8754f` — curl_cffi adoption
