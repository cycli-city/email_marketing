from __future__ import annotations

import random

import pytest

from app.core.retry import BackoffPolicy, backoff_seconds

NO_JITTER = BackoffPolicy(base_seconds=2, factor=2, max_seconds=60, jitter="none")


class TestExponentialBackoff:
    def test_grows_exponentially_then_caps(self) -> None:
        delays = [backoff_seconds(NO_JITTER, n) for n in range(1, 9)]
        assert delays == [2, 4, 8, 16, 32, 60, 60, 60]

    def test_equal_jitter_stays_between_half_and_full_and_is_never_zero(self) -> None:
        policy = BackoffPolicy(base_seconds=10, factor=2, max_seconds=1000, jitter="equal")
        rng = random.Random(1)
        for attempt in range(1, 8):
            ceiling = min(10 * 2 ** (attempt - 1), 1000)
            for _ in range(200):
                value = backoff_seconds(policy, attempt, rng=rng)
                assert ceiling / 2 <= value <= ceiling

    def test_full_jitter_is_bounded_by_the_cap(self) -> None:
        policy = BackoffPolicy(base_seconds=10, factor=2, max_seconds=30, jitter="full")
        rng = random.Random(2)
        assert all(0 <= backoff_seconds(policy, 6, rng=rng) <= 30 for _ in range(500))

    def test_jitter_actually_varies(self) -> None:
        policy = BackoffPolicy(jitter="equal")
        rng = random.Random(3)
        assert len({round(backoff_seconds(policy, 3, rng=rng), 3) for _ in range(50)}) > 20

    def test_rejects_attempt_zero(self) -> None:
        with pytest.raises(ValueError, match="attempt"):
            backoff_seconds(NO_JITTER, 0)


class TestRetryAfter:
    def test_always_waits_at_least_the_provider_requested_time(self) -> None:
        rng = random.Random(4)
        for _ in range(500):
            assert backoff_seconds(NO_JITTER, 1, retry_after=30, rng=rng) >= 30

    def test_adds_at_most_ten_percent_jitter(self) -> None:
        rng = random.Random(5)
        assert all(
            30 <= backoff_seconds(NO_JITTER, 1, retry_after=30, rng=rng) <= 33 for _ in range(500)
        )

    def test_retry_after_beats_the_exponential_cap(self) -> None:
        # attempt 1 would be 2 s, the cap is 60 s — but the provider said wait 600 s, so we wait 600 s.
        assert backoff_seconds(NO_JITTER, 1, retry_after=600, rng=random.Random(6)) >= 600

    def test_absurd_retry_after_is_bounded(self) -> None:
        policy = BackoffPolicy(max_retry_after_seconds=3600, jitter="none")
        assert backoff_seconds(policy, 1, retry_after=10**9, rng=random.Random(7)) <= 3600 * 1.1

    def test_negative_retry_after_is_treated_as_zero(self) -> None:
        assert backoff_seconds(NO_JITTER, 1, retry_after=-5, rng=random.Random(8)) == 0
