"""Utilities for configuring consistent logging across Passivbot."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

TRACE_LEVEL = 5
TRACE_LEVEL_NAME = "TRACE"

DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(message)s"
DEFAULT_FORMAT_WITH_PREFIX = "%(asctime)s %(levelname)-8s [%(log_prefix)s] %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%dT%H:%M:%S"


class PrefixFilter(logging.Filter):
    """Filter that adds a log_prefix attribute to log records."""

    def __init__(self, prefix: str = ""):
        super().__init__()
        self.prefix = prefix

    def filter(self, record: logging.LogRecord) -> bool:
        record.log_prefix = self.prefix
        return True


class _MaxLevelFilter(logging.Filter):
    """Block records at or above a given level — used to split stdout from stderr."""

    def __init__(self, max_exclusive_level: int):
        super().__init__()
        self.max_level = max_exclusive_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self.max_level


_LOG_LEVEL_ALIASES = {
    "warning": 0,
    "warn": 0,
    "w": 0,
    "info": 1,
    "i": 1,
    "debug": 2,
    "d": 2,
    "trace": 3,
    "t": 3,
}


def normalize_log_level(value, default=None):
    """Return normalized log level 0-3 or default when invalid/missing."""
    if value is None:
        return default
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in _LOG_LEVEL_ALIASES:
            return _LOG_LEVEL_ALIASES[cleaned]
        try:
            value = float(cleaned)
        except ValueError:
            return default
    try:
        level = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(0, min(level, 3))


def resolve_log_level(cli_value, config_value, fallback=1):
    """Resolve final log level from CLI override and config value."""
    cli_level = normalize_log_level(cli_value, None)
    if cli_level is not None:
        return cli_level
    cfg_level = normalize_log_level(config_value, None)
    if cfg_level is not None:
        return cfg_level
    return fallback


def _ensure_trace_level() -> None:
    """Register the TRACE log level on the logging module if missing."""
    if logging.getLevelName(TRACE_LEVEL) != TRACE_LEVEL_NAME:
        logging.addLevelName(TRACE_LEVEL, TRACE_LEVEL_NAME)
    if getattr(logging, TRACE_LEVEL_NAME, None) != TRACE_LEVEL:
        setattr(logging, TRACE_LEVEL_NAME, TRACE_LEVEL)

    if not hasattr(logging.Logger, "trace"):

        def trace(self: logging.Logger, msg: str, *args, **kwargs) -> None:
            if self.isEnabledFor(TRACE_LEVEL):
                self._log(TRACE_LEVEL, msg, args, **kwargs)

        logging.Logger.trace = trace  # type: ignore[attr-defined]


def _normalize_debug(debug: Optional[int | str]) -> int:
    level = normalize_log_level(debug, None)
    if level is None:
        return 1
    return level


def _debug_to_level(debug: int) -> int:
    if debug <= 0:
        return logging.WARNING
    if debug == 1:
        return logging.INFO
    if debug == 2:
        return logging.DEBUG
    return TRACE_LEVEL


def configure_logging(
    debug: Optional[int | str] = 1,
    *,
    log_file: Optional[str] = None,
    rotation: bool = False,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    stream: bool = True,
    fmt: Optional[str] = None,
    datefmt: str = DEFAULT_DATEFMT,
    prefix: Optional[str] = None,
) -> None:
    """Initialise the root logger based on Passivbot's debug settings.

    Args:
        debug: Logging level (0=warning, 1=info, 2=debug, 3=trace)
        log_file: Optional path to log file
        rotation: Enable log rotation
        max_bytes: Max bytes per log file before rotation
        backup_count: Number of backup files to keep
        stream: Enable console output
        fmt: Custom log format (defaults based on prefix)
        datefmt: Date format string
        prefix: Optional prefix to add to all log messages (e.g., exchange name)
    """
    _ensure_trace_level()
    debug_level = _normalize_debug(debug)
    numeric_level = _debug_to_level(debug_level)

    # Choose format based on prefix
    if fmt is None:
        fmt = DEFAULT_FORMAT_WITH_PREFIX if prefix else DEFAULT_FORMAT

    formatter = logging.Formatter(fmt=fmt, datefmt=datefmt)
    handlers: list[logging.Handler] = []

    # Create prefix filter if needed
    prefix_filter = PrefixFilter(prefix or "") if prefix else None

    if stream:
        # Split streams: INFO/DEBUG/TRACE → stdout, WARNING+ → stderr.
        # launchd routes stdout to passivbot.log, stderr to passivbot_error.log,
        # so this keeps the error log focused on real problems instead of
        # a firehose of INFO chatter.
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        stdout_handler.setLevel(numeric_level)
        stdout_handler.addFilter(_MaxLevelFilter(logging.WARNING))
        if prefix_filter:
            stdout_handler.addFilter(prefix_filter)
        handlers.append(stdout_handler)

        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        stderr_handler.setLevel(max(numeric_level, logging.WARNING))
        if prefix_filter:
            stderr_handler.addFilter(prefix_filter)
        handlers.append(stderr_handler)

    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        if rotation:
            file_handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backup_count)
        else:
            file_handler = logging.FileHandler(path)
        file_handler.setFormatter(formatter)
        file_handler.setLevel(numeric_level)
        if prefix_filter:
            file_handler.addFilter(prefix_filter)
        handlers.append(file_handler)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()

    for handler in handlers:
        root.addHandler(handler)

    # Configure CCXT logger to only log at TRACE level.
    # CCXT logs full API request/response payloads at DEBUG, which is too noisy.
    # These payloads belong at TRACE (level 3) per log_analysis_prompt.md guidelines.
    ccxt_logger = logging.getLogger("ccxt")
    if debug_level >= 3:
        # TRACE mode: allow CCXT logs through
        ccxt_logger.setLevel(TRACE_LEVEL)
    else:
        # DEBUG and below: suppress CCXT's noisy API payloads
        # Set to WARNING so only actual warnings/errors from CCXT are shown
        ccxt_logger.setLevel(logging.WARNING)
