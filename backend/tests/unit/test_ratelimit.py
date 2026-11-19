from __future__ import annotations

from app.core.clock import FakeClock
from app.core.ratelimit import InMemoryRateLimiter, Limit

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

    async def test_a_flood_of_unique_keys_cannot_grow_memory_without_bound(self) -> None:
        limiter = InMemoryRateLimiter(FakeClock(), max_keys=100)
        for i in range(5_000):
            await limiter.hit(f"attacker-{i}", Limit("api", 5, 60))
        assert len(limiter._hits) <= 100
