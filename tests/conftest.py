"""Shared pytest fixtures for MÜŞAHİT test suite."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from musahit.common.config import Settings


@pytest.fixture()
def tmp_settings(tmp_path: Path) -> Settings:
    """Settings instance pointing at a temporary directory (no .env, no TOML)."""
    return Settings(
        db_path=tmp_path / "test.duckdb",
        briefings_dir=tmp_path / "briefings",
        logs_dir=tmp_path / "logs",
        data_dir=tmp_path / "data",
        piper_voice_path=tmp_path / "voices" / "tr_TR-dfki-medium.onnx",
    )


@pytest.fixture()
def mem_db() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB connection without VSS (safe for offline tests)."""
    conn = duckdb.connect(":memory:")
    yield conn
    conn.close()
