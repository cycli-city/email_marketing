"""Proactive per-minute pacing (in-process; ADR-024 — one process owns the workers).

Free tiers often cap *tokens or requests per minute* (e.g. Groq: 8,000 tokens/min per model). Sending into a
known-full window only earns a 429, so the manager asks the pacer first and either waits a moment or tries another
provider. A 429 that still happens is handled by the normal rate-limit path.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

WINDOW_SECONDS = 60.0


class MinuteWindow:
    """Sliding 60-second window of consumed weight, capped at ``limit``."""

    def __init__(self, limit: float) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        self.limit = limit
        self._events: deque[tuple[datetime, float]] = deque()
        self._total = 0.0

    def _prune(self, now: datetime) -> None:
        while self._events and (now - self._events[0][0]).total_seconds() >= WINDOW_SECONDS:
            _, weight = self._events.popleft()
            self._total -= weight
        if not self._events:
            self._total = 0.0  # kill floating-point drift

    def wait_seconds(self, weight: float, now: datetime) -> float:
        """How long until ``weight`` fits (0 = fits now). A request larger than the whole budget is let through
        once the window is empty — the provider is the judge, and refusing forever would deadlock the task."""
        self._prune(now)
        if self._total + weight <= self.limit or not self._events:
            return 0.0
        needed = self._total + weight - self.limit
        freed = 0.0
        for stamp, w in self._events:
            freed += w
            if freed >= needed:
                return max(0.0, WINDOW_SECONDS - (now - stamp).total_seconds())
        return WINDOW_SECONDS

    def commit(self, weight: float, now: datetime) -> None:
        self._prune(now)
        self._events.append((now, weight))
        self._total += weight

    @property
    def used(self) -> float:
        return self._total


class Pacer:
    """One ``MinuteWindow`` per provider key, created lazily from the provider's configured limit."""

    def __init__(self) -> None:
        self._windows: dict[str, MinuteWindow] = {}

    def wait_seconds(self, key: str, limit: float | None, weight: float, now: datetime) -> float:
        window = self._window(key, limit)
        return 0.0 if window is None else window.wait_seconds(weight, now)

    def commit(self, key: str, limit: float | None, weight: float, now: datetime) -> None:
        window = self._window(key, limit)
        if window is not None:
            window.commit(weight, now)

    def _window(self, key: str, limit: float | None) -> MinuteWindow | None:
        if not limit:
            self._windows.pop(key, None)
            return None
        existing = self._windows.get(key)
        if existing is None or existing.limit != limit:
            # First use, or the limit was edited on the settings page → start a fresh window.
            existing = self._windows[key] = MinuteWindow(limit)
        return existing
