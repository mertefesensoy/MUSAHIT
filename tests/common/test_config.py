"""Tests for musahit.common.config — Settings and _TomlSource."""

from __future__ import annotations

from pathlib import Path

import pytest

from musahit.common.config import Settings


class TestSettingsDefaults:
    def test_db_path_default(self) -> None:
        s = Settings()
        assert s.db_path == Path("data/musahit.duckdb")

    def test_ollama_base_url_default(self) -> None:
        s = Settings()
        assert s.ollama_base_url == "http://localhost:11434"

    def test_arc_thresholds_from_adr_008(self) -> None:
        s = Settings()
        assert s.arc_cosine_threshold == 0.55
        assert s.arc_jaccard_threshold == 0.4
        assert s.arc_window_days == 30
        assert s.arc_open_to_watch_days == 7

    def test_dashboard_defaults(self) -> None:
        s = Settings()
        assert s.dashboard_host == "127.0.0.1"
        assert s.dashboard_port == 8001

    def test_reddit_creds_empty_by_default(self) -> None:
        s = Settings()
        assert s.reddit_client_id == ""
        assert s.reddit_client_secret == ""

    def test_smtp_empty_by_default(self) -> None:
        s = Settings()
        assert s.smtp_host == ""
        assert s.operator_email == ""

    def test_retention_days(self) -> None:
        s = Settings()
        assert s.raw_articles_retention_days == 90
        assert s.articles_retention_days == 365
        assert s.backup_retention_days == 30

    def test_bootstrap_days(self) -> None:
        s = Settings()
        assert s.bootstrap_days == 7

    def test_min_free_disk_gb(self) -> None:
        s = Settings()
        assert s.min_free_disk_gb == 5


class TestSettingsOverride:
    def test_programmatic_override(self) -> None:
        s = Settings(db_path=Path("custom/path.duckdb"))
        assert s.db_path == Path("custom/path.duckdb")

    def test_arc_threshold_override(self) -> None:
        s = Settings(arc_cosine_threshold=0.70)
        assert s.arc_cosine_threshold == 0.70

    def test_string_path_coerced_to_path(self) -> None:
        s = Settings(db_path="some/other.duckdb")  # type: ignore[arg-type]
        assert isinstance(s.db_path, Path)


class TestTomlSource:
    def test_loads_from_config_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_toml = tmp_path / "config.toml"
        config_toml.write_text(
            'db_path = "data/fromtoml.duckdb"\ndashboard_port = 9999\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        s = Settings()
        assert s.db_path == Path("data/fromtoml.duckdb")
        assert s.dashboard_port == 9999

    def test_missing_config_toml_uses_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        s = Settings()
        assert s.dashboard_port == 8001

    def test_env_var_overrides_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_toml = tmp_path / "config.toml"
        config_toml.write_text("dashboard_port = 9999\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DASHBOARD_PORT", "7777")
        s = Settings()
        assert s.dashboard_port == 7777
