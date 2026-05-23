"""DuckDB schema migration runner.

Tracks applied migrations in `schema_version`. Safe to rerun: each migration
is guarded by a version check before execution. VSS extension (vector search)
is a soft dependency — tables apply without it, HNSW indices are added only
when VSS loads successfully.

The public entry point for everything is `init_db()`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import duckdb

from musahit.common.db import make_connection
from musahit.common.logging import get_logger

# Absolute path resolved at import time — works regardless of working directory
# or Task Scheduler invocation path.
# Depth:  migrations.py → common/ → musahit/ → repo-root
DEFAULT_MIGRATIONS_DIR: Path = Path(__file__).resolve().parents[2] / "scripts" / "migrations"

HNSW_INDICES: list[tuple[str, str, str]] = [
    ("idx_article_emb_hnsw", "article_embeddings", "embedding"),
    ("idx_arc_centroid_hnsw", "arc_centroids", "centroid"),
]


# ── VSS helpers ──────────────────────────────────────────────────────────────


def _install_and_load_vss(conn: duckdb.DuckDBPyConnection) -> None:
    """Run INSTALL vss + LOAD vss. Separated so tests can monkeypatch it."""
    conn.execute("INSTALL vss")
    conn.execute("LOAD vss")


def try_load_vss(conn: duckdb.DuckDBPyConnection, log: Any) -> bool:
    """Attempt to install and load the VSS extension; return True on success.

    Failure (no network, unsupported platform, extension-server outage) is
    logged at WARN level and execution continues. The pipeline degrades to
    sequential scan for vector queries — slower but functionally correct.
    """
    try:
        _install_and_load_vss(conn)
        log.info("vss_loaded")
        return True
    except duckdb.Error as exc:
        log.warning("vss_load_failed", error=str(exc))
        return False


# ── Schema-version helpers ────────────────────────────────────────────────────


def ensure_schema_version_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the schema_version tracking table if it does not exist.

    Called before any migration scan so the table is always present, even
    when the migrations directory is empty.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def applied_versions(conn: duckdb.DuckDBPyConnection) -> set[int]:
    """Return the set of migration version numbers already applied."""
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    return {row[0] for row in rows}


# ── Migration runner ──────────────────────────────────────────────────────────


def _iter_statements(sql: str) -> list[str]:
    """Split a multi-statement SQL string into individual, non-empty statements.

    Line comments (-- ...) are stripped before splitting on ';' so that a
    semicolon inside a comment does not produce a spurious empty statement.
    String literals are not parsed — this is safe because none of our schema
    SQL contains semicolons inside quoted strings.
    """
    no_comments = re.sub(r"--[^\n]*", "", sql)
    return [stmt.strip() for stmt in no_comments.split(";") if stmt.strip()]


def apply_migration(
    conn: duckdb.DuckDBPyConnection,
    version: int,
    description: str,
    sql: str,
    log: Any,
) -> None:
    """Execute all statements in sql and record version in schema_version.

    Runs inside a single transaction: if any statement fails the whole
    migration rolls back and the version row is not recorded.
    """
    log.info("applying_migration", version=version, description=description)
    stmts = _iter_statements(sql)
    conn.begin()
    try:
        for stmt in stmts:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            [version, description],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def apply_pending_migrations(
    conn: duckdb.DuckDBPyConnection,
    migrations_dir: Path,
    log: Any,
) -> int:
    """Apply all unapplied migrations found in migrations_dir.

    Files must match the pattern ``NNN_<description>.sql`` (e.g.
    ``001_initial_schema.sql``). They are applied in lexical / numeric order.

    Returns the number of migrations newly applied.
    """
    already_applied = applied_versions(conn)
    sql_files = sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql"))

    count = 0
    for sql_file in sql_files:
        stem = sql_file.stem  # "001_initial_schema"
        version_str, _, description = stem.partition("_")
        version = int(version_str)

        if version in already_applied:
            log.debug("migration_already_applied", version=version)
            continue

        sql = sql_file.read_text(encoding="utf-8")
        apply_migration(conn, version, description, sql, log)
        count += 1

    return count


# ── HNSW index creation ───────────────────────────────────────────────────────


def create_hnsw_indices(conn: duckdb.DuckDBPyConnection, log: Any) -> int:
    """Create HNSW indices for embedding columns. Caller must have loaded VSS.

    Each index is created idempotently with ``IF NOT EXISTS``. Failures are
    per-index soft: if one CREATE INDEX errors (e.g. VSS version mismatch,
    platform incompatibility) a WARN is logged and the next index is attempted.

    Returns the count of ``CREATE INDEX`` statements that completed without
    raising — including no-ops where the index already existed.
    """
    # Older DuckDB VSS versions require this flag for file-based HNSW indices.
    # Silently ignore if the setting does not exist in newer DuckDB releases.
    try:
        conn.execute("SET hnsw_enable_experimental_persistence = true")
    except duckdb.Error:
        pass

    created = 0
    for index_name, table_name, column_name in HNSW_INDICES:
        try:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {index_name} "
                f"ON {table_name} USING HNSW ({column_name}) "
                f"WITH (metric = 'cosine')"
            )
            log.info("hnsw_index_ready", index=index_name)
            created += 1
        except duckdb.Error as exc:
            log.warning("hnsw_index_failed", index=index_name, error=str(exc))
    return created


# ── Public entry point ────────────────────────────────────────────────────────


def init_db(
    db_path: str | Path,
    *,
    load_vss: bool = True,
    migrations_dir: Path = DEFAULT_MIGRATIONS_DIR,
) -> dict[str, int | bool]:
    """Initialise or update the MÜŞAHİT DuckDB schema.

    Operations in order:
      1. Create parent directories (unless db_path is ``:memory:``).
      2. Open a connection with ``load_vss=False`` — VSS handling is done here,
         not in :func:`make_connection`, so the fallback logic can run.
      3. (If ``load_vss`` is True) attempt to load the VSS extension.
      4. Create ``schema_version`` table if absent.
      5. Apply pending migrations in numeric order.
      6. (If VSS loaded) create HNSW indices idempotently.

    Returns a status dict with three keys:

    * ``vss_loaded`` (bool): whether VSS was available for this run.
    * ``migrations_applied`` (int): number of new migrations applied.
    * ``hnsw_indices_created`` (int): count of HNSW ``CREATE INDEX`` calls
      that succeeded, including idempotent no-ops on existing indices. Always
      ``0`` when ``vss_loaded`` is ``False``.
    """
    log = get_logger("musahit.migrations")
    db_path = Path(db_path)

    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)

    # Open WITHOUT VSS — we manage the extension ourselves below.
    conn = make_connection(db_path, load_vss=False)
    try:
        vss_loaded = try_load_vss(conn, log) if load_vss else False

        ensure_schema_version_table(conn)
        migrations_applied = apply_pending_migrations(conn, migrations_dir, log)

        hnsw_indices_created = create_hnsw_indices(conn, log) if vss_loaded else 0

        result = {
            "vss_loaded": vss_loaded,
            "migrations_applied": migrations_applied,
            "hnsw_indices_created": hnsw_indices_created,
        }
        log.info("init_db_done", **result)
        return result
    finally:
        conn.close()
