"""Injectable clock so time-dependent rules (send windows, follow-up due dates, cooldowns) are testable."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Deterministic clock for tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 5, 9, 0, tzinfo=UTC)  # a Monday, 09:00 UTC
        if self._now.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware datetime")

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware datetime")
        self._now = value


SYSTEM_CLOCK: Clock = SystemClock()
