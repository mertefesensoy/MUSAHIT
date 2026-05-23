# Implementation: common layer — types · db · logging · config

**Date** · 2026-05-22
**Author** · Claude Code (Mert Efe Şensoy directing)
**ADR refs** · ADR-001 · ADR-002 · ADR-003 · ADR-004 · ADR-005 · ADR-006 · ADR-008 · ADR-010 · ADR-011 · ADR-012

---

## ❯ Problem / Motivation

Build step 1 of 20 per BOOTSTRAP.md. Before any pipeline stage can be written,
the shared foundation must exist: domain enumerations that all stages reference,
a DuckDB connection factory, a structlog configuration, and a settings class that
merges `.env` secrets with `config.toml` defaults.

Without this layer every subsequent module would redefine types independently,
creating divergence risk and import cycles.

---

## ❯ What Changed

| File | Description |
|---|---|
| `pyproject.toml` | Project metadata, all runtime dependencies, ruff config, pytest config. |
| `.env.example` | Template showing the three secret groups: Reddit, SMTP, operator email. |
| `config.toml` | Non-secret defaults for every configurable parameter; safe to commit. |
| `musahit/__init__.py` | Package marker for the `musahit` Python namespace. |
| `musahit/common/__init__.py` | Subpackage marker. |
| `musahit/common/types.py` | All shared domain enums: Band, Tier, SourceKind, Fragility, IngestStatus, ArcState, Confidence, Category, OverrideAction, OverrideTarget, PipelineStatus. Also exports `PRIMARY_BANDS` and `SOCIAL_BANDS` frozensets. |
| `musahit/common/db.py` | `make_connection()` factory and `open_connection()` context manager; `load_vss` flag lets unit tests run offline. |
| `musahit/common/logging.py` | `configure_logging()` sets up structlog with stdlib backend for file + stdout JSON output; `get_logger(name)` returns a named bound logger. |
| `musahit/common/config.py` | `Settings` (pydantic-settings) with `_TomlSource` that reads `config.toml`; `get_settings()` singleton cached with `lru_cache`. |
| `tests/conftest.py` | `tmp_settings` and `mem_db` fixtures shared across all test modules. |
| `tests/common/test_types.py` | 34 tests covering every enum's member count, string values, and ASCII-safety of identifiers. |
| `tests/common/test_db.py` | 7 tests for connection creation, context manager cleanup, and exception propagation; VSS test marked skip (network). |
| `tests/common/test_logging.py` | 9 tests for logging setup, log file creation, and JSON output validity. |
| `tests/common/test_config.py` | 11 tests for defaults, programmatic override, TOML loading, and env-var priority. |
| `docs/implementations/_TEMPLATE.md` | Reusable template for all future implementation docs. |

---

## ❯ Implementation Approach

### Package layout decision

BOOTSTRAP.md labels the source directory `src/` but the CLI invocation is
`python -m musahit.pipeline`. Using `src/` as the Python package root would
require either a `package-dir` mapping in setuptools or a `PYTHONPATH=src`
wrapper. The cleaner resolution: the Python package is named `musahit/` and
lives at the repo root, matching the CLI namespace directly.

BOOTSTRAP's `src/` references correspond to actual paths under `musahit/`
(e.g. BOOTSTRAP's `src/score/defcon.py` = actual `musahit/score/defcon.py`).
ADR file protection comments have been documented with this mapping in mind.

### DEFCON intentionally absent from `types.py`

ADR-004 designates `musahit/score/defcon.py` as FILE-PROTECTED. Putting
`DEFCON` in `common/types.py` would place a FILE-PROTECTED enum in a
non-protected module, and would require `score/defcon.py` to re-export from
common — adding indirection with no benefit. Other modules that need `DEFCON`
will import it directly from `musahit.score.defcon` (step 11 of the build order).

### Settings priority order

```
init kwargs  >  env vars  >  .env file  >  config.toml  >  field defaults
```

This order means: programmatic construction (used in tests) always wins; actual
environment variables override `.env` (useful in Task Scheduler with explicit
env vars); `.env` overrides `config.toml` defaults; `config.toml` overrides the
Python-level defaults. Secrets (Reddit, SMTP) only come from `.env` — they have
no column in `config.toml` to prevent accidental commit of credentials.

### DuckDB `load_vss=False` parameter

The `INSTALL vss` call is idempotent but requires network access on first run.
All tests use `load_vss=False` and in-memory DB so the test suite passes
completely offline. The `TestVssExtension` class is kept but unconditionally
skipped; it documents the expected behavior when network is available.

### structlog + stdlib backend

structlog is configured with `stdlib.LoggerFactory()` and `ProcessorFormatter`
so that:
1. Log records flow through stdlib's handler chain (enabling file + stdout from
   a single configuration call).
2. Third-party libraries that use stdlib `logging` emit in the same JSON format.
3. The `configure_logging(log_file=...)` parameter lets the pipeline pass a
   per-run log path without touching global state.

---

## ❯ Mathematical / Statistical Details

No formulas in this layer. The arc linking thresholds (`arc_cosine_threshold`,
`arc_jaccard_threshold`) are stored in Settings as configurable floats (not
computed here) — their mathematical meaning is documented in ADR-008.

---

## ❯ Design Decisions

**`_TomlSource` custom class vs `SettingsConfigDict(toml_file=...)`**
pydantic-settings added `toml_file` shorthand in 2.2+. A custom
`PydanticBaseSettingsSource` is used instead to be explicit about the priority
ordering and to give the test suite a clear seam (tests `monkeypatch.chdir` to
a tmp directory so relative `config.toml` resolves correctly). The custom class
is ~15 lines and is well-understood.

**`get_settings()` cached with `lru_cache`**
The pipeline reads settings once at startup. `lru_cache(maxsize=1)` makes the
singleton pattern explicit and avoids repeated disk reads. Tests bypass it with
`Settings(...)` directly and never call `get_settings()` — this prevents test
pollution from cache state leaking between tests.

**`SOCIAL_BANDS` and `PRIMARY_BANDS` as frozensets in `types.py`**
ADR-005's promotion rules reference these sets repeatedly. Centralizing them in
`types.py` (not in `score/promotion.py`) lets any stage import them without
creating an import dependency on the score layer. `frozenset` is chosen over
`set` because it is immutable and hashable, matching the intent of "this is a
fixed constant."

---

## ❯ Verification

```powershell
# From repo root (MÜŞAHİT directory):

# Install in editable mode
pip install -e ".[dev]"

# Lint and format
python -m ruff check musahit/common/ tests/common/
python -m ruff format --check musahit/common/ tests/common/

# Tests
python -m pytest tests/common/ -v
# Expected: 61 passed, 1 skipped (VSS network test)

# Smoke-check imports
python -c "from musahit.common.types import Band, Confidence; print(Confidence.YUKSEK)"
python -c "from musahit.common.config import Settings; s = Settings(); print(s.dashboard_port)"
python -c "from musahit.common.db import make_connection; c = make_connection(':memory:', load_vss=False); c.execute('SELECT 1'); print('db ok')"
```

---

## ❯ Related Docs

- BOOTSTRAP.md — build order and coding conventions
- ADR-001 — architecture overview (single Python process, DuckDB)
- ADR-002 — LLM stack (Ollama model names stored in Settings)
- ADR-003 — source registry and bands (Band, Tier, SourceKind, Fragility defined here)
- ADR-004 — DEFCON schema (DEFCON enum deferred to step 11)
- ADR-005 — bias promotion rules (PRIMARY_BANDS, SOCIAL_BANDS, Confidence defined here)
- ADR-006 — storage (DuckDB connection pattern)
- ADR-008 — story arc model (ArcState defined here; arc thresholds in Settings)
- ADR-010 — TTS and delivery (piper_voice_path in Settings)
- ADR-011 — dashboard (dashboard_host, dashboard_port in Settings)
- ADR-012 — failure and retention (retention day fields in Settings)
