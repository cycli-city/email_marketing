"""Small, forgiving readers for vendor JSON. Vendor data is untrusted: anything of the wrong type becomes ``None``."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.integrations.gmail.mime import ADDRESS_RE


def text(value: object, *, limit: int = 500) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def email(value: object) -> str | None:
    """A syntactically valid, lower-cased address, or ``None``. (Syntax only — deliverability is a separate question.)"""
    raw = text(value)
    if raw is None:
        return None
    candidate = (
        raw.lower().removeprefix("mailto:").split(";")[0].strip()
    )  # OSM allows several, ";"-separated
    return candidate if ADDRESS_RE.fullmatch(candidate) else None


def number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def integer(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def decimal_header(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value.strip())
    except InvalidOperation:
        return None


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def sequence(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
