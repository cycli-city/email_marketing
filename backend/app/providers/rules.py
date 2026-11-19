"""Pure provider rules: who is eligible, in what order, and how a provider's state changes after a call.

No I/O and no clock reads — callers pass ``now`` — so every rule here is exhaustively unit-tested with a fake clock.
The database layer (``store.py``) only persists what these functions decide.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.core.redact import scrub_error
from app.core.retry import BackoffPolicy, backoff_seconds
from app.domain.enums import Capability, CircuitState, CreditsAccuracy, ProviderStatus
from app.providers.errors import (
    AuthError,
    ForbiddenReason,
    PermissionDeniedError,
    ProviderError,
    QuotaExhaustedError,
    RateLimitedError,
    TransientProviderError,
)
from app.providers.spec import ProviderResult

# ───────────────────────────── policy ─────────────────────────────

_NO_JITTER = BackoffPolicy(jitter="none")
_IN_CALL_BACKOFF = BackoffPolicy(base_seconds=0.5, factor=2.0, max_seconds=8.0, jitter="equal")
_COOLDOWN_BACKOFF = BackoffPolicy(base_seconds=30.0, factor=2.0, max_seconds=1800.0, jitter="equal")


@dataclass(frozen=True, slots=True)
class ManagerPolicy:
    """Every tunable of the manager in one place (REQUIREMENTS_AUTONOMY §E: "configurable maximum attempts")."""

    # In-call tries of a transient failure on ONE provider, and the (short) delays between them.
    max_attempts_per_provider: int = 3
    in_call_backoff: BackoffPolicy = _IN_CALL_BACKOFF
    # After a provider fails every in-call try it sits out for a growing cooldown.
    cooldown_backoff: BackoffPolicy = _COOLDOWN_BACKOFF
    default_rate_limit_seconds: float = 60.0  # a 429 without Retry-After
    unknown_reset_recheck_seconds: float = 3600.0  # exhausted, reset time unknown → probe hourly
    circuit_threshold: int = 5  # consecutive failed call-sequences before the circuit shows OPEN
    probe_window_seconds: float = 60.0  # half-open: one caller probes, the rest skip, this long


DEFAULT_MANAGER_POLICY = ManagerPolicy()

# ───────────────────────────── snapshot & eligibility ─────────────────────────────


class Reason(StrEnum):
    UNCONFIGURED = "unconfigured"
    DISABLED = "disabled"
    NEEDS_ATTENTION = "needs_attention"
    PERSONAL_DATA_NOT_ALLOWED = "personal_data_not_allowed"
    PAID_NOT_ALLOWED = "paid_not_allowed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    EXHAUSTED = "exhausted"
    DAILY_LIMIT = "daily_limit"
    MONTHLY_LIMIT = "monthly_limit"
    CREDITS_EXHAUSTED = "credits_exhausted"
    RATE_LIMITED = "rate_limited"
    COOLDOWN = "cooldown"
    CIRCUIT_OPEN = "circuit_open"


@dataclass(frozen=True, slots=True)
class Unavailable:
    reason: Reason
    # When this provider could become eligible again; None = only a human can fix it (or never).
    until: datetime | None = None

    def describe(self, now: datetime) -> str:
        if self.until is None:
            return self.reason.value
        wait = max(0, int((self.until - now).total_seconds()))
        return f"{self.reason.value} (back in ~{wait}s)"


@dataclass(frozen=True, slots=True)
class ProviderSnapshot:
    """A provider as the selector sees it. ``daily_used`` / ``monthly_used`` are already rolled over to *today*."""

    key: str
    capability: Capability
    enabled: bool
    configured: bool
    status: ProviderStatus
    priority: int
    is_free: bool
    paid_fallback_allowed: bool
    personal_data_ok: bool
    fail_over_on_limits: bool
    daily_used: int = 0
    daily_limit: int | None = None
    monthly_used: int = 0
    monthly_limit: int | None = None
    remaining_credits: Decimal | None = None
    credits_accuracy: CreditsAccuracy = CreditsAccuracy.UNKNOWN
    reset_at: datetime | None = None
    rate_limited_until: datetime | None = None
    cooldown_until: datetime | None = None
    circuit_state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    estimated_cost_per_unit: Decimal = Decimal(0)
    cost_spent_month: Decimal = Decimal(0)
    monthly_budget: Decimal | None = None
    rate_limit_per_minute: int | None = (
        None  # for proactive pacing (units are the provider's own: requests/tokens)
    )

    @property
    def headroom(self) -> float:
        """Fraction of the tightest known limit still free (1.0 when no limit is known)."""
        fractions = [1.0]
        if self.daily_limit:
            fractions.append(max(0.0, 1.0 - self.daily_used / self.daily_limit))
        if self.monthly_limit:
            fractions.append(max(0.0, 1.0 - self.monthly_used / self.monthly_limit))
        return min(fractions)


def next_utc_midnight(now: datetime) -> datetime:
    day = now.astimezone(UTC).date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def next_month_start(now: datetime) -> datetime:
    utc = now.astimezone(UTC)
    year, month = (utc.year + 1, 1) if utc.month == 12 else (utc.year, utc.month + 1)
    return datetime(year, month, 1, tzinfo=UTC)


def month_start(day: date) -> date:
    return day.replace(day=1)


def check_eligibility(
    p: ProviderSnapshot, *, now: datetime, units: int = 1, personal_data: bool = False
) -> Unavailable | None:
    """``None`` ⇒ eligible. Otherwise the reason and — when knowable — when it could be eligible again.

    Checked in order of how permanent the blocker is: needs a human → not allowed for this request → temporary
    (cooldown / rate limit) → quota. The first blocker found is the one reported."""
    return (
        _human_blocker(p)
        or _request_blocker(p, now=now, units=units, personal_data=personal_data)
        or _temporary_blocker(p, now=now)
        or _quota_blocker(p, now=now, units=units)
    )


def _human_blocker(p: ProviderSnapshot) -> Unavailable | None:
    """Only a person can clear these: no key, switched off, or a credential the provider rejected (HTTP 401)."""
    if not p.configured:
        return Unavailable(Reason.UNCONFIGURED)
    if not p.enabled or p.status == ProviderStatus.DISABLED:
        return Unavailable(Reason.DISABLED)
    if p.status == ProviderStatus.NEEDS_ATTENTION:
        return Unavailable(Reason.NEEDS_ATTENTION)  # time does not repair a rejected credential
    return None


def _request_blocker(
    p: ProviderSnapshot, *, now: datetime, units: int, personal_data: bool
) -> Unavailable | None:
    """Provider is fine, but this *request* may not go to it: privacy clearance, or paid without opt-in/budget."""
    if personal_data and not p.personal_data_ok:
        return Unavailable(Reason.PERSONAL_DATA_NOT_ALLOWED)
    if p.is_free:
        return None
    if not p.paid_fallback_allowed:
        return Unavailable(Reason.PAID_NOT_ALLOWED)
    projected = p.cost_spent_month + p.estimated_cost_per_unit * units
    if p.monthly_budget is None or projected > p.monthly_budget:
        return Unavailable(
            Reason.BUDGET_EXHAUSTED, until=next_month_start(now) if p.monthly_budget else None
        )
    return None


def _temporary_blocker(p: ProviderSnapshot, *, now: datetime) -> Unavailable | None:
    """Clears by itself: a documented reset date, a Retry-After, or a cooldown after failures."""
    if p.status == ProviderStatus.EXHAUSTED and p.reset_at is not None and p.reset_at > now:
        return Unavailable(Reason.EXHAUSTED, until=p.reset_at)
    if p.rate_limited_until is not None and p.rate_limited_until > now:
        return Unavailable(Reason.RATE_LIMITED, until=p.rate_limited_until)
    if p.cooldown_until is not None and p.cooldown_until > now:
        reason = Reason.CIRCUIT_OPEN if p.circuit_state != CircuitState.CLOSED else Reason.COOLDOWN
        return Unavailable(reason, until=p.cooldown_until)
    return None


def _quota_blocker(p: ProviderSnapshot, *, now: datetime, units: int) -> Unavailable | None:
    """Out of allowance. Only a *provider-reported* credit balance may block, and only until its reset time: a
    report is a snapshot that nothing refreshes while the provider is blocked, so one with a past or unknown reset
    must let a probe through (a genuinely exhausted provider answers 429/quota, which re-blocks it properly).
    An internal estimate never blocks on its own."""
    if p.daily_limit is not None and p.daily_used + units > p.daily_limit:
        return Unavailable(Reason.DAILY_LIMIT, until=next_utc_midnight(now))
    if p.monthly_limit is not None and p.monthly_used + units > p.monthly_limit:
        return Unavailable(Reason.MONTHLY_LIMIT, until=next_month_start(now))
    reported = p.credits_accuracy == CreditsAccuracy.PROVIDER_REPORTED
    if (
        reported
        and p.remaining_credits is not None
        and p.remaining_credits < units
        and p.reset_at is not None
        and p.reset_at > now
    ):
        return Unavailable(Reason.CREDITS_EXHAUSTED, until=p.reset_at)
    return None


@dataclass(frozen=True, slots=True)
class Selection:
    ordered: tuple[ProviderSnapshot, ...]  # eligible providers, best first
    unavailable: dict[str, Unavailable] = field(default_factory=dict)

    @property
    def next_available_at(self) -> datetime | None:
        """Earliest time any currently-unavailable provider could return (None if none will without a human)."""
        times = [u.until for u in self.unavailable.values() if u.until is not None]
        return min(times) if times else None


def select_providers(
    snapshots: list[ProviderSnapshot],
    *,
    now: datetime,
    units: int = 1,
    personal_data: bool = False,
    exclude: frozenset[str] = frozenset(),
) -> Selection:
    """Eligible providers ordered: free before paid, then lowest ``priority`` number, then most headroom left
    (spreads load across free tiers instead of draining one while another sits idle)."""
    eligible: list[ProviderSnapshot] = []
    unavailable: dict[str, Unavailable] = {}
    for snap in snapshots:
        if snap.key in exclude:
            continue
        verdict = check_eligibility(snap, now=now, units=units, personal_data=personal_data)
        if verdict is None:
            eligible.append(snap)
        else:
            unavailable[snap.key] = verdict
    eligible.sort(key=lambda s: (not s.is_free, s.priority, -s.headroom, s.key))
    return Selection(tuple(eligible), unavailable)


# ───────────────────────────── state transitions ─────────────────────────────


@dataclass(frozen=True, slots=True)
class StatePatch:
    """Column updates for ``provider_states`` plus an optional human-readable activity-feed entry."""

    fields: dict[str, Any]
    # (level, event_type, message) — only for *state changes* worth the owner's attention; never per call.
    note: tuple[str, str, str] | None = None
    # When the provider is expected to be usable again (None = needs a human); mirrors the fields for callers.
    available_at: datetime | None = None
    # Whether this failure increments the lifetime ``failure_count`` (a refused *request* says nothing about health).
    count_failure: bool = True


def _jitter(rng: random.Random | None) -> float:
    return (rng or random).random()


def on_success(
    result: ProviderResult[Any], *, previous: ProviderSnapshot, now: datetime
) -> StatePatch:
    fields: dict[str, Any] = {
        "status": ProviderStatus.READY,
        "consecutive_failures": 0,
        "circuit_state": CircuitState.CLOSED,
        "cooldown_until": None,
        "rate_limited_until": None,
        "last_success_at": now,
    }
    if previous.status in (
        ProviderStatus.EXHAUSTED,
        ProviderStatus.RATE_LIMITED,
        ProviderStatus.COOLDOWN,
    ):
        fields["reset_at"] = None
    if result.remaining is not None:
        fields["remaining_credits"] = result.remaining
        fields["credits_accuracy"] = CreditsAccuracy.PROVIDER_REPORTED
        if result.limit is not None:
            fields["credits_total"] = result.limit
        if result.reset_at is not None:
            fields["reset_at"] = result.reset_at
    if result.cost:
        fields["cost_spent_month"] = previous.cost_spent_month + result.cost
    note = None
    if previous.status != ProviderStatus.READY or previous.circuit_state != CircuitState.CLOSED:
        note = ("info", "provider_recovered", f"{previous.key} is working again")
    return StatePatch(fields, note, available_at=now)


def on_failure(
    error: ProviderError,
    *,
    previous: ProviderSnapshot,
    now: datetime,
    policy: ManagerPolicy = DEFAULT_MANAGER_POLICY,
    rng: random.Random | None = None,
) -> StatePatch:
    """How one failed call *sequence* (after in-call retries) changes the provider."""
    base: dict[str, Any] = {
        "last_failure_at": now,
        "last_error": scrub_error(f"{type(error).__name__}: {error}")[:500],
    }
    ctx = _FailureContext(previous=previous, now=now, policy=policy, rng=rng, base=base)
    for kind, handler in _FAILURE_HANDLERS:
        if isinstance(error, kind):
            return handler(error, ctx)
    # InvalidResponseError / PermanentProviderError / anything unclassified: the provider is reachable but this
    # request or response was unusable. Record it for the health view; don't penalise availability.
    return StatePatch(base, None, now)


@dataclass(frozen=True, slots=True)
class _FailureContext:
    previous: ProviderSnapshot
    now: datetime
    policy: ManagerPolicy
    rng: random.Random | None
    base: dict[str, Any]  # last_failure_at / last_error, applied to every failure

    @property
    def key(self) -> str:
        return self.previous.key


def _on_rate_limited(error: RateLimitedError, ctx: _FailureContext) -> StatePatch:
    """HTTP 429: wait at least what the provider asked (plus jitter). Not a fault: no failure streak, no circuit."""
    asked = (
        error.retry_after
        if error.retry_after is not None
        else ctx.policy.default_rate_limit_seconds
    )
    wait = backoff_seconds(_NO_JITTER, 1, retry_after=asked, rng=ctx.rng)
    until = ctx.now + timedelta(seconds=wait)
    fields = ctx.base | {"status": ProviderStatus.RATE_LIMITED, "rate_limited_until": until}
    note = (
        "warning",
        "provider_rate_limited",
        f"{ctx.key} is rate limited — paused for ~{int(wait)}s",
    )
    return StatePatch(fields, note, until)


def _on_quota_exhausted(error: ProviderError, ctx: _FailureContext) -> StatePatch:
    """Allowance used up. Back at the documented reset if we know it; otherwise re-probe about hourly."""
    reset_at: datetime | None = getattr(error, "reset_at", None)
    known = reset_at is not None and reset_at > ctx.now
    if not known:
        recheck = ctx.policy.unknown_reset_recheck_seconds * (0.5 + _jitter(ctx.rng))
        reset_at = ctx.now + timedelta(seconds=recheck)
    fields = ctx.base | {"status": ProviderStatus.EXHAUSTED, "reset_at": reset_at}
    when = "until its quota resets" if known else "— reset time unknown, re-checking hourly"
    note = ("warning", "provider_exhausted", f"{ctx.key} quota exhausted {when}")
    return StatePatch(fields, note, reset_at)


def _on_auth_error(_error: AuthError, ctx: _FailureContext) -> StatePatch:
    """HTTP 401: stop using the provider until a human restores the credential."""
    fields = ctx.base | {"status": ProviderStatus.NEEDS_ATTENTION}
    note = (
        "error",
        "provider_auth_failed",
        f"{ctx.key} rejected its credentials — needs attention (not used until fixed)",
    )
    return StatePatch(fields, note, None)


def _on_forbidden(error: PermissionDeniedError, ctx: _FailureContext) -> StatePatch:
    """HTTP 403: classify *why* (§E). Quota → exhausted. Policy refusal of one request → provider untouched.
    Permissions / account restriction / unknown → needs attention."""
    if error.reason == ForbiddenReason.QUOTA:
        return _on_quota_exhausted(error, ctx)
    if error.reason == ForbiddenReason.POLICY:
        return StatePatch({}, None, ctx.now, count_failure=False)
    fields = ctx.base | {"status": ProviderStatus.NEEDS_ATTENTION}
    note = (
        "error",
        "provider_forbidden",
        f"{ctx.key} refused access ({error.reason.value}) — needs attention (not used until fixed)",
    )
    return StatePatch(fields, note, None)


def _on_transient(error: TransientProviderError, ctx: _FailureContext) -> StatePatch:
    """Network / 5xx that survived the in-call retries: cool the provider down; open the circuit on a long streak."""
    previous, policy = ctx.previous, ctx.policy
    streak = previous.consecutive_failures + 1
    wait = backoff_seconds(policy.cooldown_backoff, streak, rng=ctx.rng)
    until = ctx.now + timedelta(seconds=wait)
    opened = streak >= policy.circuit_threshold
    fields = ctx.base | {
        "consecutive_failures": streak,
        "cooldown_until": until,
        "circuit_state": CircuitState.OPEN if opened else CircuitState.CLOSED,
        "status": ProviderStatus.COOLDOWN,
    }
    note = None
    if streak == 1 or (opened and previous.circuit_state == CircuitState.CLOSED):
        what = "circuit opened" if opened else "cooling down"
        note = (
            "warning",
            "provider_unavailable",
            scrub_error(f"{ctx.key} unavailable ({what}, ~{int(wait)}s): {error}")[:300],
        )
    return StatePatch(fields, note, until)


# Order matters only where classes overlap; PermissionDeniedError(QUOTA) is routed to the quota handler itself.
_FAILURE_HANDLERS: tuple[
    tuple[type[ProviderError], Callable[[Any, _FailureContext], StatePatch]], ...
] = (
    (RateLimitedError, _on_rate_limited),
    (QuotaExhaustedError, _on_quota_exhausted),
    (AuthError, _on_auth_error),
    (PermissionDeniedError, _on_forbidden),
    (TransientProviderError, _on_transient),
)


def effective_usage(
    used: int, stored_period: date | None, current_period: date
) -> int:  # rollover helper shared with the store
    return used if stored_period == current_period else 0
