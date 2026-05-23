"""Tests for musahit.common.migrations — schema init and migration runner.

All tests run offline (load_vss=False or monkeypatched). The VSS-requires-network
path is exercised via monkeypatching _install_and_load_vss.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from musahit.common import migrations
from musahit.common.migrations import init_db

# ── Helpers ────────────────────────────────────────────────────────────────────

EXPECTED_TABLES = {
    "sources",
    "pipeline_runs",
    "arcs",
    "arc_centroids",
    "raw_articles",
    "articles",
    "article_embeddings",
    "clusters",
    "cluster_articles",
    "cluster_embeddings",
    "ingest_log",
    "promotion_log",
    "manual_overrides",
    "briefings",
    "schema_version",
}

EXPECTED_INDICES = {
    "idx_articles_published",
    "idx_articles_source",
    "idx_clusters_final_defcon",
    "idx_clusters_created",
    "idx_clusters_arc",
    "idx_arcs_state",
    "idx_ingest_log_run",
}


def _tables(conn: duckdb.DuckDBPyConnection) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    return {row[0] for row in rows}


def _index_names(conn: duckdb.DuckDBPyConnection) -> set[str]:
    rows = conn.execute("SELECT index_name FROM duckdb_indexes()").fetchall()
    return {row[0] for row in rows}


def _open_ro(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Open the DB read-only for assertions."""
    return duckdb.connect(str(db_path))


# ── TestFreshInit ──────────────────────────────────────────────────────────────


class TestFreshInit:
    def test_returns_expected_dict(self, tmp_path: Path) -> None:
        result = init_db(tmp_path / "x.duckdb", load_vss=False)
        assert result == {
            "vss_loaded": False,
            "migrations_applied": 1,
            "hnsw_indices_created": 0,
        }

    def test_all_tables_created(self, tmp_path: Path) -> None:
        init_db(tmp_path / "x.duckdb", load_vss=False)
        with _open_ro(tmp_path / "x.duckdb") as conn:
            assert EXPECTED_TABLES.issubset(_tables(conn))

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        db_path = tmp_path / "nested" / "data" / "musahit.duckdb"
        init_db(db_path, load_vss=False)
        assert db_path.exists()

    def test_in_memory_path_skips_mkdir(self) -> None:
        # :memory: must not attempt to create parent dirs (it has no path).
        result = init_db(":memory:", load_vss=False)
        assert result["migrations_applied"] == 1


# ── TestRerunIsNoOp ────────────────────────────────────────────────────────────


class TestRerunIsNoOp:
    def test_second_run_applies_zero_migrations(self, tmp_path: Path) -> None:
        db_path = tmp_path / "x.duckdb"
        init_db(db_path, load_vss=False)
        result2 = init_db(db_path, load_vss=False)
        assert result2["migrations_applied"] == 0

    def test_schema_version_row_count_unchanged(self, tmp_path: Path) -> None:
        db_path = tmp_path / "x.duckdb"
        init_db(db_path, load_vss=False)
        init_db(db_path, load_vss=False)
        with _open_ro(db_path) as conn:
            count = conn.execute("SELECT count(*) FROM schema_version").fetchone()[0]
        assert count == 1

    def test_second_run_does_not_raise(self, tmp_path: Path) -> None:
        db_path = tmp_path / "x.duckdb"
        init_db(db_path, load_vss=False)
        init_db(db_path, load_vss=False)  # must not raise


# ── TestSchemaVersionRow ───────────────────────────────────────────────────────


class TestSchemaVersionRow:
    def test_version_description_applied_at(self, tmp_path: Path) -> None:
        db_path = tmp_path / "x.duckdb"
        init_db(db_path, load_vss=False)
        with _open_ro(db_path) as conn:
            row = conn.execute(
                "SELECT version, description, applied_at FROM schema_version WHERE version = 1"
            ).fetchone()
        assert row is not None
        version, description, applied_at = row
        assert version == 1
        assert description == "initial_schema"
        assert applied_at is not None


# ── TestVssDisabledFallback ────────────────────────────────────────────────────


class TestVssDisabledFallback:
    def test_vss_false_in_result(self, tmp_path: Path) -> None:
        result = init_db(tmp_path / "x.duckdb", load_vss=False)
        assert result["vss_loaded"] is False
        assert result["hnsw_indices_created"] == 0

    def test_no_hnsw_indices_in_catalog(self, tmp_path: Path) -> None:
        db_path = tmp_path / "x.duckdb"
        init_db(db_path, load_vss=False)
        with _open_ro(db_path) as conn:
            hnsw = {n for n in _index_names(conn) if "hnsw" in n.lower()}
        assert hnsw == set()

    def test_schema_is_complete_without_vss(self, tmp_path: Path) -> None:
        init_db(tmp_path / "x.duckdb", load_vss=False)
        with _open_ro(tmp_path / "x.duckdb") as conn:
            assert EXPECTED_TABLES.issubset(_tables(conn))


# ── TestVssLoadFailureIsSoft ───────────────────────────────────────────────────


class TestVssLoadFailureIsSoft:
    """The critical VSS fallback: load_vss=True + extension failure keeps going."""

    def test_vss_error_is_not_reraised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(conn: duckdb.DuckDBPyConnection) -> None:
            raise duckdb.Error("simulated: VSS extension server unavailable")

        monkeypatch.setattr(migrations, "_install_and_load_vss", _fail)
        # Must not raise.
        result = init_db(tmp_path / "x.duckdb", load_vss=True)
        assert result["vss_loaded"] is False

    def test_migrations_still_applied_after_vss_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(conn: duckdb.DuckDBPyConnection) -> None:
            raise duckdb.Error("simulated")

        monkeypatch.setattr(migrations, "_install_and_load_vss", _fail)
        result = init_db(tmp_path / "x.duckdb", load_vss=True)
        assert result["migrations_applied"] == 1

    def test_schema_complete_after_vss_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(conn: duckdb.DuckDBPyConnection) -> None:
            raise duckdb.Error("simulated")

        monkeypatch.setattr(migrations, "_install_and_load_vss", _fail)
        db_path = tmp_path / "x.duckdb"
        init_db(db_path, load_vss=True)
        with _open_ro(db_path) as conn:
            assert EXPECTED_TABLES.issubset(_tables(conn))


# ── TestHnswPerIndexSoftFail ───────────────────────────────────────────────────


class TestHnswPerIndexSoftFail:
    """HNSW index creation fails gracefully when VSS is faked but index DDL errors."""

    def test_bogus_index_does_not_abort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pretend VSS loaded — but target a nonexistent table so CREATE INDEX fails.
        monkeypatch.setattr(migrations, "try_load_vss", lambda conn, log: True)
        monkeypatch.setattr(
            migrations,
            "HNSW_INDICES",
            [("idx_bogus", "nonexistent_table", "missing_col")],
        )
        # Must not raise.
        result = init_db(tmp_path / "x.duckdb", load_vss=True)
        assert result["vss_loaded"] is True
        assert result["hnsw_indices_created"] == 0

    def test_schema_complete_after_hnsw_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(migrations, "try_load_vss", lambda conn, log: True)
        monkeypatch.setattr(
            migrations,
            "HNSW_INDICES",
            [("idx_bogus", "nonexistent_table", "missing_col")],
        )
        db_path = tmp_path / "x.duckdb"
        init_db(db_path, load_vss=True)
        with _open_ro(db_path) as conn:
            assert EXPECTED_TABLES.issubset(_tables(conn))


# ── TestForeignKeyConstraintsEnforced ─────────────────────────────────────────


class TestForeignKeyConstraintsEnforced:
    def test_raw_article_rejects_missing_source(self, tmp_path: Path) -> None:
        init_db(tmp_path / "x.duckdb", load_vss=False)
        with _open_ro(tmp_path / "x.duckdb") as conn, pytest.raises(duckdb.ConstraintException):
            conn.execute(
                "INSERT INTO raw_articles (id, source_id, url, fetched_at) "
                "VALUES ('r1', 'does_not_exist', 'http://x.com', CURRENT_TIMESTAMP)"
            )

    def test_raw_article_accepts_valid_source(self, tmp_path: Path) -> None:
        init_db(tmp_path / "x.duckdb", load_vss=False)
        with _open_ro(tmp_path / "x.duckdb") as conn:
            conn.execute(
                "INSERT INTO sources "
                "(id, display_name, band, tier, kind, url, fragility) "
                "VALUES ('s1', 'Test', 'centrist', 'news', 'RSS', 'http://t.com', 'ROBUST')"
            )
            conn.execute(
                "INSERT INTO raw_articles (id, source_id, url, fetched_at) "
                "VALUES ('r1', 's1', 'http://x.com', CURRENT_TIMESTAMP)"
            )
            count = conn.execute("SELECT count(*) FROM raw_articles").fetchone()[0]
        assert count == 1


# ── TestMigrationDirectoryOverride ────────────────────────────────────────────


class TestMigrationDirectoryOverride:
    def test_empty_dir_applies_zero_migrations(self, tmp_path: Path) -> None:
        empty_dir = tmp_path / "no_migrations"
        empty_dir.mkdir()
        result = init_db(tmp_path / "x.duckdb", load_vss=False, migrations_dir=empty_dir)
        assert result["migrations_applied"] == 0

    def test_schema_version_table_exists_with_empty_dir(self, tmp_path: Path) -> None:
        """ensure_schema_version_table runs before the migration scan."""
        empty_dir = tmp_path / "no_migrations"
        empty_dir.mkdir()
        db_path = tmp_path / "x.duckdb"
        init_db(db_path, load_vss=False, migrations_dir=empty_dir)
        with _open_ro(db_path) as conn:
            count = conn.execute("SELECT count(*) FROM schema_version").fetchone()[0]
        assert count == 0  # table exists but is empty


# ── TestExpectedIndexNamesExactly ──────────────────────────────────────────────


class TestExpectedIndexNamesExactly:
    def test_all_seven_expected_indices_present(self, tmp_path: Path) -> None:
        db_path = tmp_path / "x.duckdb"
        init_db(db_path, load_vss=False)
        with _open_ro(db_path) as conn:
            actual = _index_names(conn)
        assert EXPECTED_INDICES.issubset(actual), f"Missing indices: {EXPECTED_INDICES - actual}"

    def test_no_hnsw_indices_without_vss(self, tmp_path: Path) -> None:
        db_path = tmp_path / "x.duckdb"
        init_db(db_path, load_vss=False)
        with _open_ro(db_path) as conn:
            hnsw_names = {n for n in _index_names(conn) if "hnsw" in n.lower()}
        assert hnsw_names == set(), f"Unexpected HNSW indices: {hnsw_names}"
