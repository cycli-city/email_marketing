"""What a provider *is* (static facts) and what a call to it *returns* (value + reported usage)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Generic, TypeVar

from app.domain.enums import Capability

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Static description of one provider *for one capability* (a vendor offering two capabilities has two specs).

    The numbers here are only **defaults** copied into ``provider_states`` the first time a provider is seen; the
    database row (editable on the settings page, refreshed from provider-reported usage) is what runs.
    """

    key: str  # stable id, e.g. "groq"
    capability: Capability
    display_name: str
    free_tier: bool = True
    default_priority: int = 100  # lower = tried first
    daily_limit: int | None = None
    monthly_limit: int | None = None
    rate_limit_per_minute: int | None = None  # in `pacing_unit`s
    pacing_unit: str = "requests"  # "requests" | "tokens" — what rate_limit_per_minute counts
    estimated_cost_per_unit: Decimal = Decimal(0)  # only meaningful for paid providers
    # Sending prospect personal data to a provider that trains on inputs would be a privacy breach. Default is "no";
    # a provider is opted in only after its terms were read (docs/PROVIDERS.md).
    may_receive_personal_data: bool = False
    # Sending is special: when a mailbox hits its limit we WAIT for the reset. We never fail over to another mailbox
    # or provider to get around a sending quota (REQUIREMENTS_AUTONOMY §D/§I).
    fail_over_on_limits: bool = True
    # Quality class within a capability (e.g. LLM "smart" vs "fast"). A call that asks for a tier is only ever served
    # by providers of that tier, so a drafting task is never silently degraded to a small classifier model.
    tier: str | None = None

    def __post_init__(self) -> None:
        if not self.key or not self.key.islower() or " " in self.key:
            raise ValueError(f"provider key must be lowercase without spaces: {self.key!r}")
        if self.capability == Capability.EMAIL_SEND and self.fail_over_on_limits:
            raise ValueError("email_send providers must set fail_over_on_limits=False (ADR-025)")


@dataclass(frozen=True, slots=True)
class ProviderResult(Generic[T]):
    """What an adapter operation returns: the value plus whatever usage the provider *itself* reported.

    Only fill the optional fields from real response data (headers / usage endpoint). The manager never invents
    credits: an unreported figure stays unknown (REQUIREMENTS_AUTONOMY §D).
    """

    value: T
    # Actual billable units, if the provider says so (e.g. tokens, credits).
    units_used: Decimal | None = None
    # Provider-reported remaining allowance, its size, and when it resets.
    remaining: Decimal | None = None
    limit: Decimal | None = None
    reset_at: datetime | None = None
    cost: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CallOutcome(Generic[T]):
    """What the business logic gets back from ``ProviderManager.call``."""

    value: T
    provider_key: str
    failovers: tuple[str, ...]  # providers that were tried and failed before this one, in order
    attempts: int  # total vendor calls made (including in-call retries)
    latency_ms: int
