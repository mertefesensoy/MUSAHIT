"""Tests for musahit.common.db — connection factory and context manager."""

from __future__ import annotations

import duckdb
import pytest

from musahit.common.db import make_connection, open_connection


class TestMakeConnection:
    def test_in_memory_connection_is_usable(self) -> None:
        conn = make_connection(":memory:", load_vss=False)
        try:
            result = conn.execute("SELECT 42 AS answer").fetchone()
            assert result == (42,)
        finally:
            conn.close()

    def test_returns_duckdb_connection(self) -> None:
        conn = make_connection(":memory:", load_vss=False)
        try:
            assert isinstance(conn, duckdb.DuckDBPyConnection)
        finally:
            conn.close()

    def test_can_create_and_query_table(self) -> None:
        conn = make_connection(":memory:", load_vss=False)
        try:
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.execute("INSERT INTO t VALUES (1), (2), (3)")
            rows = conn.execute("SELECT sum(x) FROM t").fetchone()
            assert rows == (6,)
        finally:
            conn.close()

    def test_file_db_created_at_path(self, tmp_path) -> None:
        db_file = tmp_path / "test.duckdb"
        conn = make_connection(db_file, load_vss=False)
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        assert db_file.exists()


class TestOpenConnection:
    def test_context_manager_yields_connection(self) -> None:
        with open_connection(":memory:", load_vss=False) as conn:
            result = conn.execute("SELECT 99").fetchone()
            assert result == (99,)

    def test_connection_closed_after_context(self) -> None:
        with open_connection(":memory:", load_vss=False) as conn:
            pass
        with pytest.raises(duckdb.Error):
            conn.execute("SELECT 1")

    def test_context_manager_closes_on_exception(self) -> None:
        closed_conn = None
        try:
            with open_connection(":memory:", load_vss=False) as conn:
                closed_conn = conn
                raise ValueError("intentional test error")
        except ValueError:
            pass
        assert closed_conn is not None
        with pytest.raises(duckdb.Error):
            closed_conn.execute("SELECT 1")


@pytest.mark.skipif(
    True,  # Skip by default — requires internet to download VSS extension.
    reason="VSS extension install requires network access",
)
class TestVssExtension:
    def test_vss_loads_on_in_memory_db(self) -> None:
        conn = make_connection(":memory:", load_vss=True)
        try:
            conn.execute("SELECT * FROM duckdb_extensions() WHERE extension_name = 'vss'")
        finally:
            conn.close()
