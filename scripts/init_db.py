"""Apply DuckDB schema migrations to data/musahit.duckdb.

Usage (from repo root)::

    python scripts/init_db.py

The DB path comes from Settings (config.toml / environment). To use a custom
path set DB_PATH in the environment or edit config.toml before running.

Safe to rerun: migrations already applied are skipped; HNSW indices are created
idempotently. VSS extension failure is non-fatal · schema still applies without
vector-search indices.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure repo root is on sys.path so `import musahit` works whether the
# package is installed editable or the script is run directly by Task Scheduler
# with `sys.path[0]` set to `scripts/`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from musahit.common.config import get_settings  # noqa: E402
from musahit.common.db import open_connection  # noqa: E402
from musahit.common.logging import configure_logging, get_logger  # noqa: E402
from musahit.common.migrations import init_db  # noqa: E402
from musahit.ingest.sources import seed_sources  # noqa: E402


def main() -> int:
    configure_logging()
    log = get_logger("init_db")
    settings = get_settings()
    log.info("init_db_starting", db_path=str(settings.db_path))
    result = init_db(settings.db_path)
    log.info("init_db_complete", **result)
    # init_db closes its own connection; open a fresh one for seeding.
    with open_connection(settings.db_path, load_vss=False) as conn:
        seeded = seed_sources(conn)
        log.info("sources_seeded", count=seeded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
