"""Provider Manager state and usage ledger (REQUIREMENTS_AUTONOMY §C, §D)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, WorkspaceScopedMixin
from app.db.types import enum_col
from app.domain.enums import Capability, CircuitState, CreditsAccuracy, ProviderStatus, UsageOutcome


class ProviderState(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Everything the manager knows about one provider *for one capability*.

    ``remaining_credits`` is only trustworthy according to ``credits_accuracy``; when the provider
    exposes no usage endpoint it is NULL/``unknown`` — never an invented number.
    """

    __tablename__ = "provider_states"
    __table_args__ = (
        UniqueConstraint("workspace_id", "provider_key", "capability"),
        CheckConstraint("priority >= 0", name="priority_non_negative"),
    )

    provider_key: Mapped[str] = mapped_column(String(64))
    capability: Mapped[Capability] = mapped_column(enum_col(Capability))
    enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    priority: Mapped[int] = mapped_column(SmallInteger, default=100, server_default=text("100"))
    status: Mapped[ProviderStatus] = mapped_column(
        enum_col(ProviderStatus), default=ProviderStatus.UNCONFIGURED
    )
    credentials_present: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    plan: Mapped[str | None] = mapped_column(String(64))
    is_free_tier: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    paid_fallback_allowed: Mapped[bool] = mapped_column(default=False, server_default=text("false"))

    credits_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    remaining_credits: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    credits_accuracy: Mapped[CreditsAccuracy] = mapped_column(
        enum_col(CreditsAccuracy), default=CreditsAccuracy.UNKNOWN
    )
    daily_used: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    daily_limit: Mapped[int | None] = mapped_column(Integer)
    usage_day: Mapped[date | None] = mapped_column(Date)
    monthly_used: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    monthly_limit: Mapped[int | None] = mapped_column(Integer)
    usage_month: Mapped[date | None] = mapped_column(Date)  # first day of the tracked month
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer)
    reset_at: Mapped[datetime | None]  # documented quota reset, when known

    rate_limited_until: Mapped[datetime | None]
    cooldown_until: Mapped[datetime | None]
    circuit_state: Mapped[CircuitState] = mapped_column(
        enum_col(CircuitState), default=CircuitState.CLOSED
    )
    failure_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_success_at: Mapped[datetime | None]
    last_failure_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    estimated_cost_per_unit: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal(0), server_default=text("0")
    )
    cost_spent_month: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal(0), server_default=text("0")
    )
    monthly_budget: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))


class ProviderUsageEvent(Base, UUIDPrimaryKeyMixin, WorkspaceScopedMixin):
    """Append-only ledger of provider calls (for usage counters, failover history and the Ops page)."""

    __tablename__ = "provider_usage_events"
    __table_args__ = (
        Index("ix_provider_usage_events_provider_time", "provider_key", "created_at"),
    )

    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    provider_key: Mapped[str] = mapped_column(String(64))
    capability: Mapped[Capability] = mapped_column(enum_col(Capability))
    outcome: Mapped[UsageOutcome] = mapped_column(enum_col(UsageOutcome))
    units: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal(1))
    cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal(0), server_default=text("0")
    )
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_class: Mapped[str | None] = mapped_column(String(120))
    task_ref: Mapped[str | None] = mapped_column(String(200))
