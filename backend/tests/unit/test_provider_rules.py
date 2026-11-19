"""Provider eligibility, ordering and state transitions — pure rules, fake clock, no database."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.retry import BackoffPolicy
from app.domain.enums import Capability, CircuitState, CreditsAccuracy, ProviderStatus
from app.providers.errors import (
    AuthError,
    ForbiddenReason,
    InvalidResponseError,
    PermanentProviderError,
    PermissionDeniedError,
    QuotaExhaustedError,
    RateLimitedError,
    TransientProviderError,
)
from app.providers.rules import (
    ManagerPolicy,
    ProviderSnapshot,
    Reason,
    check_eligibility,
    next_month_start,
    next_utc_midnight,
    on_failure,
    on_success,
    select_providers,
)
from app.providers.spec import ProviderResult

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
PLAIN = ManagerPolicy(
    cooldown_backoff=BackoffPolicy(base_seconds=30, factor=2, max_seconds=1800, jitter="none"),
    default_rate_limit_seconds=60,
    circuit_threshold=3,
)


def snap(key: str = "groq", /, **overrides: object) -> ProviderSnapshot:
    base: dict[str, object] = {
        "key": key,
        "capability": Capability.LLM,
        "enabled": True,
        "configured": True,
        "status": ProviderStatus.READY,
        "priority": 100,
        "is_free": True,
        "paid_fallback_allowed": False,
        "personal_data_ok": True,
        "fail_over_on_limits": True,
    }
    base.update(overrides)
    return ProviderSnapshot(**base)  # type: ignore[arg-type]


def verdict(**overrides: object) -> tuple[Reason | None, datetime | None]:
    result = check_eligibility(snap(**overrides), now=NOW)
    return (None, None) if result is None else (result.reason, result.until)


# ───────────────────────────── eligibility ─────────────────────────────


class TestEligibility:
    def test_a_healthy_configured_provider_is_eligible(self) -> None:
        assert verdict() == (None, None)

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"configured": False}, Reason.UNCONFIGURED),
            ({"enabled": False}, Reason.DISABLED),
            ({"status": ProviderStatus.DISABLED}, Reason.DISABLED),
            ({"status": ProviderStatus.NEEDS_ATTENTION}, Reason.NEEDS_ATTENTION),
        ],
    )
    def test_hard_blockers_have_no_automatic_return_time(
        self, overrides: dict[str, object], reason: Reason
    ) -> None:
        assert verdict(**overrides) == (reason, None)

    def test_a_provider_that_needs_attention_stays_out_however_much_time_passes(self) -> None:
        s = snap(status=ProviderStatus.NEEDS_ATTENTION, cooldown_until=NOW - timedelta(days=30))
        assert check_eligibility(s, now=NOW + timedelta(days=365)) is not None

    def test_personal_data_only_goes_to_providers_cleared_for_it(self) -> None:
        s = snap(personal_data_ok=False)
        assert check_eligibility(s, now=NOW, personal_data=False) is None
        blocked = check_eligibility(s, now=NOW, personal_data=True)
        assert blocked is not None
        assert blocked.reason == Reason.PERSONAL_DATA_NOT_ALLOWED

    def test_exhausted_until_the_documented_reset_then_eligible_again(self) -> None:
        reset = NOW + timedelta(hours=5)
        s = snap(status=ProviderStatus.EXHAUSTED, reset_at=reset)
        assert verdict(status=ProviderStatus.EXHAUSTED, reset_at=reset) == (Reason.EXHAUSTED, reset)
        assert check_eligibility(s, now=reset) is None

    def test_rate_limit_and_cooldown_expire_on_their_own(self) -> None:
        until = NOW + timedelta(seconds=45)
        assert verdict(rate_limited_until=until) == (Reason.RATE_LIMITED, until)
        assert verdict(cooldown_until=until) == (Reason.COOLDOWN, until)
        assert verdict(cooldown_until=until, circuit_state=CircuitState.OPEN) == (
            Reason.CIRCUIT_OPEN,
            until,
        )
        assert check_eligibility(snap(cooldown_until=until), now=until) is None

    def test_daily_limit_resets_at_the_next_utc_midnight(self) -> None:
        assert verdict(daily_used=1000, daily_limit=1000) == (
            Reason.DAILY_LIMIT,
            datetime(2026, 11, 20, tzinfo=UTC),
        )
        assert verdict(daily_used=999, daily_limit=1000) == (None, None)

    def test_monthly_limit_resets_on_the_first_of_next_month(self) -> None:
        assert verdict(monthly_used=50, monthly_limit=50) == (
            Reason.MONTHLY_LIMIT,
            datetime(2026, 12, 1, tzinfo=UTC),
        )

    def test_a_request_needing_more_units_than_remain_is_not_eligible(self) -> None:
        s = snap(daily_used=95, daily_limit=100)
        assert check_eligibility(s, now=NOW, units=5) is None
        assert check_eligibility(s, now=NOW, units=6) is not None

    def test_only_provider_reported_credits_can_block(self) -> None:
        """An internal estimate must never lock a provider out on its own (never invent credits)."""
        guess = snap(
            remaining_credits=Decimal(0), credits_accuracy=CreditsAccuracy.INTERNAL_COUNTER
        )
        assert check_eligibility(guess, now=NOW) is None
        reset = NOW + timedelta(hours=2)
        real = snap(
            remaining_credits=Decimal(0),
            credits_accuracy=CreditsAccuracy.PROVIDER_REPORTED,
            reset_at=reset,
        )
        blocked = check_eligibility(real, now=NOW)
        assert blocked is not None
        assert (blocked.reason, blocked.until) == (Reason.CREDITS_EXHAUSTED, reset)

    @pytest.mark.parametrize("reset_at", [None, NOW - timedelta(minutes=5)])
    def test_a_stale_empty_balance_does_not_lock_the_provider_out_forever(
        self, reset_at: datetime | None
    ) -> None:
        """Nothing refreshes a reported balance while the provider is blocked, so a report whose reset is past
        (or unknown) must let a probe through instead of blocking indefinitely."""
        stale = snap(
            remaining_credits=Decimal(0),
            credits_accuracy=CreditsAccuracy.PROVIDER_REPORTED,
            reset_at=reset_at,
        )
        assert check_eligibility(stale, now=NOW) is None

    def test_paid_providers_need_explicit_opt_in_and_a_budget(self) -> None:
        paid: dict[str, object] = {"is_free": False, "estimated_cost_per_unit": Decimal("0.01")}
        assert verdict(**paid)[0] == Reason.PAID_NOT_ALLOWED
        assert (
            verdict(**paid, paid_fallback_allowed=True)[0] == Reason.BUDGET_EXHAUSTED
        )  # no budget set
        ok = {**paid, "paid_fallback_allowed": True, "monthly_budget": Decimal(5)}
        assert verdict(**ok) == (None, None)
        over = {**ok, "cost_spent_month": Decimal("4.995")}
        assert verdict(**over)[0] == Reason.BUDGET_EXHAUSTED


class TestPeriodHelpers:
    def test_next_midnight_is_computed_in_utc_whatever_the_input_zone(self) -> None:
        ist = datetime(
            2026, 11, 19, 23, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))
        )  # = 18:00 UTC
        assert next_utc_midnight(ist) == datetime(2026, 11, 20, tzinfo=UTC)

    def test_month_start_rolls_the_year(self) -> None:
        assert next_month_start(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == datetime(
            2027, 1, 1, tzinfo=UTC
        )
        assert next_month_start(datetime(2026, 1, 31, tzinfo=UTC)) == datetime(
            2026, 2, 1, tzinfo=UTC
        )


# ───────────────────────────── selection ─────────────────────────────


class TestSelection:
    def order(self, *snaps: ProviderSnapshot, **kwargs: object) -> list[str]:
        return [p.key for p in select_providers(list(snaps), now=NOW, **kwargs).ordered]  # type: ignore[arg-type]

    def test_orders_by_priority_number(self) -> None:
        assert self.order(
            snap("b", priority=20), snap("a", priority=10), snap("c", priority=30)
        ) == ["a", "b", "c"]

    def test_free_providers_always_come_before_paid(self) -> None:
        paid = snap(
            "paid",
            is_free=False,
            paid_fallback_allowed=True,
            monthly_budget=Decimal(10),
            priority=1,
        )
        assert self.order(paid, snap("free", priority=999)) == ["free", "paid"]

    def test_equal_priority_prefers_the_provider_with_more_headroom(self) -> None:
        busy = snap("busy", daily_used=900, daily_limit=1000)
        idle = snap("idle", daily_used=100, daily_limit=1000)
        assert self.order(busy, idle) == ["idle", "busy"]

    def test_excluded_providers_are_skipped_and_not_reported(self) -> None:
        sel = select_providers([snap("a"), snap("b")], now=NOW, exclude=frozenset({"a"}))
        assert [p.key for p in sel.ordered] == ["b"]
        assert "a" not in sel.unavailable

    def test_next_available_is_the_earliest_return_time(self) -> None:
        soon, later = NOW + timedelta(minutes=2), NOW + timedelta(hours=3)
        sel = select_providers(
            [
                snap("a", cooldown_until=later),
                snap("b", rate_limited_until=soon),
                snap("c", configured=False),
            ],
            now=NOW,
        )
        assert not sel.ordered
        assert sel.next_available_at == soon
        assert set(sel.unavailable) == {"a", "b", "c"}

    def test_nothing_is_available_without_a_human_when_every_blocker_is_manual(self) -> None:
        sel = select_providers(
            [snap("a", configured=False), snap("b", status=ProviderStatus.NEEDS_ATTENTION)], now=NOW
        )
        assert sel.next_available_at is None


# ───────────────────────────── transitions ─────────────────────────────


class TestOnSuccess:
    def test_resets_the_failure_streak_and_closes_the_circuit(self) -> None:
        previous = snap(
            status=ProviderStatus.COOLDOWN,
            consecutive_failures=4,
            circuit_state=CircuitState.OPEN,
            cooldown_until=NOW - timedelta(seconds=1),
        )
        patch = on_success(ProviderResult("ok"), previous=previous, now=NOW)
        assert patch.fields["status"] == ProviderStatus.READY
        assert patch.fields["consecutive_failures"] == 0
        assert patch.fields["circuit_state"] == CircuitState.CLOSED
        assert patch.fields["cooldown_until"] is None
        assert patch.note is not None
        assert patch.note[1] == "provider_recovered"

    def test_a_routine_success_is_silent(self) -> None:
        assert on_success(ProviderResult("ok"), previous=snap(), now=NOW).note is None

    def test_provider_reported_usage_is_recorded_as_such(self) -> None:
        reset = NOW + timedelta(hours=2)
        result = ProviderResult("ok", remaining=Decimal(742), limit=Decimal(1000), reset_at=reset)
        fields = on_success(result, previous=snap(), now=NOW).fields
        assert fields["remaining_credits"] == Decimal(742)
        assert fields["credits_total"] == Decimal(1000)
        assert fields["credits_accuracy"] == CreditsAccuracy.PROVIDER_REPORTED
        assert fields["reset_at"] == reset

    def test_credits_are_never_invented_when_the_provider_reports_none(self) -> None:
        fields = on_success(ProviderResult("ok"), previous=snap(), now=NOW).fields
        assert "remaining_credits" not in fields
        assert "credits_accuracy" not in fields

    def test_cost_accumulates_for_budgeting(self) -> None:
        previous = snap(cost_spent_month=Decimal("1.50"))
        fields = on_success(
            ProviderResult("ok", cost=Decimal("0.25")), previous=previous, now=NOW
        ).fields
        assert fields["cost_spent_month"] == Decimal("1.75")


class TestOnFailure:
    def test_429_honours_retry_after_and_is_not_a_fault(self) -> None:
        patch = on_failure(
            RateLimitedError(retry_after=120),
            previous=snap(consecutive_failures=2),
            now=NOW,
            policy=PLAIN,
        )
        until = patch.fields["rate_limited_until"]
        assert NOW + timedelta(seconds=120) <= until <= NOW + timedelta(seconds=133)
        assert patch.fields["status"] == ProviderStatus.RATE_LIMITED
        assert "consecutive_failures" not in patch.fields
        assert "circuit_state" not in patch.fields

    def test_429_without_retry_after_still_backs_off(self) -> None:
        patch = on_failure(RateLimitedError(), previous=snap(), now=NOW, policy=PLAIN)
        assert patch.fields["rate_limited_until"] >= NOW + timedelta(seconds=60)

    def test_quota_exhaustion_with_a_known_reset_waits_for_exactly_that(self) -> None:
        reset = NOW + timedelta(hours=7)
        patch = on_failure(
            QuotaExhaustedError(reset_at=reset), previous=snap(), now=NOW, policy=PLAIN
        )
        assert (patch.fields["status"], patch.fields["reset_at"], patch.available_at) == (
            ProviderStatus.EXHAUSTED,
            reset,
            reset,
        )

    @pytest.mark.parametrize("reset_at", [None, NOW - timedelta(minutes=1)])
    def test_unknown_or_stale_reset_means_recheck_in_about_an_hour(
        self, reset_at: datetime | None
    ) -> None:
        patch = on_failure(
            QuotaExhaustedError(reset_at=reset_at),
            previous=snap(),
            now=NOW,
            policy=PLAIN,
            rng=random.Random(1),
        )
        wait = patch.fields["reset_at"] - NOW
        assert timedelta(minutes=30) <= wait <= timedelta(minutes=90)

    def test_401_stops_using_the_provider_until_a_human_fixes_it(self) -> None:
        patch = on_failure(AuthError(), previous=snap(), now=NOW, policy=PLAIN)
        assert patch.fields["status"] == ProviderStatus.NEEDS_ATTENTION
        assert patch.available_at is None
        assert patch.note is not None
        assert patch.note[0] == "error"

    @pytest.mark.parametrize(
        "reason",
        [ForbiddenReason.PERMISSIONS, ForbiddenReason.ACCOUNT_RESTRICTED, ForbiddenReason.UNKNOWN],
    )
    def test_403_for_permissions_or_account_problems_needs_attention(
        self, reason: ForbiddenReason
    ) -> None:
        patch = on_failure(
            PermissionDeniedError(reason=reason), previous=snap(), now=NOW, policy=PLAIN
        )
        assert patch.fields["status"] == ProviderStatus.NEEDS_ATTENTION
        assert patch.available_at is None

    def test_403_that_means_quota_is_treated_as_exhaustion(self) -> None:
        reset = NOW + timedelta(hours=1)
        error = PermissionDeniedError(reason=ForbiddenReason.QUOTA, reset_at=reset)
        patch = on_failure(error, previous=snap(), now=NOW, policy=PLAIN)
        assert patch.fields["status"] == ProviderStatus.EXHAUSTED
        assert patch.fields["reset_at"] == reset

    def test_403_policy_rejection_leaves_the_provider_untouched(self) -> None:
        """A refusal of one request is not evidence the provider is unhealthy."""
        patch = on_failure(
            PermissionDeniedError(reason=ForbiddenReason.POLICY),
            previous=snap(),
            now=NOW,
            policy=PLAIN,
        )
        assert patch.fields == {}
        assert not patch.count_failure

    def test_transient_failures_cool_down_with_growing_backoff(self) -> None:
        waits = []
        previous = snap()
        for streak in range(1, 5):
            patch = on_failure(
                TransientProviderError("503"), previous=previous, now=NOW, policy=PLAIN
            )
            waits.append((patch.fields["cooldown_until"] - NOW).total_seconds())
            previous = snap(consecutive_failures=patch.fields["consecutive_failures"])
            assert previous.consecutive_failures == streak
        assert waits == [30, 60, 120, 240]

    def test_the_circuit_opens_at_the_threshold(self) -> None:
        below = on_failure(
            TransientProviderError("x"),
            previous=snap(consecutive_failures=1),
            now=NOW,
            policy=PLAIN,
        )
        at = on_failure(
            TransientProviderError("x"),
            previous=snap(consecutive_failures=2),
            now=NOW,
            policy=PLAIN,
        )
        assert below.fields["circuit_state"] == CircuitState.CLOSED
        assert at.fields["circuit_state"] == CircuitState.OPEN

    def test_the_activity_feed_is_told_once_not_on_every_failure(self) -> None:
        first = on_failure(
            TransientProviderError("x"),
            previous=snap(consecutive_failures=0),
            now=NOW,
            policy=PLAIN,
        )
        second = on_failure(
            TransientProviderError("x"),
            previous=snap(consecutive_failures=1),
            now=NOW,
            policy=PLAIN,
        )
        opened = on_failure(
            TransientProviderError("x"),
            previous=snap(consecutive_failures=2),
            now=NOW,
            policy=PLAIN,
        )
        assert first.note is not None
        assert second.note is None
        assert opened.note is not None

    @pytest.mark.parametrize(
        "error", [InvalidResponseError("bad json"), PermanentProviderError("422")]
    )
    def test_unusable_requests_and_responses_do_not_take_the_provider_offline(
        self, error: Exception
    ) -> None:
        patch = on_failure(error, previous=snap(), now=NOW, policy=PLAIN)  # type: ignore[arg-type]
        assert "status" not in patch.fields
        assert "cooldown_until" not in patch.fields
        assert patch.fields["last_error"].startswith(type(error).__name__)
