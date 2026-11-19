"""The Provider Manager (REQUIREMENTS_AUTONOMY §B to §E).

Business logic asks for a *capability* and passes an operation; the manager picks the best eligible provider
(free first, by priority, spreading load), reserves quota atomically, runs the operation with bounded retries, and —
when a provider cannot serve the call — fails over to the next one. If nothing can serve it, it raises
:class:`AllProvidersUnavailableError` carrying *when* something could, so the caller **parks** that one task and the
queue resumes it automatically. Nothing is lost and nothing else stops.

Never done, by design (REQUIREMENTS_AUTONOMY §D/§I):

* creating accounts or rotating keys/identities to dodge a limit — every provider here has exactly one credential;
* failing over around a *sending* limit (``fail_over_on_limits=False``) — we wait for the reset instead;
* failing over around a *policy refusal* of a request — that would be shopping for a more permissive provider;
* blindly retrying a non-idempotent call whose outcome is unknown (a send that timed out) — the caller reconciles.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar

import structlog

from app.core.clock import Clock
from app.core.retry import backoff_seconds
from app.db.models import ProviderState
from app.domain.enums import ActivityLevel, Capability, ProviderStatus
from app.providers.errors import (
    AllProvidersUnavailableError,
    AuthError,
    ForbiddenReason,
    PermissionDeniedError,
    ProviderError,
    ProviderLimitReachedError,
    QuotaExhaustedError,
    RateLimitedError,
    TransientProviderError,
    UncertainOutcomeError,
)
from app.providers.pacing import Pacer
from app.providers.registry import Provider, ProviderRegistry
from app.providers.rules import (
    DEFAULT_MANAGER_POLICY,
    ManagerPolicy,
    StatePatch,
    check_eligibility,
    select_providers,
)
from app.providers.spec import CallOutcome, ProviderResult
from app.providers.store import ProviderStore, Reservation, snapshot_from_state

log = structlog.get_logger("providers.manager")

T = TypeVar("T")
P = TypeVar("P", bound=Provider)

# A call may legitimately wait this long for a per-minute window to open before trying another provider.
MAX_PACING_WAIT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ProviderView:
    """One row of the settings / System Operations page. Every figure is real, or explicitly marked unknown."""

    key: str
    capability: Capability
    display_name: str
    status: ProviderStatus  # the stored status
    effective_state: (
        str  # what the selector would say right now: "available" or the blocking reason
    )
    available_again_at: datetime | None
    enabled: bool
    configured: bool
    priority: int
    is_free: bool
    plan: str | None
    daily_used: int
    daily_limit: int | None
    monthly_used: int
    monthly_limit: int | None
    remaining_credits: Decimal | None
    credits_accuracy: str  # provider_reported | internal_counter | unknown
    reset_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error: str | None
    failure_count: int
    consecutive_failures: int


class ProviderManager:
    def __init__(
        self,
        store: ProviderStore,
        registry: ProviderRegistry,
        clock: Clock,
        *,
        policy: ManagerPolicy = DEFAULT_MANAGER_POLICY,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        pacer: Pacer | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._clock = clock
        self._policy = policy
        self._rng = rng
        self._sleep = sleep
        self._pacer = pacer or Pacer()

    async def start(self) -> None:
        """Create/refresh state rows for every registered provider (call once at startup)."""
        await self._store.sync(self._registry.all())

    # ───────────────────────────── the one entry point ─────────────────────────────

    async def call(
        self,
        capability: Capability,
        operation: Callable[[Any], Awaitable[ProviderResult[T]]],
        *,
        units: int = 1,
        weight: float = 1.0,
        personal_data: bool = False,
        idempotent: bool = True,
        task_ref: str | None = None,
        only: Collection[str] | None = None,
        tier: str | None = None,
    ) -> CallOutcome[T]:
        """Run ``operation(provider)`` on the best available provider for ``capability``.

        ``units``        quota units this call consumes (requests, credits…).
        ``weight``       amount counted against the per-minute pacing window (e.g. estimated tokens).
        ``personal_data`` True if the payload contains prospect personal data → only providers cleared for it.
        ``idempotent``   False for calls that must not be repeated blindly (sending) — see module docstring.
        ``only``         restrict to these provider keys (e.g. a specific mailbox).
        ``tier``         restrict to providers of this quality tier (e.g. LLM "smart" vs "fast").
        """
        providers = self._registry.for_capability(capability)
        if only is not None:
            providers = {k: p for k, p in providers.items() if k in only}
        if tier is not None:
            providers = {k: p for k, p in providers.items() if p.spec.tier == tier}
        now = self._clock.now()
        snapshots = await self._store.snapshots(providers)
        selection = select_providers(snapshots, now=now, units=units, personal_data=personal_data)
        reasons = {key: verdict.describe(now) for key, verdict in selection.unavailable.items()}
        next_at = selection.next_available_at
        failed_over: list[str] = []
        attempts_total = 0
        started = time.monotonic()

        for snap in selection.ordered:
            provider = providers[snap.key]
            wait = self._pacer.wait_seconds(snap.key, snap.rate_limit_per_minute, weight, now)
            if wait > MAX_PACING_WAIT_SECONDS:
                reasons[snap.key] = f"per-minute window full (~{int(wait)}s)"
                paced_until = now + timedelta(seconds=wait)
                next_at = paced_until if next_at is None else min(next_at, paced_until)
                continue
            if wait > 0:
                await self._sleep(wait)

            reservation = await self._store.reserve(provider, units)
            if reservation is None:
                reasons[snap.key] = "quota or availability just taken by another task"
                continue
            if reservation.probe:
                log.info("providers.probing_after_outage", provider=snap.key)
            self._pacer.commit(snap.key, snap.rate_limit_per_minute, weight, self._clock.now())

            call_started = time.monotonic()
            try:
                result, attempts = await self._run(provider, operation, idempotent=idempotent)
            except ProviderError as exc:
                attempts_total += exc.attempts
                exc.provider_key = snap.key
                latency = int((time.monotonic() - call_started) * 1000)
                # Raises when the call must NOT be routed elsewhere; otherwise we fail over to the next provider.
                next_at = await self._handle_failure(
                    provider,
                    exc,
                    reservation,
                    units=units,
                    latency=latency,
                    task_ref=task_ref,
                    next_at=next_at,
                    reasons=reasons,
                    idempotent=idempotent,
                )
                failed_over.append(snap.key)
                continue
            attempts_total += attempts
            latency = int((time.monotonic() - call_started) * 1000)
            await self._bookkeep_success(
                provider, result, units=units, latency=latency, task_ref=task_ref
            )
            if failed_over:
                await self._announce_failover(capability, failed_over, snap.key)
            return CallOutcome(
                value=result.value,
                provider_key=snap.key,
                failovers=tuple(failed_over),
                attempts=attempts_total,
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        log.warning(
            "providers.none_available",
            capability=capability.value,
            next_available_at=str(next_at),
            reasons=reasons,
        )
        raise AllProvidersUnavailableError(
            capability.value, next_available_at=next_at, reasons=reasons
        )

    # ───────────────────────────── internals ─────────────────────────────

    async def _run(
        self,
        provider: Provider,
        operation: Callable[[Any], Awaitable[ProviderResult[T]]],
        *,
        idempotent: bool,
    ) -> tuple[ProviderResult[T], int]:
        """One provider, with bounded in-call retries of transient failures (jittered exponential backoff)."""
        max_attempts = self._policy.max_attempts_per_provider
        for attempt in range(1, max_attempts + 1):
            try:
                return await operation(provider), attempt
            except TransientProviderError as exc:
                exc.attempts = attempt
                if exc.request_may_have_been_processed and not idempotent:
                    raise  # must not be repeated: handled as an uncertain outcome
                if attempt == max_attempts:
                    raise
                delay = backoff_seconds(self._policy.in_call_backoff, attempt, rng=self._rng)
                log.info(
                    "providers.retrying",
                    provider=provider.spec.key,
                    attempt=attempt,
                    in_s=round(delay, 2),
                    error=type(exc).__name__,
                )
                await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _handle_failure(
        self,
        provider: Provider,
        exc: ProviderError,
        reservation: Reservation,
        *,
        units: int,
        latency: int,
        task_ref: str | None,
        idempotent: bool,
        next_at: datetime | None,
        reasons: dict[str, str],
    ) -> datetime | None:
        """Record the failure, then either raise (the call must not be routed elsewhere) or return the updated
        "earliest time anything could be available" so the caller can fail over to the next provider."""
        key = provider.spec.key
        patch = await self._bookkeep_failure(
            provider, exc, reservation, units=units, latency=latency, task_ref=task_ref
        )
        reasons[key] = f"{type(exc).__name__}: {exc}"[:200]

        # 1. A timeout after a non-idempotent request was sent: the outcome is unknown. Never retry or re-route.
        if (
            isinstance(exc, TransientProviderError)
            and exc.request_may_have_been_processed
            and not idempotent
        ):
            raise UncertainOutcomeError(key, exc) from exc

        # 2. Requests that are wrong or refused on policy grounds are not a reason to try someone else.
        policy_refusal = (
            isinstance(exc, PermissionDeniedError) and exc.reason == ForbiddenReason.POLICY
        )
        if policy_refusal or not _failover_worthy(exc):
            raise exc

        # 3. Limits on a no-failover capability (sending): wait for the reset, never route around it.
        hit_limit = isinstance(exc, RateLimitedError | QuotaExhaustedError) or (
            isinstance(exc, PermissionDeniedError) and exc.reason == ForbiddenReason.QUOTA
        )
        if hit_limit and not provider.spec.fail_over_on_limits:
            raise ProviderLimitReachedError(
                key, resume_at=patch.available_at, detail=str(exc)
            ) from exc

        if patch.available_at is not None:
            next_at = patch.available_at if next_at is None else min(next_at, patch.available_at)
        log.warning(
            "providers.failing_over",
            provider=key,
            error=type(exc).__name__,
            resume=str(patch.available_at),
        )
        return next_at

    async def _bookkeep_success(
        self,
        provider: Provider,
        result: ProviderResult[Any],
        *,
        units: int,
        latency: int,
        task_ref: str | None,
    ) -> None:
        """The call succeeded; a bookkeeping hiccup must never throw the result away (and cause a repeat call)."""
        try:
            await self._store.record_success(
                provider, result, units=units, latency_ms=latency, task_ref=task_ref
            )
        except Exception:
            log.exception(
                "providers.bookkeeping_failed", provider=provider.spec.key, stage="success"
            )

    async def _bookkeep_failure(
        self,
        provider: Provider,
        exc: ProviderError,
        reservation: Reservation,
        *,
        units: int,
        latency: int,
        task_ref: str | None,
    ) -> StatePatch:
        try:
            patch = await self._store.record_failure(
                provider, exc, units=units, latency_ms=latency, task_ref=task_ref
            )
        except Exception:
            log.exception(
                "providers.bookkeeping_failed", provider=provider.spec.key, stage="failure"
            )
            return StatePatch({}, None, None)
        if _provider_did_not_count(exc):
            try:
                await self._store.refund(reservation)
            except Exception:
                log.exception("providers.refund_failed", provider=provider.spec.key)
        return patch

    async def _announce_failover(
        self, capability: Capability, failed: list[str], used: str
    ) -> None:
        names = ", ".join(failed)
        try:
            await self._store.add_activity(
                ActivityLevel.INFO,
                "provider_failover",
                f"{capability.value}: {names} unavailable — continued with {used}",
                {"capability": capability.value, "failed": failed, "used": used},
            )
        except Exception:
            log.exception("providers.activity_failed")

    # ───────────────────────────── views & owner controls ─────────────────────────────

    async def overview(self, capability: Capability | None = None) -> list[ProviderView]:
        """Everything the settings page shows: real figures, with accuracy stated, never invented."""
        now = self._clock.now()
        views: list[ProviderView] = []
        for row in await self._store.list_states(capability):
            provider = self._registry.get(row.capability, row.provider_key)
            if provider is None:  # state row for a provider no longer registered
                continue
            views.append(self._view(row, provider, now))
        return views

    def _view(self, row: ProviderState, provider: Provider, now: datetime) -> ProviderView:
        snap = snapshot_from_state(row, provider.spec, configured=provider.configured, now=now)
        verdict = check_eligibility(snap, now=now)
        return ProviderView(
            key=row.provider_key,
            capability=row.capability,
            display_name=provider.spec.display_name,
            status=row.status,
            effective_state="available" if verdict is None else verdict.describe(now),
            available_again_at=None if verdict is None else verdict.until,
            enabled=row.enabled,
            configured=provider.configured,
            priority=row.priority,
            is_free=row.is_free_tier,
            plan=row.plan,
            daily_used=snap.daily_used,
            daily_limit=row.daily_limit,
            monthly_used=snap.monthly_used,
            monthly_limit=row.monthly_limit,
            remaining_credits=row.remaining_credits,
            credits_accuracy=row.credits_accuracy.value,
            reset_at=row.reset_at,
            last_success_at=row.last_success_at,
            last_failure_at=row.last_failure_at,
            last_error=row.last_error,
            failure_count=row.failure_count,
            consecutive_failures=row.consecutive_failures,
        )

    async def update_settings(
        self, provider_key: str, capability: Capability, **changes: Any
    ) -> None:
        await self._store.update_settings(provider_key, capability, **changes)

    async def reset_attention(self, provider_key: str, capability: Capability) -> None:
        await self._store.reset_attention(provider_key, capability)


def _failover_worthy(exc: ProviderError) -> bool:
    """Errors that mean "this provider cannot serve this call right now" (vs. "the request is wrong")."""
    return isinstance(
        exc,
        TransientProviderError
        | RateLimitedError
        | QuotaExhaustedError
        | AuthError
        | PermissionDeniedError,
    )


def _provider_did_not_count(exc: ProviderError) -> bool:
    """Failures where the provider almost certainly did not charge the call against its quota → refund our counter."""
    return isinstance(
        exc, TransientProviderError | RateLimitedError | AuthError | PermissionDeniedError
    )
