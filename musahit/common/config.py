"""Pydantic-settings based configuration.

Priority (highest to lowest):
  1. Programmatic init kwargs
  2. Environment variables
  3. .env file (secrets)
  4. config.toml (non-secret defaults)
  5. Field defaults defined here

The split keeps secrets out of config.toml (which is committed) and out of
environment variables (which appear in process listings).
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class _TomlSource(PydanticBaseSettingsSource):
    """Read non-secret defaults from config.toml at the working directory."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        toml_path: Path = Path("config.toml"),
    ) -> None:
        super().__init__(settings_cls)
        self._data: dict[str, Any] = {}
        if toml_path.exists():
            with toml_path.open("rb") as fh:
                self._data = tomllib.load(fh)

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        value = self._data.get(field_name)
        return value, field_name, field_name in self._data

    def field_is_complex(self, field: FieldInfo) -> bool:
        return False

    def __call__(self) -> dict[str, Any]:
        return {k: v for k, v in self._data.items() if k in self.settings_cls.model_fields}


class Settings(BaseSettings):
    """All runtime configuration for MÜŞAHİT.

    Secrets (Reddit creds, SMTP) come from .env only and have empty defaults
    so the system starts without them (ingest will gracefully skip affected
    sources rather than crashing).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Paths ---
    db_path: Path = Path("data/musahit.duckdb")
    briefings_dir: Path = Path("briefings")
    logs_dir: Path = Path("logs")
    data_dir: Path = Path("data")

    # --- Piper TTS (ADR-010) ---
    piper_voice_path: Path = Path("voices/tr_TR-dfki-medium.onnx")

    # --- Ollama (ADR-002) ---
    ollama_base_url: str = "http://localhost:11434"
    worker_model: str = "qwen2.5:7b-instruct-q4_K_M"
    writer_model: str = "trendyol-llm-7b-q4"
    embed_model: str = "bge-m3:latest"

    # --- Worker model parameters (ADR-002) ---
    worker_temperature: float = 0.1
    worker_max_tokens: int = 512
    worker_max_retries: int = 2

    # --- Writer model parameters (ADR-002) ---
    writer_temperature: float = 0.3
    writer_max_tokens: int = 4096

    # --- Arc linking thresholds (ADR-008) ---
    arc_cosine_threshold: float = 0.55
    arc_jaccard_threshold: float = 0.4
    arc_window_days: int = 30
    arc_open_to_watch_days: int = 7
    arc_watch_to_resolved_days: int = 30

    # --- Bootstrap period (BOOTSTRAP.md) ---
    bootstrap_days: int = 7

    # --- Disk pressure guard (ADR-012) ---
    min_free_disk_gb: int = 5

    # --- HTTP fetch defaults (ADR-003) ---
    default_rate_limit_seconds: int = 5
    default_timeout_seconds: int = 60

    # --- Reddit filter thresholds (ADR-003) ---
    reddit_min_score: int = 50
    reddit_min_comments: int = 25

    # --- Reddit credentials (.env only) ---
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "MUSAHIT/0.1 (personal OSINT)"

    # --- SMTP failure alerts (.env only, optional) ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    operator_email: str = ""

    # --- Dashboard (ADR-011) ---
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8001

    # --- Retention policies in days (ADR-012) ---
    raw_articles_retention_days: int = 90
    articles_retention_days: int = 365
    backup_retention_days: int = 30
    log_retention_days: int = 90

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _TomlSource(settings_cls),
            file_secret_settings,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide singleton Settings instance.

    Cached after first call so config.toml and .env are read exactly once.
    Tests that need custom settings should call Settings(...) directly and
    NOT go through get_settings().
    """
    return Settings()
