"""Rate limiting.

The deployment is a single always-on process (ADR-008), so an in-process limiter is exact — no shared store is
needed (ADR-024: no Redis). Memory is bounded (LRU over keys, and each key keeps at most ``max_requests``
timestamps) so a flood of random keys cannot exhaust RAM. State resets on restart, which is acceptable: limits
exist to bound abuse, not to keep a ledger.
"""

from __future__ import annotations

import math
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Protocol

from app.core.clock import SYSTEM_CLOCK, Clock


@dataclass(frozen=True, slots=True)
class Limit:
    name: str
    max_requests: int
    window_seconds: int = 60


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int  # seconds; 0 when allowed


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: Limit) -> Decision: ...


class InMemoryRateLimiter:
    """Exact sliding-window log per (limit name, key)."""

    def __init__(self, clock: Clock = SYSTEM_CLOCK, max_keys: int = 50_000) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    async def hit(self, key: str, limit: Limit) -> Decision:
        now = self._clock.now().timestamp()
        bucket = f"{limit.name}:{key}"
        window = self._hits.get(bucket)
        if window is None:
            window = deque()
            self._hits[bucket] = window
            while len(self._hits) > self._max_keys:
                self._hits.popitem(last=False)  # evict least-recently-used key
        else:
            self._hits.move_to_end(bucket)

        cutoff = now - limit.window_seconds
        while window and window[0] <= cutoff:
            window.popleft()

        if len(window) >= limit.max_requests:
            retry_after = max(1, math.ceil(window[0] + limit.window_seconds - now))
            return Decision(False, limit.max_requests, 0, retry_after)
        window.append(now)
        return Decision(True, limit.max_requests, limit.max_requests - len(window), 0)
