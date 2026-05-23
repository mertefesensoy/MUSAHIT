"""Tests for musahit.common.logging — structlog setup."""

from __future__ import annotations

import json
from pathlib import Path

from musahit.common.logging import configure_logging, get_logger


class TestConfigureLogging:
    def test_runs_without_error(self) -> None:
        configure_logging(log_level="WARNING")

    def test_accepts_debug_level(self) -> None:
        configure_logging(log_level="DEBUG")

    def test_accepts_lowercase_level(self) -> None:
        configure_logging(log_level="info")

    def test_creates_log_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "run.jsonl"
        configure_logging(log_file=log_file)
        assert log_file.exists()

    def test_creates_log_file_parent_dirs(self, tmp_path: Path) -> None:
        log_file = tmp_path / "nested" / "dir" / "run.jsonl"
        configure_logging(log_file=log_file)
        assert log_file.exists()


class TestGetLogger:
    def test_returns_bound_logger(self) -> None:
        configure_logging()
        logger = get_logger("test.module")
        assert logger is not None

    def test_different_names_return_different_loggers(self) -> None:
        configure_logging()
        a = get_logger("module.a")
        b = get_logger("module.b")
        assert a is not b

    def test_logger_has_info_method(self) -> None:
        configure_logging()
        logger = get_logger("test")
        assert callable(getattr(logger, "info", None))

    def test_logger_has_error_method(self) -> None:
        configure_logging()
        logger = get_logger("test")
        assert callable(getattr(logger, "error", None))

    def test_output_is_json(self, capfd) -> None:
        """Verify each log line is valid JSON."""
        configure_logging(log_level="DEBUG")
        logger = get_logger("json_test")
        logger.info("test event", key="value")
        captured = capfd.readouterr()
        lines = [line for line in captured.out.strip().splitlines() if line]
        assert lines, "Expected at least one log line"
        parsed = json.loads(lines[-1])
        assert isinstance(parsed, dict)
