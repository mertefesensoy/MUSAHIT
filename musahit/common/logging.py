"""structlog configuration: JSON output, UTC timestamps, named loggers.

Call configure_logging() once at pipeline startup before any log statements.
All subsequent get_logger() calls return bound loggers sharing that config.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog


def configure_logging(
    log_level: str = "INFO",
    log_file: Path | None = None,
) -> None:
    """Configure structlog with JSON lines output and UTC timestamps.

    Installs a stdlib root handler so structlog output goes to stdout and,
    optionally, to a daily log file. The file is created (with parent dirs)
    if it does not exist.

    Args:
        log_level: Minimum log level string, e.g. "DEBUG" or "WARNING".
        log_file: If given, also write JSON lines to this path.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    # --- stdlib root logger ---
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    plain_fmt = logging.Formatter("%(message)s")

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(plain_fmt)
    root.addHandler(stdout_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(plain_fmt)
        root.addHandler(file_handler)

    # --- structlog ---
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Attach the JSON formatter to every stdlib handler
    json_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    for handler in root.handlers:
        handler.setFormatter(json_formatter)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a named structlog bound logger.

    The name appears as ``logger`` in every JSON log line, which is useful
    for filtering by module in log analysis tools.
    """
    return structlog.get_logger(name)
