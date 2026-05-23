# Implementation: static source registry

**Date** · 2026-05-23
**Author** · Claude Code (Mert Efe Şensoy directing)
**ADR refs** · ADR-003 · ADR-013

---

## ❯ Problem / Motivation

Build step 3 of 20 per BOOTSTRAP.md. Every downstream ingest module (rss.py,
html_scrape.py, kap.py, reddit.py, poller.py) and the promotion-ceiling logic in
`score/promotion.py` need a single authoritative list of sources with their band,
tier, kind, URL, and operational metadata. This file is FILE-PROTECTED — once
committed, changes require an ADR amendment.

Two decisions in ADR-003 were also amended in this commit (see ADR-013):
- `bloomberg_ht.band` changed from INTERNATIONAL to CENTRIST (Demirören ownership)
- `x_stub` Source object not created (SOCIAL_X band reserved in enum only)

---

## ❯ What Changed

| File | Description |
|---|---|
| `ADR-013-source-registry-amendments.md` | Formal supersession of two ADR-003 decisions |
| `musahit/ingest/__init__.py` | Ingest subpackage marker |
| `musahit/ingest/sources.py` | FILE-PROTECTED: Source dataclass, 37 source entries, validators, helpers, seed_sources |
| `tests/test_sources.py` | 39 tests across 7 test classes |
| `scripts/init_db.py` | Added seed_sources call after init_db |
| `.gitignore` | Created; covers data/*.duckdb, briefings/, logs/, .env |

---

## ❯ Implementation Approach

### URL verification policy

Each RSS/Atom source URL was verified via WebFetch at scaffold time. Sources whose
URL returned valid feed XML are marked with a `★ VERIFIED` inline comment. Sources
where WebFetch returned 4xx/5xx or a connection error are marked `⌛ PENDING` and
carry `notes="URL pending operator verification · <reason>"`. HTML-kind sources are
exempt (their root domain URLs are correct by construction; the specific scrape
endpoint is owned by the individual ingest module).

**7 pending sources** (RSS/API kind, URL unconfirmed):
`anadolu`, `t24`, `medyascope`, `dw_tr`, `voa_tr`, `reuters_tr`, `kap`

### Registry validator

`_build_sources_index()` runs at module import time and raises `ValueError` on:
- Duplicate source IDs
- IDs that are not lowercase alphanumeric + underscores
- Empty URLs
- Non-positive `rate_limit_seconds`

This makes misconfiguration a hard import error visible immediately in tests,
not a silent runtime failure during a 3 AM ingest run.

### seed_sources upsert

Explicit `.value` coercion (`s.band.value`, etc.) is used for all enum fields.
DuckDB's StrEnum handling is not relied upon — the stored values are canonical
string literals (e.g. `"centrist"`, not `<Band.CENTRIST: 'centrist'>`). The upsert
SQL uses `ON CONFLICT (id) DO UPDATE SET ...` so re-running after adding a note to
an existing source updates the row without creating duplicates.

### Connection management in scripts/init_db.py

`init_db()` closes its own connection in a `finally` block. `seed_sources` receives
a fresh connection opened by `open_connection(settings.db_path, load_vss=False)` in
the `main()` function — a second round-trip to the DB file is acceptable for this
one-time setup script.

---

## ❯ Mathematical / Statistical Details

None — this is a purely structural change.

---

## ❯ Design Decisions

**FILE-PROTECTED header comment**
Required by BOOTSTRAP.md. The header makes the constraint visible to any future
editor in their first scroll of the file.

**Operator-facing `notes` field, not a separate metadata dict**
Notes are free-text strings; structured metadata (fragility, rate_limit_seconds) are
typed fields. This avoids an over-engineered metadata layer for what is essentially
operator-maintained commentary.

**SOURCES tuple, not a frozenset**
Tuples preserve declaration order, which matters for seeding (deterministic insert
order aids debugging) and for future modules that iterate sources in a defined order
(poller.py processes them sequentially per ADR-003's polite-scraping requirement).

**SOURCES_BY_ID validator raises ValueError at import time**
This catches configuration mistakes at test-collection time — before any ingest
module runs. The alternative (raising at first `get_source()` call) would leave a
misconfigured registry silently valid until the broken source is first accessed.

**x_stub not created (ADR-013 Amendment 2)**
A Source with `kind=DEFERRED` that is permanently skipped by the poller provides
no operational value and clutters the dashboard's source health view. The SOCIAL_X
band slot and SourceKind.DEFERRED enum value are retained in types.py for when an
X ingest strategy is chosen.

---

## ❯ Sources pending operator URL verification

The following sources have `notes` containing `"URL pending operator verification"`.
The operator should verify these URLs before the first production ingest run.

| id | Issue | Suggested action |
|---|---|---|
| anadolu | ECONNREFUSED at scaffold time | Try `https://www.aa.com.tr/` in browser; locate RSS link |
| t24 | HTTP 403 | Try `https://t24.com.tr/` in browser; locate RSS link (may require browser UA) |
| medyascope | HTTP 403 | Same as t24 |
| dw_tr | rss.dw.com blocked from WebFetch | Verify `https://rss.dw.com/rdf/rss-tur-all` manually |
| voa_tr | HTTP 403; domain migrated to voaturkce.com | Locate current API URL from `https://www.voaturkce.com/` |
| reuters_tr | reuters.com blocked from WebFetch | Verify parent feed URL; confirm Turkey filter works in normalize stage |
| kap | RSS paths returned 404 | Navigate KAP site to find disclosure RSS endpoint |

---

## ❯ Verification

```powershell
python -m ruff check musahit/ingest/sources.py tests/test_sources.py scripts/init_db.py
python -m ruff format --check musahit/ingest/sources.py tests/test_sources.py scripts/init_db.py
python -m pytest tests/ -q
# Expected: green

# Smoke test with throwaway DB
$env:DB_PATH = "tmp/smoke_sources.duckdb"
New-Item -ItemType Directory -Force -Path tmp
python scripts/init_db.py
# Expected log: migrations_applied=1, sources_seeded count=37
Remove-Item tmp -Recurse -Force
```

---

## ❯ Related Docs

- BOOTSTRAP.md — build step 3 of 20; file protection list
- ADR-003 — source registry specification (band taxonomy, source table, fragility)
- ADR-013 — amendments: bloomberg_ht band + x_stub omission
- ADR-005 — promotion ceiling rules (uses Band enum values from this registry)
- `docs/implementations/2026-05-22-common-layer.md` — Band/Tier/SourceKind/Fragility enums
- `docs/implementations/2026-05-22-init-db.md` — schema that seed_sources writes into
