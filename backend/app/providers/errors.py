"""Provider error taxonomy (REQUIREMENTS_AUTONOMY §C, §E).

Adapters translate every vendor failure into one of these; the manager decides what each means:

=========================  ==========================================  =======================  ==================
error                      meaning                                     provider state           failover?
=========================  ==========================================  =======================  ==================
``TransientProviderError`` network / timeout / 5xx                     retry, then cooldown     yes
``RateLimitedError``       HTTP 429 (+ ``Retry-After``)                rate-limited until T     yes
``QuotaExhaustedError``    daily/monthly/credit limit reached          exhausted until reset    yes (not on send)
``AuthError``              HTTP 401 / bad key                          needs attention          yes
``PermissionDeniedError``  HTTP 403 — see ``reason``                   depends on reason        depends
``InvalidResponseError``   2xx but unusable / schema mismatch          unchanged                no
``PermanentProviderError`` request is wrong (400/404/422)              unchanged                no
=========================  ==========================================  =======================  ==================

Failing over around a *policy rejection* would be shopping for a more permissive provider, so it is never done.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum


class ProviderError(Exception):
    """Base class. ``provider_key`` is stamped by the manager."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        provider_key: str | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.provider_key = provider_key
        self.attempts = 1  # vendor calls made before giving up; set by the manager's retry loop


class TransientProviderError(ProviderError):
    """Temporary: connection error, timeout, 5xx. ``request_may_have_been_processed`` is True when the provider
    could have acted on the request already (timeout / reset *after* sending) — such a call must not be blindly
    repeated if the operation is not idempotent (e.g. sending an email)."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        request_may_have_been_processed: bool = False,
        provider_key: str | None = None,
    ) -> None:
        super().__init__(message, http_status=http_status, provider_key=provider_key)
        self.request_may_have_been_processed = request_may_have_been_processed


class RateLimitedError(ProviderError):
    def __init__(
        self,
        message: str = "rate limited",
        *,
        retry_after: float | None = None,
        http_status: int | None = 429,
        provider_key: str | None = None,
    ) -> None:
        super().__init__(message, http_status=http_status, provider_key=provider_key)
        self.retry_after = retry_after


class QuotaExhaustedError(ProviderError):
    """The plan's allowance is used up. ``reset_at`` is the *documented/reported* reset time when known."""

    def __init__(
        self,
        message: str = "quota exhausted",
        *,
        reset_at: datetime | None = None,
        http_status: int | None = None,
        provider_key: str | None = None,
    ) -> None:
        super().__init__(message, http_status=http_status, provider_key=provider_key)
        self.reset_at = reset_at


class AuthError(ProviderError):
    """HTTP 401 — the credential is missing, wrong or revoked. Stop using the provider until a human fixes it."""

    def __init__(
        self,
        message: str = "authentication failed",
        *,
        http_status: int | None = 401,
        provider_key: str | None = None,
    ) -> None:
        super().__init__(message, http_status=http_status, provider_key=provider_key)


class ForbiddenReason(StrEnum):
    QUOTA = "quota"  # 403 used by some vendors for an exhausted allowance
    PERMISSIONS = "permissions"  # missing scope / plan does not include this endpoint
    ACCOUNT_RESTRICTED = "account_restricted"  # suspended, billing issue, region block
    POLICY = "policy"  # this particular request was refused on content/policy grounds
    UNKNOWN = "unknown"


class PermissionDeniedError(ProviderError):
    def __init__(
        self,
        message: str = "forbidden",
        *,
        reason: ForbiddenReason = ForbiddenReason.UNKNOWN,
        reset_at: datetime | None = None,
        http_status: int | None = 403,
        provider_key: str | None = None,
    ) -> None:
        super().__init__(message, http_status=http_status, provider_key=provider_key)
        self.reason = reason
        self.reset_at = reset_at


class InvalidResponseError(ProviderError):
    """The provider answered, but not with something we can use. Logged, never hammered."""


class PermanentProviderError(ProviderError):
    """Retrying or switching provider cannot help: the request itself is wrong."""


# ───────────────────────── manager-level outcomes ─────────────────────────


class AllProvidersUnavailableError(Exception):
    """No eligible provider for this capability right now. The caller **parks** the task until ``next_available_at``
    (``None`` = needs a human, e.g. nothing is configured) — nothing is lost and nothing else pauses."""

    def __init__(
        self,
        capability: str,
        *,
        next_available_at: datetime | None,
        reasons: dict[str, str],
    ) -> None:
        detail = (
            "; ".join(f"{k}: {v}" for k, v in sorted(reasons.items())) or "no providers registered"
        )
        super().__init__(f"no provider available for {capability} ({detail})")
        self.capability = capability
        self.next_available_at = next_available_at
        self.reasons = reasons


class ProviderLimitReachedError(Exception):
    """A provider on a no-failover capability (sending) hit its limit. Never routed around (§I): wait for the reset."""

    def __init__(self, provider_key: str, *, resume_at: datetime | None, detail: str) -> None:
        super().__init__(f"{provider_key}: {detail}")
        self.provider_key = provider_key
        self.resume_at = resume_at


class UncertainOutcomeError(Exception):
    """A non-idempotent call may or may not have been performed (timeout after sending). The caller must
    reconcile — it is never retried or re-routed automatically."""

    def __init__(self, provider_key: str, cause: ProviderError) -> None:
        super().__init__(f"{provider_key}: outcome unknown after {type(cause).__name__}: {cause}")
        self.provider_key = provider_key
        self.cause = cause


# ───────────────────────── helpers for adapters ─────────────────────────


def parse_retry_after(value: str | None, *, now: datetime) -> float | None:
    """``Retry-After`` is either delta-seconds or an HTTP date. Returns seconds (>= 0) or ``None`` if unusable."""
    if not value:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - now).total_seconds())
    return max(0.0, seconds) if math.isfinite(seconds) else None


def error_from_status(
    status: int,
    message: str,
    *,
    retry_after: float | None = None,
    forbidden_reason: ForbiddenReason = ForbiddenReason.UNKNOWN,
) -> ProviderError:
    """Default HTTP-status → error mapping. Adapters call this and may refine (e.g. a 429 whose body says the
    *daily* quota is gone is a ``QuotaExhaustedError``, not a short rate limit)."""
    if status == 429:
        return RateLimitedError(message, retry_after=retry_after, http_status=status)
    if status == 401:
        return AuthError(message, http_status=status)
    if status == 403:
        return PermissionDeniedError(message, reason=forbidden_reason, http_status=status)
    if status in (408, 409, 425) or status >= 500:
        return TransientProviderError(message, http_status=status)
    return PermanentProviderError(message, http_status=status)
