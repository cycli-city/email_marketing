from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.providers.pacing import MinuteWindow, Pacer

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


class TestMinuteWindow:
    def test_fits_until_the_budget_is_spent(self) -> None:
        w = MinuteWindow(100)
        assert w.wait_seconds(60, T0) == 0
        w.commit(60, T0)
        assert w.wait_seconds(40, T0) == 0
        w.commit(40, T0)
        assert w.wait_seconds(1, T0) > 0

    def test_wait_is_until_enough_old_weight_leaves_the_window(self) -> None:
        w = MinuteWindow(100)
        w.commit(70, at(0))
        w.commit(30, at(20))
        # Need 50 more; freeing the first event (70) is enough → it leaves at t=60.
        assert w.wait_seconds(50, at(30)) == pytest.approx(30)
        # Need 100 → must wait for both → the later one leaves at t=80.
        assert w.wait_seconds(100, at(30)) == pytest.approx(50)

    def test_old_events_expire(self) -> None:
        w = MinuteWindow(100)
        w.commit(100, at(0))
        assert w.wait_seconds(100, at(59.9)) > 0, "still inside the 60 s window"
        assert w.wait_seconds(100, at(60)) == 0
        assert w.used == 0, "the expired event was dropped, not just ignored"

    def test_a_request_bigger_than_the_whole_budget_is_not_deadlocked(self) -> None:
        w = MinuteWindow(100)
        assert w.wait_seconds(500, T0) == 0, "empty window: let the provider decide"
        w.commit(10, T0)
        assert w.wait_seconds(500, T0) > 0, "…but it still waits for the window to empty"

    def test_rejects_a_non_positive_limit(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            MinuteWindow(0)


class TestPacer:
    def test_providers_are_paced_independently(self) -> None:
        pacer = Pacer()
        pacer.commit("a", 100, 90, T0)
        assert pacer.wait_seconds("a", 100, 20, T0) > 0
        assert pacer.wait_seconds("b", 100, 20, T0) == 0

    def test_no_limit_means_no_pacing(self) -> None:
        pacer = Pacer()
        pacer.commit("a", None, 10**9, T0)
        assert pacer.wait_seconds("a", None, 10**9, T0) == 0

    def test_editing_the_limit_starts_a_fresh_window(self) -> None:
        pacer = Pacer()
        pacer.commit("a", 100, 100, T0)
        assert pacer.wait_seconds("a", 100, 10, T0) > 0
        assert pacer.wait_seconds("a", 1000, 10, T0) == 0
