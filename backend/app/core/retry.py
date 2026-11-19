"""Backoff math shared by the job queue and the provider retry engine (pure: no I/O, injectable randomness).

REQUIREMENTS_AUTONOMY §E: exponential backoff + jitter, ``Retry-After`` is always respected, and a rate-limited
caller must not hammer the provider while it is limited.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

Jitter = Literal["none", "equal", "full"]


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    base_seconds: float = 5.0
    factor: float = 2.0
    max_seconds: float = 3600.0
    jitter: Jitter = "equal"
    # A provider's explicit Retry-After may legitimately exceed ``max_seconds``; still bound it so one bad
    # header can't park work for days.
    max_retry_after_seconds: float = 24 * 3600.0


DEFAULT_POLICY = BackoffPolicy()


def backoff_seconds(
    policy: BackoffPolicy,
    attempt: int,
    *,
    retry_after: float | None = None,
    rng: random.Random | None = None,
) -> float:
    """Seconds to wait before retry number ``attempt`` (1 = the first retry).

    * With ``retry_after`` (HTTP 429/503): wait **at least** that long, plus up to 10 % jitter so many callers
      released by the same header don't all return in the same instant.
    * Otherwise: ``base * factor**(attempt-1)`` capped at ``max_seconds``, then jittered:
      ``equal`` = half fixed + half random (never zero, never above the cap), ``full`` = uniform(0, cap), ``none``.
    """
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    rand = rng or random
    if retry_after is not None:
        wait = min(max(retry_after, 0.0), policy.max_retry_after_seconds)
        return wait * (1.0 + 0.1 * rand.random())
    ceiling = min(policy.base_seconds * policy.factor ** (attempt - 1), policy.max_seconds)
    if policy.jitter == "none":
        return ceiling
    if policy.jitter == "full":
        return rand.uniform(0.0, ceiling)
    return ceiling / 2.0 + rand.uniform(0.0, ceiling / 2.0)
