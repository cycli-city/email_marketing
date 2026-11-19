"""Rate limiting.

* ``InMemoryRateLimiter`` — exact sliding-window log, per process. Memory is bounded (LRU over keys, and
  each key keeps at most ``max_requests`` timestamps) so a flood of random keys cannot exhaust RAM.
* ``RedisRateLimiter`` — sliding-window *counter* (two fixed windows, weighted). Costs 3 Redis commands
  per request and is shared across instances.
* ``FallbackRateLimiter`` — if Redis is unavailable the in-memory limiter keeps protecting the API
  (protection must never switch off just because a dependency is down).
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import structlog
from redis.asyncio import Redis

from app.core.clock import SYSTEM_CLOCK, Clock

logger = structlog.get_logger("ratelimit")


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


class RedisRateLimiter:
    def __init__(
        self,
        redis_provider: Callable[[], Redis],
        clock: Clock = SYSTEM_CLOCK,
        prefix: str = "aoi:rl",
    ) -> None:
        self._provider = redis_provider
        self._clock = clock
        self._prefix = prefix

    async def hit(self, key: str, limit: Limit) -> Decision:
        now = self._clock.now().timestamp()
        window = limit.window_seconds
        index = int(now // window)
        elapsed_fraction = (now % window) / window
        base = f"{self._prefix}:{limit.name}:{key}"
        current_key, previous_key = f"{base}:{index}", f"{base}:{index - 1}"

        async with self._provider().pipeline(transaction=False) as pipe:
            pipe.incr(current_key)
            pipe.expire(current_key, window * 2)
            pipe.get(previous_key)
            current, _, previous = await pipe.execute()

        estimated = int(previous or 0) * (1 - elapsed_fraction) + int(current)
        if estimated > limit.max_requests:
            retry_after = max(1, math.ceil(window - (now % window)))
            return Decision(False, limit.max_requests, 0, retry_after)
        return Decision(
            True, limit.max_requests, max(0, math.floor(limit.max_requests - estimated)), 0
        )


class FallbackRateLimiter:
    def __init__(
        self, primary: RateLimiter, fallback: RateLimiter, log_every_seconds: float = 60.0
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._log_every = log_every_seconds
        self._last_logged = 0.0

    async def hit(self, key: str, limit: Limit) -> Decision:
        try:
            return await self._primary.hit(key, limit)
        except (
            Exception
        ) as exc:  # deliberately broad: ANY primary failure must fall back, never fail open
            monotonic = time.monotonic()
            if monotonic - self._last_logged >= self._log_every:
                self._last_logged = monotonic
                logger.warning("ratelimit.primary_failed_using_fallback", error=type(exc).__name__)
            return await self._fallback.hit(key, limit)
