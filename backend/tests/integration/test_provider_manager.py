"""ProviderManager end to end on a real PostgreSQL: selection, failover, retries, cooldowns, quotas, safety rules."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FakeClock
from app.core.retry import BackoffPolicy
from app.db.models import ActivityEvent, ProviderState, ProviderUsageEvent
from app.domain.enums import Capability, CircuitState, CreditsAccuracy, ProviderStatus, UsageOutcome
from app.providers.errors import (
    AllProvidersUnavailableError,
    AuthError,
    ForbiddenReason,
    InvalidResponseError,
    PermanentProviderError,
    PermissionDeniedError,
    ProviderLimitReachedError,
    QuotaExhaustedError,
    RateLimitedError,
    TransientProviderError,
    UncertainOutcomeError,
)
from app.providers.manager import ProviderManager
from app.providers.mock import MockProvider, respond
from app.providers.registry import ProviderRegistry
from app.providers.rules import ManagerPolicy
from app.providers.spec import ProviderResult, ProviderSpec
from app.providers.store import ProviderStore

Factory = async_sessionmaker[AsyncSession]

POLICY = ManagerPolicy(
    max_attempts_per_provider=3,
    in_call_backoff=BackoffPolicy(base_seconds=1, factor=2, max_seconds=8, jitter="none"),
    cooldown_backoff=BackoffPolicy(base_seconds=30, factor=2, max_seconds=1800, jitter="none"),
    default_rate_limit_seconds=60,
    circuit_threshold=2,
    probe_window_seconds=60,
)


def spec(key: str, *, priority: int = 100, **kwargs: Any) -> ProviderSpec:
    kwargs.setdefault("may_receive_personal_data", True)
    return ProviderSpec(key, Capability.LLM, key.title(), default_priority=priority, **kwargs)


class Harness:
    """Builds a manager over mock providers; sleeps are recorded instead of waited."""

    def __init__(self, factory: Factory, workspace_id: uuid.UUID, clock: FakeClock) -> None:
        self.factory, self.workspace_id, self.clock = factory, workspace_id, clock
        self.registry = ProviderRegistry()
        self.sleeps: list[float] = []
        self.store = ProviderStore(factory, clock, workspace_id, policy=POLICY)

        async def fake_sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        self.manager = ProviderManager(
            self.store, self.registry, clock, policy=POLICY, sleep=fake_sleep
        )

    def add(
        self,
        key: str,
        *,
        configured: bool = True,
        script: list[Any] | None = None,
        **spec_kwargs: Any,
    ) -> MockProvider:
        provider = MockProvider(spec(key, **spec_kwargs), configured=configured, script=script)
        self.registry.register(provider)
        return provider

    async def call(self, **kwargs: Any) -> Any:
        return await self.manager.call(Capability.LLM, respond, **kwargs)

    async def state(self, key: str) -> ProviderState:
        async with self.factory() as s:
            return (
                await s.execute(
                    select(ProviderState).where(
                        ProviderState.workspace_id == self.workspace_id,
                        ProviderState.provider_key == key,
                    )
                )
            ).scalar_one()

    async def usage(self) -> list[ProviderUsageEvent]:
        async with self.factory() as s:
            return list(
                (
                    await s.execute(
                        select(ProviderUsageEvent).order_by(ProviderUsageEvent.created_at)
                    )
                ).scalars()
            )

    async def activity(self) -> list[str]:
        async with self.factory() as s:
            return [
                e.event_type
                for e in (
                    await s.execute(select(ActivityEvent).order_by(ActivityEvent.created_at))
                ).scalars()
            ]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 19, 10, 0, tzinfo=UTC))


@pytest.fixture
def h(factory: Factory, workspace_id: uuid.UUID, clock: FakeClock) -> Harness:
    return Harness(factory, workspace_id, clock)


# ───────────────────────────── registration ─────────────────────────────


class TestStart:
    async def test_creates_a_state_row_per_provider_from_the_spec_defaults(
        self, h: Harness
    ) -> None:
        h.add("groq", daily_limit=1000, rate_limit_per_minute=8000, priority=10)
        h.add("keyless", configured=False)
        await h.manager.start()
        groq, keyless = await h.state("groq"), await h.state("keyless")
        assert (groq.status, groq.credentials_present, groq.priority, groq.daily_limit) == (
            ProviderStatus.READY,
            True,
            10,
            1000,
        )
        assert (keyless.status, keyless.credentials_present) == (ProviderStatus.UNCONFIGURED, False)

    async def test_is_idempotent_and_never_overwrites_the_owners_choices(self, h: Harness) -> None:
        h.add("groq", priority=10)
        await h.manager.start()
        await h.manager.update_settings(
            "groq", Capability.LLM, priority=3, daily_limit=77, enabled=False
        )
        await h.manager.start()
        row = await h.state("groq")
        assert (row.priority, row.daily_limit, row.enabled) == (3, 77, False)

    async def test_a_provider_whose_key_appears_later_becomes_ready(self, h: Harness) -> None:
        late = h.add("late", configured=False)
        await h.manager.start()
        late.set_configured(True)
        await h.manager.start()
        assert (await h.state("late")).status == ProviderStatus.READY

    async def test_only_whitelisted_settings_can_be_edited(self, h: Harness) -> None:
        h.add("groq")
        await h.manager.start()
        with pytest.raises(ValueError, match="not editable"):
            await h.manager.update_settings("groq", Capability.LLM, consecutive_failures=0)


# ───────────────────────────── choosing & counting ─────────────────────────────


class TestSelectionAndUsage:
    async def test_uses_the_highest_priority_provider_and_counts_the_call(self, h: Harness) -> None:
        primary, backup = h.add("primary", priority=10), h.add("backup", priority=20)
        outcome = await h.call(task_ref="t-1")
        assert (outcome.value, outcome.provider_key, outcome.failovers) == (
            "ok:primary",
            "primary",
            (),
        )
        assert (primary.calls, backup.calls) == (1, 0)
        row = await h.state("primary")
        assert (row.daily_used, row.monthly_used, row.status) == (1, 1, ProviderStatus.READY)
        assert row.last_success_at == h.clock.now()
        (event,) = await h.usage()
        assert (event.provider_key, event.outcome, event.task_ref) == (
            "primary",
            UsageOutcome.SUCCESS,
            "t-1",
        )

    async def test_the_owners_priority_change_takes_effect(self, h: Harness) -> None:
        a, b = h.add("a", priority=10), h.add("b", priority=20)
        await h.manager.start()
        await h.manager.update_settings("b", Capability.LLM, priority=1)
        assert (await h.call()).provider_key == "b"
        assert (a.calls, b.calls) == (0, 1)

    async def test_disabled_and_unconfigured_providers_are_never_called(self, h: Harness) -> None:
        off, keyless, on = (
            h.add("off", priority=1),
            h.add("keyless", priority=2, configured=False),
            h.add("on", priority=3),
        )
        await h.manager.start()
        await h.manager.update_settings("off", Capability.LLM, enabled=False)
        assert (await h.call()).provider_key == "on"
        assert (off.calls, keyless.calls, on.calls) == (0, 0, 1)

    async def test_a_tier_is_only_ever_served_by_that_tier_even_when_it_is_exhausted(
        self, h: Harness
    ) -> None:
        """A drafting task must never be silently degraded to the small classifier model."""
        smart = h.add("smart", priority=2, tier="smart")
        fast = h.add("fast", priority=1, tier="fast", script=[QuotaExhaustedError("done")])
        assert (await h.call(tier="smart")).provider_key == "smart"
        with pytest.raises(AllProvidersUnavailableError):
            await h.call(tier="fast")
        assert (smart.calls, fast.calls) == (1, 1)

    async def test_the_restriction_only_list_limits_the_candidates(self, h: Harness) -> None:
        h.add("a", priority=1)
        b = h.add("b", priority=2)
        assert (await h.call(only={"b"})).provider_key == "b"
        assert b.calls == 1

    async def test_the_daily_counter_is_exact_and_rolls_over_at_utc_midnight(
        self, h: Harness
    ) -> None:
        h.add("only", daily_limit=3)
        for _ in range(3):
            await h.call()
        with pytest.raises(AllProvidersUnavailableError) as blocked:
            await h.call()
        assert blocked.value.next_available_at == datetime(2026, 11, 20, tzinfo=UTC)
        assert (await h.state("only")).daily_used == 3, "a refused call must not consume quota"

        h.clock.set(datetime(2026, 11, 20, 0, 0, 1, tzinfo=UTC))
        await h.call()
        assert (await h.state("only")).daily_used == 1

    async def test_racing_tasks_can_never_overspend_the_quota(self, h: Harness) -> None:
        provider = h.add("only", daily_limit=10)
        await h.manager.start()

        async def attempt() -> str:
            try:
                return str((await h.call()).provider_key)
            except AllProvidersUnavailableError:
                return "refused"

        results = await asyncio.gather(*(attempt() for _ in range(30)))
        assert results.count("only") == 10
        assert results.count("refused") == 20
        assert provider.calls == 10
        assert (await h.state("only")).daily_used == 10

    async def test_a_call_needing_more_units_than_remain_goes_elsewhere(self, h: Harness) -> None:
        h.add("small", priority=1, daily_limit=5)
        big = h.add("big", priority=2)
        assert (await h.call(units=6)).provider_key == "big"
        assert big.calls == 1

    async def test_provider_reported_usage_is_stored_with_its_accuracy(self, h: Harness) -> None:
        reset = h.clock.now() + timedelta(hours=3)
        reported = ProviderResult("ok", remaining=Decimal(742), limit=Decimal(1000), reset_at=reset)
        h.add("groq", script=[reported])
        await h.call()
        row = await h.state("groq")
        assert (row.remaining_credits, row.credits_total, row.credits_accuracy, row.reset_at) == (
            Decimal(742),
            Decimal(1000),
            CreditsAccuracy.PROVIDER_REPORTED,
            reset,
        )

    async def test_credits_stay_unknown_when_the_provider_reports_nothing(self, h: Harness) -> None:
        h.add("quiet")
        await h.call()
        row = await h.state("quiet")
        assert (row.remaining_credits, row.credits_accuracy) == (None, CreditsAccuracy.UNKNOWN)

    async def test_personal_data_only_reaches_providers_cleared_for_it(self, h: Harness) -> None:
        strict = h.add("trains-on-inputs", priority=1, may_receive_personal_data=False)
        safe = h.add("safe", priority=2)
        assert (await h.call(personal_data=True)).provider_key == "safe"
        assert (await h.call(personal_data=False)).provider_key == "trains-on-inputs"
        assert (strict.calls, safe.calls) == (1, 1)

    async def test_paid_fallback_needs_opt_in_and_stops_at_the_budget(self, h: Harness) -> None:
        paid = h.add("paid", priority=1, free_tier=False, estimated_cost_per_unit=Decimal("0.40"))
        free = h.add("free", priority=2, daily_limit=1)
        await h.manager.start()
        assert (await h.call()).provider_key == "free"
        with pytest.raises(
            AllProvidersUnavailableError
        ) as no_optin:  # free exhausted; paid not allowed
            await h.call()
        assert "paid_not_allowed" in no_optin.value.reasons["paid"]

        await h.manager.update_settings(
            "paid", Capability.LLM, paid_fallback_allowed=True, monthly_budget=Decimal("1.00")
        )
        paid.queue(
            ProviderResult("ok", cost=Decimal("0.40")), ProviderResult("ok", cost=Decimal("0.40"))
        )
        assert (await h.call()).provider_key == "paid"
        assert (await h.call()).provider_key == "paid"
        with pytest.raises(
            AllProvidersUnavailableError
        ) as over:  # 0.80 spent, +0.40 would exceed 1.00
            await h.call()
        assert "budget_exhausted" in over.value.reasons["paid"]
        assert (await h.state("paid")).cost_spent_month == Decimal("0.80")
        assert free.calls == 1


# ───────────────────────────── failover ─────────────────────────────


class TestFailover:
    async def test_quota_exhaustion_switches_provider_and_returns_at_the_reported_reset(
        self, h: Harness
    ) -> None:
        reset = h.clock.now() + timedelta(hours=5)
        primary = h.add(
            "primary", priority=1, script=[QuotaExhaustedError("daily limit", reset_at=reset)]
        )
        backup = h.add("backup", priority=2)
        first = await h.call()
        assert (first.provider_key, first.failovers) == ("backup", ("primary",))
        row = await h.state("primary")
        assert (row.status, row.reset_at) == (ProviderStatus.EXHAUSTED, reset)

        # While exhausted it is not even tried.
        assert (await h.call()).provider_key == "backup"
        assert primary.calls == 1

        # Once the documented reset passes it is eligible again, and a success restores it.
        h.clock.set(reset)
        assert (await h.call()).provider_key == "primary"
        assert (await h.state("primary")).status == ProviderStatus.READY
        assert backup.calls == 2

    async def test_the_activity_feed_records_the_switch(self, h: Harness) -> None:
        h.add("primary", priority=1, script=[QuotaExhaustedError("done")])
        h.add("backup", priority=2)
        await h.call()
        events = await h.activity()
        assert "provider_exhausted" in events
        assert "provider_failover" in events

    async def test_rate_limits_are_respected_and_not_hammered(self, h: Harness) -> None:
        primary = h.add("primary", priority=1, script=[RateLimitedError(retry_after=90)])
        h.add("backup", priority=2)
        await h.call()
        for _ in range(5):
            assert (await h.call()).provider_key == "backup"
        assert primary.calls == 1, "no further requests while the provider said to wait"
        row = await h.state("primary")
        assert row.status == ProviderStatus.RATE_LIMITED
        assert row.consecutive_failures == 0
        h.clock.advance(seconds=100)
        assert (await h.call()).provider_key == "primary"

    async def test_transient_errors_are_retried_in_place_with_backoff(self, h: Harness) -> None:
        primary = h.add(
            "primary",
            priority=1,
            script=[TransientProviderError("503"), TransientProviderError("503")],
        )
        backup = h.add("backup", priority=2)
        outcome = await h.call()
        assert (outcome.provider_key, outcome.attempts, outcome.failovers) == ("primary", 3, ())
        assert (primary.calls, backup.calls) == (3, 0)
        assert h.sleeps == [1.0, 2.0], "exponential backoff between in-call retries"
        row = await h.state("primary")
        assert (row.status, row.consecutive_failures) == (ProviderStatus.READY, 0)

    async def test_a_provider_that_keeps_failing_goes_into_cooldown_and_work_continues_elsewhere(
        self, h: Harness
    ) -> None:
        down = [TransientProviderError("503")] * 3
        primary = h.add("primary", priority=1, script=list(down))
        backup = h.add("backup", priority=2)
        outcome = await h.call()
        assert (outcome.provider_key, outcome.failovers, primary.calls) == (
            "backup",
            ("primary",),
            3,
        )
        row = await h.state("primary")
        assert row.status == ProviderStatus.COOLDOWN
        assert row.consecutive_failures == 1
        assert row.cooldown_until == h.clock.now() + timedelta(seconds=30)

        assert (await h.call()).provider_key == "backup"
        assert primary.calls == 3, "cooling down: not tried"
        h.clock.advance(seconds=31)
        primary.queue(*down)
        assert (
            await h.call()
        ).provider_key == "backup"  # second sequence fails too → circuit opens
        row = await h.state("primary")
        assert (row.consecutive_failures, row.circuit_state) == (2, CircuitState.OPEN)
        assert backup.calls == 3

    async def test_after_an_outage_exactly_one_probe_goes_through_and_success_closes_the_circuit(
        self, h: Harness
    ) -> None:
        gate = asyncio.Event()

        async def hold() -> str:
            await gate.wait()
            return "recovered"

        primary = h.add("primary", script=[TransientProviderError("x")] * 6)
        for _ in range(2):  # two failed sequences → circuit open
            with pytest.raises(AllProvidersUnavailableError):
                await h.call()
            h.clock.advance(seconds=3600)
        assert (await h.state("primary")).circuit_state == CircuitState.OPEN
        primary.queue(hold)
        before = primary.calls

        probes = {asyncio.create_task(h.call()) for _ in range(4)}
        # The three losers are turned away at once; the winner is parked on the gate.
        losers: set[asyncio.Task[Any]] = set()
        pending = set(probes)
        async with asyncio.timeout(15):
            while len(losers) < 3:
                finished, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                losers |= finished
        assert all(isinstance(t.exception(), AllProvidersUnavailableError) for t in losers)
        assert primary.calls == before + 1, "only one probe request reached the recovering provider"

        gate.set()
        (winner,) = pending
        assert (await winner).value == "recovered"
        row = await h.state("primary")
        assert (row.circuit_state, row.consecutive_failures, row.status) == (
            CircuitState.CLOSED,
            0,
            ProviderStatus.READY,
        )

    async def test_a_401_stops_the_provider_until_the_owner_fixes_it(self, h: Harness) -> None:
        primary = h.add("primary", priority=1, script=[AuthError("invalid api key")])
        backup = h.add("backup", priority=2)
        assert (await h.call()).provider_key == "backup"
        assert (await h.state("primary")).status == ProviderStatus.NEEDS_ATTENTION

        h.clock.advance(days=30)  # time does not heal a rejected credential
        assert (await h.call()).provider_key == "backup"
        assert primary.calls == 1
        assert "provider_auth_failed" in await h.activity()

        await h.manager.reset_attention("primary", Capability.LLM)
        assert (await h.call()).provider_key == "primary"
        assert backup.calls == 2

    async def test_a_403_about_permissions_needs_attention_and_fails_over(self, h: Harness) -> None:
        h.add(
            "primary",
            priority=1,
            script=[
                PermissionDeniedError(
                    "plan lacks this endpoint", reason=ForbiddenReason.PERMISSIONS
                )
            ],
        )
        h.add("backup", priority=2)
        assert (await h.call()).provider_key == "backup"
        assert (await h.state("primary")).status == ProviderStatus.NEEDS_ATTENTION

    async def test_a_403_that_means_quota_is_handled_as_exhaustion(self, h: Harness) -> None:
        h.add(
            "primary",
            priority=1,
            script=[PermissionDeniedError("quota", reason=ForbiddenReason.QUOTA)],
        )
        h.add("backup", priority=2)
        await h.call()
        assert (await h.state("primary")).status == ProviderStatus.EXHAUSTED

    async def test_a_policy_refusal_is_not_shopped_around_and_does_not_penalise_the_provider(
        self, h: Harness
    ) -> None:
        primary = h.add(
            "primary",
            priority=1,
            script=[PermissionDeniedError("content refused", reason=ForbiddenReason.POLICY)],
        )
        backup = h.add("backup", priority=2)
        with pytest.raises(PermissionDeniedError):
            await h.call()
        assert backup.calls == 0, "must not look for a more permissive provider"
        row = await h.state("primary")
        assert (row.status, row.consecutive_failures, row.failure_count) == (
            ProviderStatus.READY,
            0,
            0,
        )
        assert primary.calls == 1

    @pytest.mark.parametrize(
        "error", [InvalidResponseError("not json"), PermanentProviderError("422 bad request")]
    )
    async def test_unusable_requests_and_responses_are_not_retried_or_rerouted(
        self, h: Harness, error: Exception
    ) -> None:
        primary = h.add("primary", priority=1, script=[error])
        backup = h.add("backup", priority=2)
        with pytest.raises(type(error)):
            await h.call()
        assert (primary.calls, backup.calls) == (1, 0)
        row = await h.state("primary")
        assert (row.status, row.cooldown_until) == (ProviderStatus.READY, None)
        assert row.daily_used == 1, "the provider processed it, so the quota stays counted"

    async def test_calls_the_provider_did_not_count_are_refunded(self, h: Harness) -> None:
        h.add("primary", priority=1, script=[TransientProviderError("x")] * 3)
        h.add("backup", priority=2)
        await h.call()
        assert (await h.state("primary")).daily_used == 0
        assert (await h.state("backup")).daily_used == 1

    async def test_failures_are_logged_as_usage_events_with_their_outcome(self, h: Harness) -> None:
        h.add("primary", priority=1, script=[RateLimitedError(retry_after=5)])
        h.add("backup", priority=2)
        await h.call()
        outcomes = {(e.provider_key, e.outcome, e.http_status) for e in await h.usage()}
        assert outcomes == {
            ("primary", UsageOutcome.RATE_LIMITED, 429),
            ("backup", UsageOutcome.SUCCESS, None),
        }


# ───────────────────────────── nothing available ─────────────────────────────


class TestAllUnavailable:
    async def test_reports_when_the_earliest_provider_returns_so_the_task_can_park(
        self, h: Harness
    ) -> None:
        h.add("a", priority=1, script=[RateLimitedError(retry_after=120)])
        h.add(
            "b",
            priority=2,
            script=[QuotaExhaustedError("done", reset_at=h.clock.now() + timedelta(hours=4))],
        )
        with pytest.raises(AllProvidersUnavailableError) as raised:
            await h.call()
        resume = raised.value.next_available_at
        assert resume is not None
        assert (
            h.clock.now() + timedelta(seconds=120)
            <= resume
            <= h.clock.now() + timedelta(seconds=133)
        )
        assert set(raised.value.reasons) == {"a", "b"}

    async def test_nothing_configured_needs_a_human_not_a_timer(self, h: Harness) -> None:
        h.add("a", configured=False)
        with pytest.raises(AllProvidersUnavailableError) as raised:
            await h.call()
        assert raised.value.next_available_at is None
        assert "unconfigured" in raised.value.reasons["a"]

    async def test_no_registered_providers_at_all(self, h: Harness) -> None:
        with pytest.raises(AllProvidersUnavailableError) as raised:
            await h.call()
        assert raised.value.reasons == {}
        assert raised.value.next_available_at is None


# ───────────────────────────── sending safety rules ─────────────────────────────


class TestSendingSafety:
    async def test_a_sending_limit_is_never_routed_around(self, h: Harness) -> None:
        h.registry.register(
            sender := MockProvider(
                ProviderSpec(
                    "mailbox-a",
                    Capability.EMAIL_SEND,
                    "Mailbox A",
                    fail_over_on_limits=False,
                    default_priority=1,
                ),
                script=[RateLimitedError(retry_after=3600)],
            )
        )
        other = MockProvider(
            ProviderSpec(
                "mailbox-b",
                Capability.EMAIL_SEND,
                "Mailbox B",
                fail_over_on_limits=False,
                default_priority=2,
            )
        )
        h.registry.register(other)
        with pytest.raises(ProviderLimitReachedError) as limit:
            await h.manager.call(Capability.EMAIL_SEND, respond)
        assert limit.value.provider_key == "mailbox-a"
        assert limit.value.resume_at is not None
        assert (sender.calls, other.calls) == (1, 0), (
            "no second mailbox was used to dodge the limit"
        )

    async def test_a_send_that_timed_out_after_dispatch_is_never_repeated(self, h: Harness) -> None:
        timed_out = TransientProviderError("read timeout", request_may_have_been_processed=True)
        primary = h.add("primary", priority=1, script=[timed_out])
        backup = h.add("backup", priority=2)
        with pytest.raises(UncertainOutcomeError):
            await h.call(idempotent=False)
        assert (primary.calls, backup.calls) == (1, 0)
        assert h.sleeps == [], "no retry delay was even scheduled"

    async def test_a_failure_before_dispatch_may_still_be_retried_for_a_non_idempotent_call(
        self, h: Harness
    ) -> None:
        primary = h.add("primary", script=[TransientProviderError("connection refused")])
        outcome = await h.call(idempotent=False)
        assert (outcome.provider_key, primary.calls, outcome.attempts) == ("primary", 2, 2)


# ───────────────────────────── pacing, resilience, views ─────────────────────────────


class TestPacingAndResilience:
    async def test_a_full_per_minute_window_diverts_to_another_provider(self, h: Harness) -> None:
        tight = h.add("tight", priority=1, rate_limit_per_minute=100, pacing_unit="tokens")
        loose = h.add("loose", priority=2)
        assert (await h.call(weight=80)).provider_key == "tight"
        assert (await h.call(weight=80)).provider_key == "loose"
        assert (tight.calls, loose.calls) == (1, 1)
        h.clock.advance(seconds=61)
        assert (await h.call(weight=80)).provider_key == "tight"

    async def test_with_nowhere_else_to_go_the_task_is_told_when_the_window_opens(
        self, h: Harness
    ) -> None:
        h.add("tight", rate_limit_per_minute=100)
        await h.call(weight=80)
        with pytest.raises(AllProvidersUnavailableError) as raised:
            await h.call(weight=80)
        assert raised.value.next_available_at is not None
        assert raised.value.next_available_at <= h.clock.now() + timedelta(seconds=60)

    async def test_a_bookkeeping_failure_never_discards_a_successful_result(
        self, h: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = h.add("primary")
        await h.manager.start()

        async def broken(*args: Any, **kwargs: Any) -> None:
            raise ConnectionError("database blip")

        monkeypatch.setattr(h.store, "record_success", broken)
        outcome = await h.call()
        assert outcome.value == "ok:primary"
        assert provider.calls == 1, "the call is not repeated or lost"

    async def test_the_overview_shows_real_figures_and_the_reason_for_unavailability(
        self, h: Harness
    ) -> None:
        h.add("groq", priority=1, daily_limit=1000, monthly_limit=30000)
        h.add("cool", priority=2, script=[RateLimitedError(retry_after=300)])
        h.add("keyless", priority=3, configured=False)
        await h.call()
        with pytest.raises(AllProvidersUnavailableError):
            await h.call(only={"cool"})
        views = {v.key: v for v in await h.manager.overview(Capability.LLM)}
        assert (
            views["groq"].daily_used,
            views["groq"].daily_limit,
            views["groq"].effective_state,
        ) == (1, 1000, "available")
        assert views["groq"].credits_accuracy == "unknown"
        assert views["groq"].remaining_credits is None
        assert views["cool"].effective_state.startswith("rate_limited")
        assert views["cool"].available_again_at is not None
        assert views["keyless"].effective_state == "unconfigured"
        assert not views["keyless"].configured


async def test_old_usage_events_can_be_purged(h: Harness) -> None:
    h.add("primary")
    await h.call()
    h.clock.advance(days=31)
    assert await h.store.purge_usage_events(older_than_days=30) == 1
    async with h.factory() as s:
        assert (
            await s.execute(select(func.count()).select_from(ProviderUsageEvent))
        ).scalar_one() == 0
