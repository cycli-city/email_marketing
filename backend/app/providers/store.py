"""Persistence for the ProviderManager (tables ``provider_states`` and ``provider_usage_events``).

Two rules keep the numbers honest under concurrency:

* **Reserve atomically.** A task claims quota with a single conditional ``UPDATE … RETURNING``; if two workers race
  for the last request, exactly one wins and the other moves to the next provider. The in-database counter can never
  go past the limit, and the whole check (enabled, cooldown, rate-limit, quota, half-open probe) happens in that one
  statement, so it cannot be stale.
* **Transition under a row lock.** After a call, the new state is computed from the *fresh* locked row (not from the
  snapshot taken before the call), so concurrent failures build the streak correctly.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import case, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.db.models import ActivityEvent, ProviderState, ProviderUsageEvent
from app.db.session import session_scope
from app.domain.enums import (
    ActivityLevel,
    Capability,
    CircuitState,
    ProviderStatus,
    UsageOutcome,
)
from app.providers.errors import (
    AuthError,
    ForbiddenReason,
    PermissionDeniedError,
    ProviderError,
    QuotaExhaustedError,
    RateLimitedError,
    TransientProviderError,
)
from app.providers.registry import Provider
from app.providers.rules import (
    DEFAULT_MANAGER_POLICY,
    ManagerPolicy,
    ProviderSnapshot,
    StatePatch,
    effective_usage,
    month_start,
    on_failure,
    on_success,
)
from app.providers.spec import ProviderResult, ProviderSpec

log = structlog.get_logger("providers.store")

_BLOCKED_STATUSES = (
    ProviderStatus.NEEDS_ATTENTION,
    ProviderStatus.DISABLED,
    ProviderStatus.UNCONFIGURED,
)
# Columns the owner may edit from the settings page (everything else is managed by the system).
EDITABLE_FIELDS = frozenset(
    {
        "enabled",
        "priority",
        "plan",
        "daily_limit",
        "monthly_limit",
        "rate_limit_per_minute",
        "paid_fallback_allowed",
        "monthly_budget",
    }
)
_STATE_COLUMNS = frozenset(ProviderState.__table__.columns.keys())


@dataclass(frozen=True, slots=True)
class Reservation:
    state_id: uuid.UUID
    units: int
    day: date
    month: date
    probe: bool  # this call is the half-open probe of a provider whose circuit was open


def usage_outcome_for(error: ProviderError) -> UsageOutcome:
    if isinstance(error, RateLimitedError):
        return UsageOutcome.RATE_LIMITED
    if isinstance(error, QuotaExhaustedError):
        return UsageOutcome.QUOTA_EXHAUSTED
    if isinstance(error, AuthError):
        return UsageOutcome.AUTH_ERROR
    if isinstance(error, PermissionDeniedError):
        return (
            UsageOutcome.QUOTA_EXHAUSTED
            if error.reason == ForbiddenReason.QUOTA
            else UsageOutcome.PERMISSION_ERROR
        )
    if isinstance(error, TransientProviderError):
        return UsageOutcome.RETRYABLE_ERROR
    return UsageOutcome.PERMANENT_ERROR


def snapshot_from_state(
    row: ProviderState, spec: ProviderSpec, *, configured: bool, now: datetime
) -> ProviderSnapshot:
    today = now.astimezone(UTC).date()
    month = month_start(today)
    same_month = row.usage_month == month
    return ProviderSnapshot(
        key=row.provider_key,
        capability=row.capability,
        enabled=row.enabled,
        configured=configured,
        status=row.status,
        priority=row.priority,
        is_free=row.is_free_tier,
        paid_fallback_allowed=row.paid_fallback_allowed,
        personal_data_ok=spec.may_receive_personal_data,
        fail_over_on_limits=spec.fail_over_on_limits,
        daily_used=effective_usage(row.daily_used, row.usage_day, today),
        daily_limit=row.daily_limit,
        monthly_used=effective_usage(row.monthly_used, row.usage_month, month),
        monthly_limit=row.monthly_limit,
        remaining_credits=row.remaining_credits,
        credits_accuracy=row.credits_accuracy,
        reset_at=row.reset_at,
        rate_limited_until=row.rate_limited_until,
        cooldown_until=row.cooldown_until,
        circuit_state=row.circuit_state,
        consecutive_failures=row.consecutive_failures,
        estimated_cost_per_unit=row.estimated_cost_per_unit,
        cost_spent_month=row.cost_spent_month if same_month else Decimal(0),
        monthly_budget=row.monthly_budget,
        rate_limit_per_minute=row.rate_limit_per_minute,
    )


class ProviderStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        workspace_id: uuid.UUID,
        *,
        policy: ManagerPolicy = DEFAULT_MANAGER_POLICY,
        rng: random.Random | None = None,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock
        self._workspace_id = workspace_id
        self._policy = policy
        self._rng = rng

    # ───────────────────────────── registration & reads ─────────────────────────────

    async def sync(self, providers: Iterable[Provider]) -> None:
        """Make sure every registered provider has a state row and that ``credentials_present`` /
        ``unconfigured↔ready`` reflect reality. Never touches an owner's choices (enabled, priority, limits…)."""
        async with session_scope(self._sessions) as session:
            for provider in providers:
                spec, configured = provider.spec, provider.configured
                await session.execute(
                    pg_insert(ProviderState)
                    .values(
                        workspace_id=self._workspace_id,
                        provider_key=spec.key,
                        capability=spec.capability,
                        enabled=True,
                        priority=spec.default_priority,
                        status=ProviderStatus.READY if configured else ProviderStatus.UNCONFIGURED,
                        credentials_present=configured,
                        is_free_tier=spec.free_tier,
                        paid_fallback_allowed=False,
                        daily_limit=spec.daily_limit,
                        monthly_limit=spec.monthly_limit,
                        rate_limit_per_minute=spec.rate_limit_per_minute,
                        estimated_cost_per_unit=spec.estimated_cost_per_unit,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["workspace_id", "provider_key", "capability"]
                    )
                )
                row = await self._lock(session, spec.key, spec.capability)
                row.credentials_present = configured
                if not configured and row.status != ProviderStatus.DISABLED:
                    row.status = ProviderStatus.UNCONFIGURED
                elif configured and row.status == ProviderStatus.UNCONFIGURED:
                    row.status = ProviderStatus.READY

    async def list_states(self, capability: Capability | None = None) -> list[ProviderState]:
        stmt = select(ProviderState).where(ProviderState.workspace_id == self._workspace_id)
        if capability is not None:
            stmt = stmt.where(ProviderState.capability == capability)
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    stmt.order_by(ProviderState.capability, ProviderState.priority)
                )
            ).scalars()
            return list(rows)

    async def snapshots(self, providers: dict[str, Provider]) -> list[ProviderSnapshot]:
        if not providers:
            return []
        capability = next(iter(providers.values())).spec.capability
        rows = {r.provider_key: r for r in await self.list_states(capability)}
        missing = [p for key, p in providers.items() if key not in rows]
        if missing:
            await self.sync(missing)
            rows = {r.provider_key: r for r in await self.list_states(capability)}
        now = self._clock.now()
        return [
            snapshot_from_state(rows[key], p.spec, configured=p.configured, now=now)
            for key, p in providers.items()
        ]

    # ───────────────────────────── reserving quota ─────────────────────────────

    async def reserve(self, provider: Provider, units: int) -> Reservation | None:
        """Atomically claim ``units`` of quota. ``None`` ⇒ the provider cannot take the call right now (somebody else
        took the last of the quota, or it was just put in cooldown / rate-limited / exhausted)."""
        spec = provider.spec
        now = self._clock.now()
        today = now.astimezone(UTC).date()
        month = month_start(today)
        s = ProviderState
        daily_now = case((s.usage_day == today, s.daily_used), else_=0)
        monthly_now = case((s.usage_month == month, s.monthly_used), else_=0)
        probing = s.circuit_state != CircuitState.CLOSED
        stmt = (
            update(s)
            .where(
                s.workspace_id == self._workspace_id,
                s.provider_key == spec.key,
                s.capability == spec.capability,
                s.enabled.is_(True),
                s.status.not_in(_BLOCKED_STATUSES),
                or_(s.status != ProviderStatus.EXHAUSTED, s.reset_at.is_(None), s.reset_at <= now),
                or_(s.cooldown_until.is_(None), s.cooldown_until <= now),
                or_(s.rate_limited_until.is_(None), s.rate_limited_until <= now),
                or_(s.daily_limit.is_(None), daily_now + units <= s.daily_limit),
                or_(s.monthly_limit.is_(None), monthly_now + units <= s.monthly_limit),
            )
            .values(
                daily_used=daily_now + units,
                usage_day=today,
                monthly_used=monthly_now + units,
                usage_month=month,
                cost_spent_month=case((s.usage_month == month, s.cost_spent_month), else_=0),
                # Half-open: the winner of this UPDATE is the only probe for `probe_window_seconds`.
                cooldown_until=case(
                    (probing, now + timedelta(seconds=self._policy.probe_window_seconds)),
                    else_=s.cooldown_until,
                ),
                circuit_state=case((probing, CircuitState.HALF_OPEN), else_=s.circuit_state),
                updated_at=now,
            )
            .returning(s.id, s.circuit_state)
            .execution_options(synchronize_session=False)
        )
        async with session_scope(self._sessions) as session:
            row = (await session.execute(stmt)).first()
        if row is None:
            return None
        return Reservation(
            state_id=row[0],
            units=units,
            day=today,
            month=month,
            probe=row[1] == CircuitState.HALF_OPEN,
        )

    async def refund(self, reservation: Reservation) -> None:
        """Give reserved units back when the provider clearly did not count the call (network failure, 429, 401…)."""
        s = ProviderState
        stmt = (
            update(s)
            .where(s.id == reservation.state_id)
            .values(
                daily_used=case(
                    (
                        s.usage_day == reservation.day,
                        case(
                            (s.daily_used >= reservation.units, s.daily_used - reservation.units),
                            else_=0,
                        ),
                    ),
                    else_=s.daily_used,
                ),
                monthly_used=case(
                    (
                        s.usage_month == reservation.month,
                        case(
                            (
                                s.monthly_used >= reservation.units,
                                s.monthly_used - reservation.units,
                            ),
                            else_=0,
                        ),
                    ),
                    else_=s.monthly_used,
                ),
            )
            .execution_options(synchronize_session=False)
        )
        async with session_scope(self._sessions) as session:
            await session.execute(stmt)

    # ───────────────────────────── recording outcomes ─────────────────────────────

    async def record_success(
        self,
        provider: Provider,
        result: ProviderResult[Any],
        *,
        units: int,
        latency_ms: int,
        task_ref: str | None,
    ) -> None:
        spec, now = provider.spec, self._clock.now()
        async with session_scope(self._sessions) as session:
            row = await self._lock(session, spec.key, spec.capability)
            previous = snapshot_from_state(row, spec, configured=provider.configured, now=now)
            patch = on_success(result, previous=previous, now=now)
            self._apply(session, row, patch, failed=False)
            session.add(
                ProviderUsageEvent(
                    workspace_id=self._workspace_id,
                    provider_key=spec.key,
                    capability=spec.capability,
                    created_at=now,
                    outcome=UsageOutcome.SUCCESS,
                    units=Decimal(units),
                    cost=result.cost or Decimal(0),
                    latency_ms=latency_ms,
                    task_ref=task_ref,
                )
            )

    async def record_failure(
        self,
        provider: Provider,
        error: ProviderError,
        *,
        units: int,
        latency_ms: int,
        task_ref: str | None,
    ) -> StatePatch:
        spec, now = provider.spec, self._clock.now()
        async with session_scope(self._sessions) as session:
            row = await self._lock(session, spec.key, spec.capability)
            previous = snapshot_from_state(row, spec, configured=provider.configured, now=now)
            patch = on_failure(
                error, previous=previous, now=now, policy=self._policy, rng=self._rng
            )
            self._apply(session, row, patch, failed=True)
            session.add(
                ProviderUsageEvent(
                    workspace_id=self._workspace_id,
                    provider_key=spec.key,
                    capability=spec.capability,
                    created_at=now,
                    outcome=usage_outcome_for(error),
                    units=Decimal(units),
                    http_status=error.http_status,
                    latency_ms=latency_ms,
                    error_class=type(error).__name__,
                    task_ref=task_ref,
                )
            )
        return patch

    async def add_activity(
        self, level: ActivityLevel, event_type: str, message: str, data: dict[str, Any]
    ) -> None:
        async with session_scope(self._sessions) as session:
            session.add(
                ActivityEvent(
                    workspace_id=self._workspace_id,
                    created_at=self._clock.now(),
                    level=level,
                    category="provider",
                    event_type=event_type,
                    message=message[:500],
                    data=data,
                )
            )

    def _apply(
        self, session: AsyncSession, row: ProviderState, patch: StatePatch, *, failed: bool
    ) -> None:
        for column, value in patch.fields.items():
            if (
                column not in _STATE_COLUMNS
            ):  # a typo must fail loudly, not silently set a Python attribute
                raise KeyError(f"unknown provider_states column {column!r}")
            setattr(row, column, value)
        if failed and patch.count_failure:
            row.failure_count += 1
        row.updated_at = self._clock.now()
        if patch.note is not None:
            level, event_type, message = patch.note
            session.add(
                ActivityEvent(
                    workspace_id=self._workspace_id,
                    created_at=self._clock.now(),
                    level=ActivityLevel(level),
                    category="provider",
                    event_type=event_type,
                    message=message[:500],
                    data={"provider": row.provider_key, "capability": row.capability.value},
                )
            )

    # ───────────────────────────── owner controls ─────────────────────────────

    async def update_settings(
        self, provider_key: str, capability: Capability, **changes: Any
    ) -> None:
        """Settings-page edits. Only whitelisted columns; unknown names raise."""
        unknown = set(changes) - EDITABLE_FIELDS
        if unknown:
            raise ValueError(f"not editable: {sorted(unknown)}")
        async with session_scope(self._sessions) as session:
            row = await self._lock(session, provider_key, capability)
            for column, value in changes.items():
                setattr(row, column, value)
            row.updated_at = self._clock.now()

    async def reset_attention(self, provider_key: str, capability: Capability) -> None:
        """The owner fixed the credential/account: let the provider be tried again (a success will confirm it)."""
        async with session_scope(self._sessions) as session:
            row = await self._lock(session, provider_key, capability)
            if row.status != ProviderStatus.NEEDS_ATTENTION:
                return
            row.status = ProviderStatus.READY
            row.consecutive_failures = 0
            row.circuit_state = CircuitState.CLOSED
            row.cooldown_until = row.rate_limited_until = row.reset_at = None
            row.updated_at = self._clock.now()

    async def purge_usage_events(self, *, older_than_days: float = 30) -> int:
        """Retention for the 500 MB free database: usage rows older than ``older_than_days`` are dropped."""
        cutoff = self._clock.now() - timedelta(days=older_than_days)
        stmt = (
            delete(ProviderUsageEvent)
            .where(ProviderUsageEvent.created_at < cutoff)
            .returning(ProviderUsageEvent.id)
        )
        async with session_scope(self._sessions) as session:
            return len((await session.execute(stmt)).all())

    async def _lock(self, session: AsyncSession, key: str, capability: Capability) -> ProviderState:
        row = (
            await session.execute(
                select(ProviderState)
                .where(
                    ProviderState.workspace_id == self._workspace_id,
                    ProviderState.provider_key == key,
                    ProviderState.capability == capability,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise LookupError(f"provider {key!r}/{capability} has no state row (call sync first)")
        return row
