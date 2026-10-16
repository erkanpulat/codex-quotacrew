"""Structured, redacting logging setup.

All log records pass through a redacting filter so credential-shaped values are
masked before they hit a handler. Logs go to a rotating file under the data dir
and, optionally, to stderr. Nothing here writes ``auth.json`` contents.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from codex_account_manager.core.paths import paths
from codex_account_manager.core.redaction import redact_text

_CONFIGURED = False


class _RedactingFilter(logging.Filter):
    """Mask secret-shaped substrings in the final formatted message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_text(record.getMessage())
            record.args = ()
        except Exception:
            record.msg = "***log redaction failed***"
            record.args = ()
        return True


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def configure_logging(level: str = "INFO", *, to_stderr: bool = False) -> None:
    """Idempotently configure application logging."""
    global _CONFIGURED
    if _CONFIGURED:
        logging.getLogger().setLevel(level.upper())
        return

    paths.ensure()
    log_file: Path = paths.logs_dir / "account-manager.log"

    root = logging.getLogger()
    root.setLevel(level.upper())

    fmt = _RedactingFormatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redactor = _RedactingFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    if to_stderr:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        stream.addFilter(redactor)
        root.addHandler(stream)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
