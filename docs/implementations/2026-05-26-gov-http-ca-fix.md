# Implementation: gov_http CA bundle fix

**Date** · 2026-05-26
**Author** · Claude Code
**ADR refs** · n/a (bug fix, no architectural change)

---

## Problem / Motivation

All four `*.gov.tr` sources routed through `gov_http` failed with SSL errors
in `run_20260526`: `anayasa_mahkemesi` (curl error 60, cert verify),
`cumhurbaskanligi` and `resmi_gazete` (curl error 35, BoringSSL internal
error), `yargitay` (connection reset, separate infrastructure issue).

The 2026-05-25 spike scripts (`spike_session_pdf.py`, `spike_curl_cffi.py`)
successfully fetched the same domains using the same `curl_cffi.Session` +
`firefox133` impersonation pattern. The gap: production passed
`verify=certifi.where()`, overriding curl-impersonate's built-in BoringSSL
CA store with certifi's PEM bundle. The PEM bundle either lacked the
intermediate CAs for certain gov.tr domains or triggered a BoringSSL
compatibility issue when loaded alongside the impersonation TLS profile.

Full investigation: `docs/investigations/2026-05-26-gov-http-production-failure.md`

---

## What Changed

| File | Description |
|---|---|
| `musahit/ingest/gov_http.py` | `_resolve_ca_bundle()` returns `True` (use built-in BoringSSL CA store) instead of `certifi.where()` when `ca_bundle` is `None`. Updated class and method docstrings. |
| `tests/test_gov_http.py` | Added `TestResolveCaBundle` class with 3 tests: default returns `True`, explicit path is honoured, `False` disables verification. |

---

## Implementation Approach

Minimal fix: change the default branch of `_resolve_ca_bundle()` from
`import certifi; return certifi.where()` to `return True`. This tells
curl_cffi to use curl-impersonate's compiled-in BoringSSL CA store — the
same store the spike used successfully.

The `ca_bundle` constructor parameter is preserved: callers can still pass
an explicit path (str) or `False` to override verification. Only the
*default* changes.

---

## Design Decisions

| Alternative | Why rejected |
|---|---|
| `verify=False` (disable verification) | Works but removes all certificate validation. The impersonation TLS fingerprint is not a substitute for CA verification — a MITM proxy would go undetected. |
| Per-domain custom CA bundle | High maintenance: each gov.tr domain renewal or CA change requires updating the bundle. Over-engineered for this failure. |
| Keep certifi but update to a newer version | The issue is BoringSSL's PEM loading interaction, not certifi's version. certifi 2025.11.12 is current. |

---

## Verification

```powershell
# Unit tests (29 pass, including 3 new):
python -m pytest tests/test_gov_http.py -v

# Full suite (zero regressions):
python -m pytest --tb=short -q

# Ruff lint clean:
python -m ruff check musahit/ingest/gov_http.py tests/test_gov_http.py

# Production verification: next nightly run (run_20260527) should show
# anayasa_mahkemesi, cumhurbaskanligi, resmi_gazete clearing the SSL
# barrier. yargitay may still fail (infrastructure issue, not this bug).
```

---

## Related Docs

- `docs/investigations/2026-05-26-gov-http-production-failure.md` — root cause investigation
- `scripts/triage/spike_session_pdf.py` — the working spike that validated the pattern
- Commit `1d8754f` — original curl_cffi adoption
