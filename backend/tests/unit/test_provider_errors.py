from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.enums import Capability
from app.providers.errors import (
    AuthError,
    ForbiddenReason,
    PermanentProviderError,
    PermissionDeniedError,
    RateLimitedError,
    TransientProviderError,
    error_from_status,
    parse_retry_after,
)
from app.providers.spec import ProviderSpec

NOW = datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)


class TestParseRetryAfter:
    @pytest.mark.parametrize(
        ("raw", "expected"), [("30", 30.0), (" 2.5 ", 2.5), ("0", 0.0), ("-5", 0.0)]
    )
    def test_delta_seconds(self, raw: str, expected: float) -> None:
        assert parse_retry_after(raw, now=NOW) == expected

    def test_http_date(self) -> None:
        assert parse_retry_after("Thu, 19 Nov 2026 10:01:30 GMT", now=NOW) == 90.0

    def test_a_date_in_the_past_means_retry_now(self) -> None:
        assert parse_retry_after("Thu, 19 Nov 2026 09:00:00 GMT", now=NOW) == 0.0

    @pytest.mark.parametrize("raw", [None, "", "soon", "nan", "inf", "-inf", "12 seconds"])
    def test_unusable_values_are_ignored_rather_than_trusted(self, raw: str | None) -> None:
        assert parse_retry_after(raw, now=NOW) is None


class TestErrorFromStatus:
    def test_429_carries_the_retry_after(self) -> None:
        err = error_from_status(429, "slow down", retry_after=12)
        assert isinstance(err, RateLimitedError)
        assert err.retry_after == 12

    def test_401_is_an_auth_error(self) -> None:
        assert isinstance(error_from_status(401, "bad key"), AuthError)

    def test_403_keeps_the_classified_reason(self) -> None:
        err = error_from_status(403, "nope", forbidden_reason=ForbiddenReason.ACCOUNT_RESTRICTED)
        assert isinstance(err, PermissionDeniedError)
        assert err.reason == ForbiddenReason.ACCOUNT_RESTRICTED
        assert error_from_status(403, "nope").reason == ForbiddenReason.UNKNOWN  # type: ignore[attr-defined]

    @pytest.mark.parametrize("status", [408, 425, 500, 502, 503, 504])
    def test_server_side_and_timeout_statuses_are_transient(self, status: int) -> None:
        assert isinstance(error_from_status(status, "x"), TransientProviderError)

    @pytest.mark.parametrize("status", [400, 404, 422])
    def test_client_errors_are_permanent_so_they_are_not_retried(self, status: int) -> None:
        assert isinstance(error_from_status(status, "x"), PermanentProviderError)


class TestProviderSpec:
    def test_keys_must_be_simple_lowercase_identifiers(self) -> None:
        for bad in ("Groq", "my provider", ""):
            with pytest.raises(ValueError, match="lowercase"):
                ProviderSpec(bad, Capability.LLM, "x")

    def test_a_sending_provider_can_never_be_a_failover_target_for_limits(self) -> None:
        with pytest.raises(ValueError, match="fail_over_on_limits"):
            ProviderSpec("gmail", Capability.EMAIL_SEND, "Gmail")
        ok = ProviderSpec("gmail", Capability.EMAIL_SEND, "Gmail", fail_over_on_limits=False)
        assert not ok.fail_over_on_limits

    def test_free_tier_and_zero_cost_are_the_defaults(self) -> None:
        spec = ProviderSpec("groq", Capability.LLM, "Groq")
        assert spec.free_tier
        assert spec.estimated_cost_per_unit == Decimal(0)
        assert not spec.may_receive_personal_data, "personal data is opt-in per provider"
