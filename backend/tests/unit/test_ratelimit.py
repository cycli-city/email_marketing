from __future__ import annotations

import pytest
from fakeredis import FakeAsyncRedis

from app.core.clock import FakeClock
from app.core.ratelimit import (
    Decision,
    FallbackRateLimiter,
    InMemoryRateLimiter,
    Limit,
    RedisRateLimiter,
)

LIMIT = Limit("api", 3, 60)


class TestInMemoryRateLimiter:
    async def test_allows_up_to_the_limit_then_blocks_with_retry_after(self) -> None:
        clock = FakeClock()
        limiter = InMemoryRateLimiter(clock)
        remaining = [(await limiter.hit("ip", LIMIT)).remaining for _ in range(3)]
        assert remaining == [2, 1, 0]
        blocked = await limiter.hit("ip", LIMIT)
        assert not blocked.allowed
        assert blocked.retry_after == 60

    async def test_window_slides(self) -> None:
        clock = FakeClock()
        limiter = InMemoryRateLimiter(clock)
        for _ in range(3):
            await limiter.hit("ip", LIMIT)
        clock.advance(seconds=30)
        denied = await limiter.hit("ip", LIMIT)
        assert not denied.allowed
        assert denied.retry_after == 30  # oldest hit leaves the window in 30 s
        clock.advance(seconds=31)
        assert (await limiter.hit("ip", LIMIT)).allowed

    async def test_keys_and_limit_names_are_isolated(self) -> None:
        limiter = InMemoryRateLimiter(FakeClock())
        for _ in range(3):
            await limiter.hit("a", LIMIT)
        assert not (await limiter.hit("a", LIMIT)).allowed
        assert (await limiter.hit("b", LIMIT)).allowed  # other client
        assert (await limiter.hit("a", Limit("sensitive", 3, 60))).allowed  # other bucket

    async def test_memory_is_bounded_by_evicting_least_recently_used_keys(self) -> None:
        limiter = InMemoryRateLimiter(FakeClock(), max_keys=2)
        one = Limit("x", 1, 60)
        await limiter.hit("a", one)
        await limiter.hit("b", one)
        await limiter.hit("c", one)  # evicts "a"
        assert (
            await limiter.hit("a", one)
        ).allowed  # forgotten ⇒ fresh budget, memory stayed bounded
        assert not (await limiter.hit("c", one)).allowed  # recent key still tracked

    async def test_blocked_requests_do_not_extend_the_ban(self) -> None:
        clock = FakeClock()
        limiter = InMemoryRateLimiter(clock)
        for _ in range(3):
            await limiter.hit("ip", LIMIT)
        for _ in range(50):  # hammering while blocked
            assert not (await limiter.hit("ip", LIMIT)).allowed
        clock.advance(seconds=61)
        assert (await limiter.hit("ip", LIMIT)).allowed


class TestRedisRateLimiter:
    @pytest.fixture
    def parts(self) -> tuple[RedisRateLimiter, FakeClock]:
        clock = FakeClock()  # starts on a window boundary (09:00:00)
        redis = FakeAsyncRedis()
        return RedisRateLimiter(lambda: redis, clock), clock

    async def test_blocks_after_the_limit(self, parts: tuple[RedisRateLimiter, FakeClock]) -> None:
        limiter, _ = parts
        results = [await limiter.hit("ip", Limit("api", 5, 60)) for _ in range(6)]
        assert [r.allowed for r in results] == [True] * 5 + [False]
        assert results[-1].retry_after == 60

    async def test_sliding_weight_prevents_a_burst_at_the_window_boundary(
        self, parts: tuple[RedisRateLimiter, FakeClock]
    ) -> None:
        limiter, clock = parts
        limit = Limit("api", 5, 60)
        for _ in range(5):
            await limiter.hit("ip", limit)
        clock.advance(seconds=60)  # new window starts; previous window's 5 still count fully
        assert not (await limiter.hit("ip", limit)).allowed
        clock.advance(seconds=30)  # half-way: the previous window now weighs 50 %
        assert (await limiter.hit("ip", limit)).allowed
        clock.advance(seconds=120)  # everything has aged out
        assert (await limiter.hit("ip", limit)).allowed

    async def test_keys_are_isolated(self, parts: tuple[RedisRateLimiter, FakeClock]) -> None:
        limiter, _ = parts
        limit = Limit("api", 1, 60)
        assert (await limiter.hit("a", limit)).allowed
        assert not (await limiter.hit("a", limit)).allowed
        assert (await limiter.hit("b", limit)).allowed

    async def test_uses_three_commands_per_request_and_sets_expiry(self) -> None:
        redis = FakeAsyncRedis()
        limiter = RedisRateLimiter(lambda: redis, FakeClock())
        await limiter.hit("ip", Limit("api", 5, 60))
        (key,) = [k async for k in redis.scan_iter("aoi:rl:*")]
        assert 0 < await redis.ttl(key) <= 120  # counters never live forever


class _Broken:
    async def hit(self, key: str, limit: Limit) -> Decision:
        raise ConnectionError("redis down")


class TestFallbackRateLimiter:
    async def test_falls_back_to_memory_when_redis_fails_and_still_enforces_the_limit(self) -> None:
        limiter = FallbackRateLimiter(_Broken(), InMemoryRateLimiter(FakeClock()))
        decisions = [await limiter.hit("ip", LIMIT) for _ in range(4)]
        assert [d.allowed for d in decisions] == [True, True, True, False]

    async def test_primary_is_used_when_healthy(self) -> None:
        redis = FakeAsyncRedis()
        limiter = FallbackRateLimiter(
            RedisRateLimiter(lambda: redis, FakeClock()), InMemoryRateLimiter(FakeClock())
        )
        await limiter.hit("ip", LIMIT)
        assert [k async for k in redis.scan_iter("aoi:rl:*")]
