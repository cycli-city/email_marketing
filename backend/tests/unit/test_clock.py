from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FakeClock, SystemClock


def test_system_clock_is_timezone_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_fake_clock_advances_and_sets() -> None:
    clock = FakeClock(datetime(2026, 3, 2, 8, 0, tzinfo=UTC))
    clock.advance(hours=1, minutes=30)
    assert clock.now() == datetime(2026, 3, 2, 9, 30, tzinfo=UTC)
    clock.set(datetime(2027, 1, 1, tzinfo=UTC))
    assert clock.now().year == 2027


def test_fake_clock_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime(2026, 1, 1))
    clock = FakeClock()
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set(datetime(2026, 1, 1))
