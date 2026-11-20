"""ZeroBounce and Abstract (Email Reputation), against the responses observed on the real services on 2026-11-20."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.providers.adapters.abstract import AbstractEmailProvider
from app.providers.adapters.zerobounce import ZeroBounceProvider
from app.providers.errors import (
    AuthError,
    InvalidResponseError,
    PermanentProviderError,
    QuotaExhaustedError,
    RateLimitedError,
)
from tests.unit.test_discovery_adapters import CLOCK, KEY, Wire, ok

# ───────────────────────────── ZeroBounce ─────────────────────────────

ZB_OBSERVED = {
    "address": "contact@example.com",
    "status": "invalid",
    "sub_status": "does_not_accept_mail",
    "free_email": False,
    "catchall_domain": None,
    "did_you_mean": None,
    "account": "contact",
    "domain": "example.com",
    "domain_age_days": None,
    "smtp_provider": "",
    "mx_found": "false",
    "mx_record": None,
    "firstname": "Pat",
    "lastname": "Doe",
    "gender": "female",
    "country": "United States",
    "region": "Texas",
    "city": "Austin",
    "zipcode": "78701",
    "processed_at": "2026-11-20 13:11:51.186",
}


def zerobounce(*replies: httpx.Response | Exception) -> tuple[ZeroBounceProvider, Wire]:
    wire = Wire(*replies)
    return ZeroBounceProvider(api_key=KEY, client=wire.client(), clock=CLOCK), wire


class TestZeroBounce:
    async def test_the_real_observed_answer(self) -> None:
        provider, wire = zerobounce(ok(ZB_OBSERVED))
        result = await provider.verify_email("  Contact@Example.com ")
        params = wire.requests[0].url.params
        assert (wire.requests[0].url.path, params["email"], params["api_key"]) == (
            "/v2/validate",
            "contact@example.com",
            KEY.get_secret_value(),
        )
        outcome = result.value
        assert (outcome.verdict, outcome.detail, outcome.is_free_provider) == (
            "undeliverable",
            "invalid/does_not_accept_mail",
            False,
        )
        assert result.units_used == 1

    async def test_the_personal_fields_zerobounce_adds_are_never_kept(self) -> None:
        provider, _ = zerobounce(ok(ZB_OBSERVED))
        outcome = (await provider.verify_email("contact@example.com")).value
        text = repr(outcome)
        for leaked in ("Pat", "Doe", "female", "Austin", "78701", "Texas"):
            assert leaked not in text

    @pytest.mark.parametrize(
        ("status", "sub", "verdict", "flags"),
        [
            ("valid", "", "deliverable", {}),
            ("invalid", "mailbox_not_found", "undeliverable", {}),
            ("catch-all", "", "risky", {"is_catch_all": True}),
            ("do_not_mail", "role_based", "risky", {"is_role": True}),
            (
                "do_not_mail",
                "role_based_catch_all",
                "risky",
                {"is_role": True, "is_catch_all": True},
            ),
            ("do_not_mail", "disposable", "undeliverable", {"is_disposable": True}),
            ("do_not_mail", "toxic", "undeliverable", {}),
            ("do_not_mail", "global_suppression", "undeliverable", {}),
            ("spamtrap", "", "undeliverable", {}),
            ("abuse", "", "undeliverable", {}),
            ("unknown", "mailbox_quota_exceeded", "unknown", {}),
            ("a-status-we-have-never-seen", "", "unknown", {}),
        ],
    )
    async def test_status_mapping_is_conservative(
        self, status: str, sub: str, verdict: str, flags: dict[str, bool]
    ) -> None:
        provider, _ = zerobounce(ok({**ZB_OBSERVED, "status": status, "sub_status": sub}))
        outcome = (await provider.verify_email("a@example.com")).value
        assert outcome.verdict == verdict
        for name, value in flags.items():
            assert getattr(outcome, name) is value

    async def test_only_valid_is_ever_deliverable(self) -> None:
        for status in ("invalid", "catch-all", "spamtrap", "abuse", "do_not_mail", "unknown", ""):
            provider, _ = zerobounce(ok({**ZB_OBSERVED, "status": status, "sub_status": ""}))
            assert (await provider.verify_email("a@example.com")).value.verdict != "deliverable"

    async def test_an_invalid_key_arrives_as_http_200_and_the_credit_check_says_which_it_is(
        self,
    ) -> None:
        error = ok({"error": "Invalid API key or your account ran out of credits"})
        provider, wire = zerobounce(error, ok({"Credits": "-1"}))
        with pytest.raises(AuthError):
            await provider.verify_email("a@example.com")
        assert [r.url.path for r in wire.requests] == ["/v2/validate", "/v2/getcredits"]

    async def test_an_empty_account_is_a_quota_not_a_bad_key(self) -> None:
        error = ok({"error": "Invalid API key or your account ran out of credits"})
        provider, _ = zerobounce(error, ok({"Credits": "0"}))
        with pytest.raises(QuotaExhaustedError):
            await provider.verify_email("a@example.com")

    async def test_an_error_while_credits_remain_is_not_blamed_on_the_key(self) -> None:
        provider, _ = zerobounce(ok({"error": "something else"}), ok({"Credits": "57"}))
        with pytest.raises(PermanentProviderError):
            await provider.verify_email("a@example.com")

    async def test_if_even_the_credit_check_fails_the_original_error_is_reported(self) -> None:
        provider, _ = zerobounce(ok({"error": "boom"}), httpx.Response(503, text="down"))
        with pytest.raises(PermanentProviderError, match="refused"):
            await provider.verify_email("a@example.com")

    async def test_credits_are_read_as_a_number(self) -> None:
        provider, _ = zerobounce(ok({"Credits": "100"}))
        assert await provider.credits() == 100
        provider, _ = zerobounce(ok({"Credits": "lots"}))
        with pytest.raises(InvalidResponseError):
            await provider.credits()

    @pytest.mark.parametrize("body", [{}, {"address": "a@example.com"}, []])
    async def test_an_answer_without_a_verdict_is_invalid(self, body: object) -> None:
        provider, _ = zerobounce(ok(body))
        with pytest.raises(InvalidResponseError):
            await provider.verify_email("a@example.com")

    @pytest.mark.parametrize("bad", ["", "no at sign", "a@b", "a@example.com&api_key=evil"])
    async def test_only_a_clean_address_is_sent(self, bad: str) -> None:
        provider, wire = zerobounce(ok(ZB_OBSERVED))
        with pytest.raises(PermanentProviderError):
            await provider.verify_email(bad)
        assert wire.requests == []

    async def test_the_key_is_never_echoed_in_an_error(self) -> None:
        provider, _ = zerobounce(
            httpx.Response(500, text=f"upstream failure for {KEY.get_secret_value()}")
        )
        with pytest.raises(Exception) as raised:  # noqa: PT011 - any provider error type is fine; the key must not leak
            await provider.verify_email("a@example.com")
        assert KEY.get_secret_value() not in str(raised.value)

    def test_declared_facts(self) -> None:
        provider, _ = zerobounce(ok({}))
        assert (
            provider.spec.key,
            provider.spec.monthly_limit,
            provider.spec.may_receive_personal_data,
        ) == ("zerobounce", 100, True)
        assert provider.configured is True
        assert (
            ZeroBounceProvider(api_key=None, client=Wire(ok({})).client(), clock=CLOCK).configured
            is False
        )


# ───────────────────────────── Abstract (Email Reputation) ─────────────────────────────

AB_OBSERVED: dict[str, Any] = {
    "email_address": "contact@example.com",
    "suggested_correction": None,
    "email_deliverability": {
        "status": "undeliverable",
        "status_detail": "dns_record_not_found",
        "is_format_valid": True,
        "is_smtp_valid": False,
        "is_mx_valid": False,
        "mx_records": None,
    },
    "email_sender": {
        "first_name": "Contact",
        "last_name": None,
        "organization_name": "Example Domain",
        "organization_type": "commercial",
    },
    "email_domain": {
        "domain": "example.com",
        "domain_age": 11421,
        "is_live_site": True,
        "registrar": "RESERVED",
    },
    "email_quality": {
        "score": 0.0,
        "is_free_email": False,
        "is_username_suspicious": False,
        "is_disposable": False,
        "is_catchall": False,
        "is_subaddress": False,
        "is_role": True,
    },
    "email_risk": {"address_risk_status": "high", "domain_risk_status": "low"},
    "email_breaches": {
        "total_breaches": 26,
        "breached_domains": [{"domain": "leaked-site.example", "breach_date": "2016-09-10"}],
    },
}


def abstract(*replies: httpx.Response | Exception) -> tuple[AbstractEmailProvider, Wire]:
    wire = Wire(*replies)
    return AbstractEmailProvider(api_key=KEY, client=wire.client(), clock=CLOCK), wire


def with_deliverability(**changes: Any) -> dict[str, Any]:
    body = {**AB_OBSERVED}
    body["email_deliverability"] = {
        **AB_OBSERVED["email_deliverability"],
        **{k: v for k, v in changes.items() if k in AB_OBSERVED["email_deliverability"]},
    }
    body["email_quality"] = {
        **AB_OBSERVED["email_quality"],
        **{k: v for k, v in changes.items() if k in AB_OBSERVED["email_quality"]},
    }
    return body


class TestAbstract:
    async def test_it_talks_to_the_email_reputation_api_not_the_old_validation_host(self) -> None:
        provider, wire = abstract(ok(AB_OBSERVED))
        await provider.verify_email("contact@example.com")
        url = wire.requests[0].url
        assert url.host == "emailreputation.abstractapi.com"
        assert (url.params["email"], url.params["api_key"]) == (
            "contact@example.com",
            KEY.get_secret_value(),
        )

    async def test_the_real_observed_answer(self) -> None:
        provider, _ = abstract(ok(AB_OBSERVED))
        result = await provider.verify_email("Contact@Example.com")
        outcome = result.value
        assert (outcome.verdict, outcome.detail) == ("undeliverable", "dns_record_not_found")
        assert (
            outcome.is_role,
            outcome.is_disposable,
            outcome.is_catch_all,
            outcome.is_free_provider,
        ) == (True, False, False, False)
        assert result.units_used == 1

    async def test_breach_history_and_sender_details_are_never_kept(self) -> None:
        provider, _ = abstract(ok(AB_OBSERVED))
        text = repr((await provider.verify_email("contact@example.com")).value)
        for dropped in ("breach", "leaked-site", "Example Domain", "Contact"):
            assert dropped not in text

    @pytest.mark.parametrize(
        ("changes", "verdict"),
        [
            ({"status": "deliverable"}, "deliverable"),
            ({"status": "deliverable", "is_catchall": True}, "risky"),
            ({"status": "deliverable", "is_disposable": True}, "risky"),
            ({"status": "risky"}, "risky"),
            ({"status": "undeliverable"}, "undeliverable"),
            ({"status": "unknown"}, "unknown"),
            ({"status": "something-new"}, "unknown"),
        ],
    )
    async def test_verdicts_are_conservative(self, changes: dict[str, Any], verdict: str) -> None:
        provider, _ = abstract(ok(with_deliverability(**changes)))
        assert (await provider.verify_email("a@example.com")).value.verdict == verdict

    async def test_a_malformed_address_answered_with_200_is_undeliverable(self) -> None:
        body = with_deliverability(
            status="undeliverable", status_detail="Invalid format", is_format_valid=False
        )
        provider, _ = abstract(ok(body))
        outcome = (await provider.verify_email("a@example.com")).value
        assert (outcome.verdict, outcome.detail) == ("undeliverable", "Invalid format")

    @pytest.mark.parametrize(
        "body", [{}, {"email_deliverability": {}}, {"email_deliverability": "x"}, []]
    )
    async def test_no_verdict_is_an_invalid_response(self, body: object) -> None:
        provider, _ = abstract(ok(body))
        with pytest.raises(InvalidResponseError):
            await provider.verify_email("a@example.com")

    async def test_the_observed_401_is_an_auth_error(self) -> None:
        reply = httpx.Response(
            401,
            json={
                "error": {
                    "message": "Invalid API key provided.",
                    "code": "unauthorized",
                    "details": None,
                }
            },
        )
        provider, _ = abstract(reply)
        with pytest.raises(AuthError):
            await provider.verify_email("a@example.com")

    async def test_quota_and_rate_limits(self) -> None:
        provider, _ = abstract(
            httpx.Response(
                422, json={"error": {"code": "quota_reached", "message": "Monthly quota reached"}}
            )
        )
        with pytest.raises(QuotaExhaustedError):
            await provider.verify_email("a@example.com")
        provider, _ = abstract(httpx.Response(429, headers={"retry-after": "3"}, text="slow"))
        with pytest.raises(RateLimitedError):
            await provider.verify_email("a@example.com")

    @pytest.mark.parametrize("bad", ["", "no at sign", "a@b", "a@example.com&api_key=evil"])
    async def test_only_a_clean_address_is_sent(self, bad: str) -> None:
        provider, wire = abstract(ok(AB_OBSERVED))
        with pytest.raises(PermanentProviderError):
            await provider.verify_email(bad)
        assert wire.requests == []

    def test_declared_facts(self) -> None:
        provider, _ = abstract(ok({}))
        assert (
            provider.spec.key,
            provider.spec.monthly_limit,
            provider.spec.may_receive_personal_data,
        ) == ("abstract", 100, True)
        assert (
            AbstractEmailProvider(
                api_key=None, client=Wire(ok({})).client(), clock=CLOCK
            ).configured
            is False
        )
