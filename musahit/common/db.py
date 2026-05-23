"""DuckDB connection factory and context manager.

All pipeline stages share the same DB file. The dashboard reads it concurrently
but never writes — DuckDB's single-writer model is safe here.

VSS extension (vector similarity search) is installed and loaded on every
connection. The INSTALL call is idempotent; it re-downloads only if the
extension is absent from the local DuckDB extension directory.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import duckdb


def make_connection(
    db_path: str | Path = "data/musahit.duckdb",
    *,
    load_vss: bool = True,
) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection to db_path and optionally load VSS.

    Args:
        db_path: Path to the DuckDB file, or ":memory:" for an in-memory DB.
        load_vss: Whether to install and load the VSS extension. Set False
            in unit tests that don't need vector search and run offline.

    Returns:
        An open DuckDB connection ready for queries.
    """
    conn = duckdb.connect(str(db_path))
    if load_vss:
        _load_vss(conn)
    return conn


def _load_vss(conn: duckdb.DuckDBPyConnection) -> None:
    """Install (idempotent) and load the VSS extension."""
    conn.execute("INSTALL vss")
    conn.execute("LOAD vss")


@contextmanager
def open_connection(
    db_path: str | Path = "data/musahit.duckdb",
    *,
    load_vss: bool = True,
) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """Context manager: yield a DuckDB connection and close it on exit.

    Example::

        with open_connection() as conn:
            conn.execute("SELECT 1")
    """
    conn = make_connection(db_path, load_vss=load_vss)
    try:
        yield conn
    finally:
        conn.close()
