"""Rotating, content-redacted local diagnostics."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from typing import Any, cast

from copper_pilot_cli.copper_config import paths

_SECRET_PATTERNS = (
    re.compile(r"cf_live_[A-Za-z0-9._~-]+"),
    re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)(flow_token[\"']?\s*[:=]\s*[\"']?)[^\s&\"']+"),
)


def redact(value: object) -> str:
    """Remove known credentials and omit multiline user/tool content."""
    text = str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]" if pattern.groups else "[REDACTED]", text)
    if "\n" in text:
        first, *rest = text.splitlines()
        text = f"{first} … [{len(rest)} lines omitted]"
    return text[:2_000]


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        copied = logging.makeLogRecord(record.__dict__.copy())
        copied.msg = redact(copied.msg)
        copied.args = tuple(redact(item) for item in copied.args) if copied.args else ()
        return super().format(copied)


def configure_diagnostics() -> None:
    """Configure a bounded local log without console or telemetry handlers."""
    logger = logging.getLogger("copper_pilot_cli")
    if any(getattr(handler, "_copper_diagnostics", False) for handler in logger.handlers):
        return
    directory = paths().logs
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        directory / "copper-pilot.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    cast(Any, handler)._copper_diagnostics = True
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
