"""Structured JSON logging (structlog). Secrets are never logged: values are redacted by key name."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_REDACT_KEYS = frozenset(
    {
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "authorization",
        "cookie",
        "client_secret",
        "encryption_key",
        "jwt_secret",
        "credentials",
    }
)


def _redact(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict):
        if key.lower() in _REDACT_KEYS:
            event_dict[key] = "***redacted***"
    return event_dict


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    # Some vendors only take their key in the query string; the HTTP client libraries log full request URLs at INFO.
    # Keep them quiet whatever the root logger is set to, so a key can never reach a log line that way.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact,
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
