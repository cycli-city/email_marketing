"""The discovery adapters, against the response and ERROR shapes observed on the real services (2026-11-20), with fictional data.

Each adapter: builds the right request (credentials where the vendor wants them), parses the observed shape, treats vendor
data as untrusted, translates every failure into the provider taxonomy, and never lets a key reach an error message.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.clock import FakeClock
from app.domain.enums import Capability
from app.providers.adapters.geoapify import CATEGORY_MAP, GeoapifyProvider
from app.providers.adapters.hunter import HunterProvider
from app.providers.adapters.mailboxvalidator import MailboxValidatorProvider
from app.providers.adapters.search import BraveSearchProvider, TavilyProvider
from app.providers.adapters.verifalia import VerifaliaProvider
from app.providers.discovery import PlaceQuery
from app.providers.errors import (
    AuthError,
    InvalidResponseError,
    PermanentProviderError,
    QuotaExhaustedError,
    RateLimitedError,
    TransientProviderError,
)

CLOCK = FakeClock(datetime(2026, 11, 20, 9, 0, tzinfo=UTC))
KEY = SecretStr(
    "unit-test-" + "a" * 24
)  # obviously fake: long enough to be scrubbed, too plain to look like a key


class Wire:
    """A canned HTTP server: scripted replies in order, every request remembered."""

    def __init__(
        self, *replies: httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]
    ) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply(request) if callable(reply) else reply

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def ok(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


# ───────────────────────────── Geoapify ─────────────────────────────

GEOAPIFY_OK = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "name": "Example Dental",
                "country_code": "us",
                "city": "Austin",
                "lon": -97.730394,
                "lat": 30.245304,
                "address_line1": "Example Dental",
                "address_line2": "1 Example Street, Austin, TX 78741, United States of America",
                "categories": ["healthcare", "healthcare.dentist", "wheelchair"],
                "website": "https://dental.example.com/",
                "opening_hours": "Mo 08:00-17:00",
                "contact": {"phone": "+1-512-555-0100", "email": "front.desk@dental.example.com"},
                "place_id": "place-abc-123",
            },
            "geometry": {"type": "Point", "coordinates": [-97.730394, 30.245304]},
        },
        {"type": "Feature", "properties": {"name": "  ", "lat": 1, "lon": 2}},  # unnamed: skipped
        {
            "type": "Feature",
            "properties": {"name": "Plain Clinic", "city": "Austin"},
        },  # sparse: fine
    ],
}


def geoapify(*replies: httpx.Response | Exception) -> tuple[GeoapifyProvider, Wire]:
    wire = Wire(*replies)
    return GeoapifyProvider(api_key=KEY, client=wire.client(), clock=CLOCK), wire


class TestGeoapify:
    async def test_asks_for_businesses_around_a_point_and_parses_the_observed_shape(self) -> None:
        provider, wire = geoapify(ok(GEOAPIFY_OK))
        result = await provider.search_places(
            PlaceQuery(("dentist",), 30.2672, -97.7431, radius_m=4000, limit=20)
        )

        params = wire.requests[0].url.params
        assert params["categories"] == CATEGORY_MAP["dentist"] == "healthcare.dentist"
        assert params["filter"] == "circle:-97.743100,30.267200,4000"
        assert (params["limit"], params["conditions"], params["apiKey"]) == (
            "20",
            "named",
            KEY.get_secret_value(),
        )
        first, second = result.value
        assert (first.name, first.website, first.phone) == (
            "Example Dental",
            "https://dental.example.com/",
            "+1-512-555-0100",
        )
        assert first.email == "front.desk@dental.example.com"
        assert (first.city, first.country_code, first.source_id) == (
            "Austin",
            "us",
            "place-abc-123",
        )
        assert first.categories == ("healthcare", "healthcare.dentist", "wheelchair")
        assert (first.latitude, first.longitude) == (30.245304, -97.730394)
        assert (second.name, second.website, second.email, second.source_id) == (
            "Plain Clinic",
            None,
            None,
            "",
        )
        assert len(result.value) == 2  # the unnamed feature was dropped
        assert result.units_used == Decimal(1)

    async def test_the_limit_is_capped(self) -> None:
        provider, wire = geoapify(ok({"features": []}))
        await provider.search_places(PlaceQuery(("dentist",), 0, 0, limit=5000))
        assert wire.requests[0].url.params["limit"] == "100"

    @pytest.mark.parametrize(
        ("published", "expected"),
        [
            ("mailto:info@dental.example.com", "info@dental.example.com"),
            ("Info@Dental.Example.com;sales@dental.example.com", "info@dental.example.com"),
            ("not an email", None),
            ("javascript:alert(1)", None),
            ("", None),
            (42, None),
        ],
    )
    async def test_a_published_email_is_normalised_or_dropped(
        self, published: object, expected: str | None
    ) -> None:
        body = {"features": [{"properties": {"name": "X", "contact": {"email": published}}}]}
        provider, _ = geoapify(ok(body))
        assert (await provider.search_places(PlaceQuery(("dentist",), 0, 0))).value[
            0
        ].email == expected

    @pytest.mark.parametrize(
        "bad", ["healthcare.dentist,office", "x&apiKey=evil", "a b", "../etc", "UPPER.case", ""]
    )
    async def test_a_hostile_category_never_reaches_the_request(self, bad: str) -> None:
        provider, wire = geoapify(ok({"features": []}))
        with pytest.raises(PermanentProviderError):
            await provider.search_places(PlaceQuery((bad,), 0, 0))
        assert wire.requests == []

    async def test_a_vendor_category_name_passes_through(self) -> None:
        provider, wire = geoapify(ok({"features": []}))
        await provider.search_places(PlaceQuery(("healthcare.hospital", "catering.cafe"), 0, 0))
        assert wire.requests[0].url.params["categories"] == "healthcare.hospital,catering.cafe"

    @pytest.mark.parametrize("body", [{}, {"features": "oops"}, [], "nope"])
    async def test_an_unusable_answer_is_reported_not_guessed_at(self, body: object) -> None:
        provider, _ = geoapify(ok(body))
        with pytest.raises(InvalidResponseError):
            await provider.search_places(PlaceQuery(("dentist",), 0, 0))

    async def test_an_invalid_key_is_an_auth_error_and_the_key_is_never_echoed(self) -> None:
        reply = httpx.Response(
            401,
            json={
                "statusCode": 401,
                "error": "Unauthorized",
                "message": f"Invalid apiKey {KEY.get_secret_value()}",
            },
        )
        provider, _ = geoapify(reply)
        with pytest.raises(AuthError) as raised:
            await provider.search_places(PlaceQuery(("dentist",), 0, 0))
        assert KEY.get_secret_value() not in str(raised.value)
        assert raised.value.provider_key == "geoapify"

    async def test_rate_limits_and_outages_use_the_right_error_types(self) -> None:
        provider, _ = geoapify(httpx.Response(429, headers={"retry-after": "7"}, text="slow down"))
        with pytest.raises(RateLimitedError) as limited:
            await provider.search_places(PlaceQuery(("dentist",), 0, 0))
        assert limited.value.retry_after == 7
        provider, _ = geoapify(httpx.Response(503, text="down"))
        with pytest.raises(TransientProviderError):
            await provider.search_places(PlaceQuery(("dentist",), 0, 0))

    async def test_a_connection_that_never_opened_is_safe_to_retry(self) -> None:
        provider, _ = geoapify(httpx.ConnectError("refused"))
        with pytest.raises(TransientProviderError) as raised:
            await provider.search_places(PlaceQuery(("dentist",), 0, 0))
        assert raised.value.request_may_have_been_processed is False

    async def test_without_a_key_it_says_so_and_calls_nothing(self) -> None:
        wire = Wire(ok({}))
        provider = GeoapifyProvider(api_key=None, client=wire.client(), clock=CLOCK)
        assert provider.configured is False
        with pytest.raises(AuthError):
            await provider.search_places(PlaceQuery(("dentist",), 0, 0))
        assert wire.requests == []


# ───────────────────────────── Hunter ─────────────────────────────

HUNTER_OK = {
    "data": {
        "domain": "example.com",
        "disposable": False,
        "webmail": False,
        "accept_all": True,
        "pattern": "{first}{last}",
        "organization": "Example Co",
        "linked_domains": [],
        "emails": [
            {
                "value": "Pat.Doe@example.com",
                "type": "personal",
                "confidence": 85,
                "first_name": "Pat",
                "last_name": "Doe",
                "position": "Head of Growth",
                "position_raw": "Head of Growth",
                "seniority": "senior",
                "department": "marketing",
                "linkedin": "https://www.linkedin.com/in/pat-doe",
                "twitter": None,
                "phone_number": None,
                "sources": [
                    {
                        "domain": "example.com",
                        "uri": "https://example.com/team",
                        "extracted_on": "2025-01-01",
                    },
                    {"domain": "x", "uri": "javascript:alert(1)"},
                ],
                "verification": {"date": "2026-11-19", "status": "valid"},
            },
            {
                "value": "info@example.com",
                "type": "generic",
                "confidence": 90,
                "sources": [],
                "verification": {"status": "unknown"},
            },
            {"value": "not-an-email", "type": "personal"},
        ],
    },
    "meta": {"results": 4420, "limit": 10, "offset": 0},
}


def hunter(*replies: httpx.Response | Exception) -> tuple[HunterProvider, Wire]:
    wire = Wire(*replies)
    return HunterProvider(api_key=KEY, client=wire.client(), clock=CLOCK), wire


class TestHunter:
    async def test_the_key_travels_in_a_header_never_in_the_url(self) -> None:
        provider, wire = hunter(ok(HUNTER_OK, headers={"x-credits-used": "1"}))
        await provider.domain_contacts("example.com", limit=50)
        request = wire.requests[0]
        assert request.headers["x-api-key"] == KEY.get_secret_value()
        assert KEY.get_secret_value() not in str(request.url)
        assert request.url.params["domain"] == "example.com"
        assert request.url.params["limit"] == "10"  # capped at what the free plan returns

    async def test_parses_the_observed_shape(self) -> None:
        provider, _ = hunter(ok(HUNTER_OK, headers={"x-credits-used": "1"}))
        result = await provider.domain_contacts("example.com")
        found = result.value
        assert (found.domain, found.organization, found.pattern, found.accept_all) == (
            "example.com",
            "Example Co",
            "{first}{last}",
            True,
        )
        person, role = found.contacts  # the malformed address was dropped
        assert (person.email, person.type, person.first_name, person.last_name) == (
            "pat.doe@example.com",
            "personal",
            "Pat",
            "Doe",
        )
        assert (person.position, person.department, person.seniority, person.confidence) == (
            "Head of Growth",
            "marketing",
            "senior",
            85,
        )
        assert person.source_urls == (
            "https://example.com/team",
        )  # the javascript: "source" was dropped
        assert person.vendor_verified is True
        assert (role.email, role.type, role.vendor_verified) == (
            "info@example.com",
            "generic",
            False,
        )
        assert result.units_used == Decimal(1)

    async def test_social_profiles_and_phone_numbers_are_not_carried_over(self) -> None:
        provider, _ = hunter(ok(HUNTER_OK))
        contact = (await provider.domain_contacts("example.com")).value.contacts[0]
        assert not hasattr(contact, "linkedin")
        assert not hasattr(contact, "phone_number")

    @pytest.mark.parametrize(
        "domain",
        [
            "example.com/path",
            "a b.com",
            "http://example.com",
            "example",
            "x@example.net",
            "",
            "exa mple.org",
        ],
    )
    async def test_only_a_plain_domain_is_ever_sent(self, domain: str) -> None:
        provider, wire = hunter(ok(HUNTER_OK))
        with pytest.raises(PermanentProviderError):
            await provider.domain_contacts(domain)
        assert wire.requests == []

    async def test_the_observed_auth_failure_shape(self) -> None:
        reply = httpx.Response(
            401,
            json={
                "errors": [
                    {
                        "id": "authentication_failed",
                        "code": 401,
                        "details": "No user found for the API key supplied",
                    }
                ]
            },
        )
        provider, _ = hunter(reply)
        with pytest.raises(AuthError):
            await provider.domain_contacts("example.com")

    @pytest.mark.parametrize("status", [402, 403, 429])
    async def test_running_out_of_credits_is_a_quota_not_a_retry(self, status: int) -> None:
        reply = httpx.Response(
            status, json={"errors": [{"id": "usage_limit_reached", "details": "monthly"}]}
        )
        provider, _ = hunter(reply)
        with pytest.raises(QuotaExhaustedError):
            await provider.domain_contacts("example.com")

    async def test_a_missing_data_object_is_an_invalid_response(self) -> None:
        provider, _ = hunter(ok({"meta": {}}))
        with pytest.raises(InvalidResponseError):
            await provider.domain_contacts("example.com")


# ───────────────────────────── Verifalia ─────────────────────────────

VERIFALIA_SUBMITTED = {"overview": {"id": "job-1", "status": "Completed", "noOfEntries": 1}}
VERIFALIA_ENTRIES = {
    "meta": {"cursor": "x"},
    "data": [
        {
            "index": 0,
            "inputData": "contact@example.com",
            "emailAddress": "contact@example.com",
            "isRoleAccount": True,
            "isFreeEmailAddress": False,
            "status": "DomainHasNullMx",
            "classification": "Undeliverable",
            "suggestions": ["contact@example.net"],
        }
    ],
}


def verifalia(
    *replies: httpx.Response | Exception, sleeps: list[float] | None = None
) -> tuple[VerifaliaProvider, Wire]:
    wire = Wire(*replies)
    spent = sleeps if sleeps is not None else []

    async def nap(seconds: float) -> None:
        spent.append(seconds)

    provider = VerifaliaProvider(
        username=SecretStr("user-1234"),
        password=SecretStr("pass-5678"),
        client=wire.client(),
        clock=CLOCK,
        sleep=nap,
    )
    return provider, wire


class TestVerifalia:
    async def test_one_address_one_job_then_the_verdict(self) -> None:
        provider, wire = verifalia(ok(VERIFALIA_SUBMITTED), ok(VERIFALIA_ENTRIES))
        result = await provider.verify_email("  Contact@Example.com ")
        submit, entries = wire.requests
        assert (submit.method, submit.url.path) == ("POST", "/v2.6/email-validations")
        assert (
            submit.read()
            == b'{"entries":[{"inputData":"contact@example.com"}],"quality":"Standard"}'
        )
        assert submit.headers["authorization"].startswith("Basic ")
        assert entries.url.path == "/v2.6/email-validations/job-1/entries"
        outcome = result.value
        assert (outcome.email, outcome.verdict, outcome.detail) == (
            "contact@example.com",
            "undeliverable",
            "DomainHasNullMx",
        )
        assert (outcome.is_role, outcome.is_free_provider) == (True, False)
        assert result.units_used == Decimal(1)

    @pytest.mark.parametrize(
        ("classification", "verdict"),
        [
            ("Deliverable", "deliverable"),
            ("Risky", "risky"),
            ("Undeliverable", "undeliverable"),
            ("Unknown", "unknown"),
            ("Brand New Thing", "unknown"),
        ],
    )
    async def test_classification_maps_to_a_verdict_and_anything_unknown_is_unknown(
        self, classification: str, verdict: str
    ) -> None:
        entries = {"data": [{"classification": classification, "status": "Success"}]}
        provider, _ = verifalia(ok(VERIFALIA_SUBMITTED), ok(entries))
        assert (await provider.verify_email("a@example.com")).value.verdict == verdict

    async def test_a_job_still_running_is_polled_until_complete(self) -> None:
        running = {"overview": {"id": "job-1", "status": "InProgress"}}
        sleeps: list[float] = []
        provider, wire = verifalia(
            ok(running, status=202),
            ok(running),
            ok(VERIFALIA_SUBMITTED),
            ok(VERIFALIA_ENTRIES),
            sleeps=sleeps,
        )
        result = await provider.verify_email("a@example.com")
        assert result.value.verdict == "undeliverable"
        assert [r.url.path for r in wire.requests][1:3] == ["/v2.6/email-validations/job-1"] * 2
        assert len(sleeps) == 2

    async def test_a_job_that_never_finishes_is_a_transient_failure(self) -> None:
        running = {"overview": {"id": "job-1", "status": "InProgress"}}
        provider, _ = verifalia(ok(running, status=202), ok(running))
        with pytest.raises(TransientProviderError, match="in time"):
            await provider.verify_email("a@example.com")

    @pytest.mark.parametrize(
        "bad", ["not an address", "a@b", "x@example.net\r\nBcc: z@example.net", "", "a@@b.com"]
    )
    async def test_only_a_clean_address_is_sent_to_the_vendor(self, bad: str) -> None:
        provider, wire = verifalia(ok(VERIFALIA_SUBMITTED))
        with pytest.raises(PermanentProviderError):
            await provider.verify_email(bad)
        assert wire.requests == []

    async def test_the_observed_401_is_an_auth_error_and_credentials_are_not_echoed(self) -> None:
        reply = httpx.Response(
            401,
            headers={"WWW-Authenticate": 'Basic realm="Verifalia"'},
            json={
                "type": "/problems/unauthorized",
                "detail": "invalid credentials user-1234 pass-5678",
            },
        )
        provider, _ = verifalia(reply)
        with pytest.raises(AuthError) as raised:
            await provider.verify_email("a@example.com")
        assert "pass-5678" not in str(raised.value)
        assert "user-1234" not in str(raised.value)

    async def test_out_of_credits_is_a_quota_error(self) -> None:
        provider, _ = verifalia(httpx.Response(402, json={"detail": "Insufficient credits"}))
        with pytest.raises(QuotaExhaustedError):
            await provider.verify_email("a@example.com")

    @pytest.mark.parametrize("entries", [{"data": []}, {}, {"data": "x"}])
    async def test_no_verdict_is_an_invalid_response(self, entries: object) -> None:
        provider, _ = verifalia(ok(VERIFALIA_SUBMITTED), ok(entries))
        with pytest.raises(InvalidResponseError):
            await provider.verify_email("a@example.com")

    async def test_needs_both_halves_of_the_credentials(self) -> None:
        wire = Wire(ok({}))
        provider = VerifaliaProvider(
            username=SecretStr("u"), password=None, client=wire.client(), clock=CLOCK
        )
        assert provider.configured is False
        with pytest.raises(AuthError):
            await provider.verify_email("a@example.com")

    def test_it_is_cleared_for_addresses_only_by_design(self) -> None:
        provider, _ = verifalia(ok({}))
        assert provider.spec.may_receive_personal_data is True
        assert (provider.spec.capability, provider.spec.daily_limit) == (
            Capability.EMAIL_VERIFICATION,
            25,
        )


# ───────────────────────────── MailboxValidator ─────────────────────────────

MBV_OBSERVED = {
    "email_address": "contact@example.com",
    "base_email_address": "contact@example.com",
    "domain": "example.com",
    "is_free": False,
    "is_syntax": True,
    "is_domain": True,
    "is_smtp": False,
    "is_verified": None,
    "is_server_down": None,
    "is_greylisted": None,
    "is_disposable": False,
    "is_suppressed": False,
    "is_role": True,
    "is_high_risk": False,
}


def mbv(*replies: httpx.Response | Exception) -> tuple[MailboxValidatorProvider, Wire]:
    wire = Wire(*replies)
    return MailboxValidatorProvider(api_key=KEY, client=wire.client(), clock=CLOCK), wire


class TestMailboxValidator:
    async def test_the_real_observed_answer_is_unknown_not_deliverable(self) -> None:
        provider, wire = mbv(ok(MBV_OBSERVED))
        result = await provider.verify_email("Contact@Example.com")
        outcome = result.value
        assert (outcome.verdict, outcome.detail, outcome.is_role, outcome.is_disposable) == (
            "unknown",
            "could_not_verify",
            True,
            False,
        )
        params = wire.requests[0].url.params
        assert (params["email"], params["key"]) == ("contact@example.com", KEY.get_secret_value())

    @pytest.mark.parametrize(
        ("overrides", "verdict", "detail"),
        [
            ({"is_syntax": False}, "undeliverable", "invalid_syntax_or_domain"),
            ({"is_domain": False}, "undeliverable", "invalid_syntax_or_domain"),
            ({"is_disposable": True, "is_verified": True}, "risky", "disposable"),
            ({"is_verified": True, "is_smtp": True}, "deliverable", "verified"),
            ({"is_verified": False}, "undeliverable", "mailbox_not_found"),
            ({"is_verified": None, "is_greylisted": True}, "unknown", "could_not_verify"),
            ({"is_verified": None, "is_server_down": True}, "unknown", "could_not_verify"),
        ],
    )
    async def test_verdict_rules_are_conservative(
        self, overrides: dict[str, Any], verdict: str, detail: str
    ) -> None:
        provider, _ = mbv(ok({**MBV_OBSERVED, **overrides}))
        outcome = (await provider.verify_email("a@example.com")).value
        assert (outcome.verdict, outcome.detail) == (verdict, detail)

    async def test_the_observed_bad_key_answer(self) -> None:
        reply = httpx.Response(
            401,
            json={
                "error": {
                    "error_code": 10001,
                    "error_message": f"API key not found. {KEY.get_secret_value()}",
                }
            },
        )
        provider, _ = mbv(reply)
        with pytest.raises(AuthError) as raised:
            await provider.verify_email("a@example.com")
        assert KEY.get_secret_value() not in str(raised.value)

    async def test_an_error_object_inside_a_200_is_never_mistaken_for_a_verdict(self) -> None:
        provider, _ = mbv(
            ok({"error": {"error_code": 10005, "error_message": "insufficient credits"}})
        )
        with pytest.raises(QuotaExhaustedError):
            await provider.verify_email("a@example.com")

    async def test_an_answer_without_a_verdict_is_invalid(self) -> None:
        provider, _ = mbv(ok({"email_address": "a@example.com"}))
        with pytest.raises(InvalidResponseError):
            await provider.verify_email("a@example.com")

    @pytest.mark.parametrize("bad", ["", "no at sign", "a@b", "a@example.com&key=evil"])
    async def test_only_a_clean_address_is_sent(self, bad: str) -> None:
        provider, wire = mbv(ok(MBV_OBSERVED))
        with pytest.raises(PermanentProviderError):
            await provider.verify_email(bad)
        assert wire.requests == []


# ───────────────────────────── Tavily and Brave ─────────────────────────────

TAVILY_OK = {
    "query": "dental clinic Austin",
    "answer": None,
    "images": [],
    "results": [
        {
            "url": "https://clinic.example.com/",
            "title": "Example Clinic",
            "content": "Drs. Example welcome you",
            "score": 0.76,
            "raw_content": None,
        },
        {"url": "javascript:alert(1)", "title": "x", "content": "y"},
        {"url": "https://other.example.org/about", "title": "", "content": ""},
    ],
    "response_time": 1.4,
}
BRAVE_OK = {
    "type": "search",
    "query": {"original": "dental clinic austin"},
    "web": {
        "type": "search",
        "results": [
            {
                "title": "Example Clinic",
                "url": "https://clinic.example.com/",
                "description": "Welcome",
            },
            {"title": "No scheme", "url": "ftp://files.example.com/", "description": "x"},
        ],
    },
}


class TestSearch:
    async def test_tavily_request_and_parsing(self) -> None:
        wire = Wire(ok(TAVILY_OK))
        tavily = TavilyProvider(api_key=KEY, client=wire.client(), clock=CLOCK)
        result = await tavily.web_search("  dental   clinic Austin \n", limit=50)
        request = wire.requests[0]
        assert request.headers["authorization"] == f"Bearer {KEY.get_secret_value()}"
        assert KEY.get_secret_value() not in str(request.url)
        assert (
            request.read()
            == b'{"query":"dental clinic Austin","max_results":10,"include_answer":false}'
        )
        first, second = result.value  # the javascript: URL is dropped
        assert (first.title, first.url, first.snippet) == (
            "Example Clinic",
            "https://clinic.example.com/",
            "Drs. Example welcome you",
        )
        assert (second.title, second.url) == (
            "https://other.example.org/about",
            "https://other.example.org/about",
        )  # no title: the URL stands in
        assert result.units_used == Decimal(1)

    async def test_tavily_observed_bad_key(self) -> None:
        wire = Wire(
            httpx.Response(
                401, json={"detail": {"error": "Unauthorized: missing or invalid API key."}}
            )
        )
        with pytest.raises(AuthError):
            await TavilyProvider(api_key=KEY, client=wire.client(), clock=CLOCK).web_search("x")

    async def test_brave_request_and_parsing(self) -> None:
        wire = Wire(ok(BRAVE_OK))
        brave = BraveSearchProvider(api_key=KEY, client=wire.client(), clock=CLOCK)
        result = await brave.web_search("dental clinic austin", limit=3)
        request = wire.requests[0]
        assert request.headers["x-subscription-token"] == KEY.get_secret_value()
        assert (request.url.params["q"], request.url.params["count"]) == (
            "dental clinic austin",
            "3",
        )
        assert [(h.title, h.url) for h in result.value] == [
            ("Example Clinic", "https://clinic.example.com/")
        ]  # ftp: dropped

    async def test_brave_reports_an_invalid_key_as_http_422_and_that_is_an_auth_problem(
        self,
    ) -> None:
        """Observed on the real service: a bad subscription token is 422, not 401. A default mapping would call it a bad
        request and keep retrying a dead key."""
        reply = httpx.Response(
            422,
            json={
                "error": {
                    "code": "SUBSCRIPTION_TOKEN_INVALID",
                    "detail": "The provided subscription token is invalid.",
                    "status": 422,
                },
                "type": "ErrorResponse",
            },
        )
        wire = Wire(reply)
        with pytest.raises(AuthError):
            await BraveSearchProvider(api_key=KEY, client=wire.client(), clock=CLOCK).web_search(
                "x"
            )

    async def test_brave_with_no_results_is_an_empty_list(self) -> None:
        wire = Wire(ok({"type": "search", "query": {}}))
        assert (
            await BraveSearchProvider(api_key=KEY, client=wire.client(), clock=CLOCK).web_search(
                "x"
            )
        ).value == []

    async def test_brave_results_are_marked_use_but_do_not_store(self) -> None:
        provider = BraveSearchProvider(api_key=KEY, client=Wire(ok({})).client(), clock=CLOCK)
        assert provider.spec.tier == "transient"
        assert provider.spec.may_receive_personal_data is False

    @pytest.mark.parametrize("make", [TavilyProvider, BraveSearchProvider])
    async def test_an_empty_query_is_never_sent(self, make: Any) -> None:
        wire = Wire(ok({}))
        with pytest.raises(PermanentProviderError):
            await make(api_key=KEY, client=wire.client(), clock=CLOCK).web_search("   \n ")
        assert wire.requests == []

    @pytest.mark.parametrize("make", [TavilyProvider, BraveSearchProvider])
    async def test_a_very_long_query_is_cut_not_sent_whole(self, make: Any) -> None:
        wire = Wire(ok(TAVILY_OK if make is TavilyProvider else BRAVE_OK))
        await make(api_key=KEY, client=wire.client(), clock=CLOCK).web_search("word " * 500)
        sent = wire.requests[0]
        query = (
            sent.url.params["q"]
            if make is BraveSearchProvider
            else __import__("json").loads(sent.read())["query"]
        )
        assert len(query) <= 400


# ───────────────────────────── shared behaviour ─────────────────────────────


class TestShared:
    @pytest.mark.parametrize(
        ("provider", "key", "capability", "personal"),
        [
            (
                GeoapifyProvider(api_key=KEY, client=Wire(ok({})).client(), clock=CLOCK),
                "geoapify",
                Capability.COMPANY_DISCOVERY,
                False,
            ),
            (
                HunterProvider(api_key=KEY, client=Wire(ok({})).client(), clock=CLOCK),
                "hunter",
                Capability.CONTACT_DISCOVERY,
                False,
            ),
            (
                TavilyProvider(api_key=KEY, client=Wire(ok({})).client(), clock=CLOCK),
                "tavily",
                Capability.SEARCH,
                False,
            ),
            (
                BraveSearchProvider(api_key=KEY, client=Wire(ok({})).client(), clock=CLOCK),
                "brave",
                Capability.SEARCH,
                False,
            ),
            (
                MailboxValidatorProvider(api_key=KEY, client=Wire(ok({})).client(), clock=CLOCK),
                "mailboxvalidator",
                Capability.EMAIL_VERIFICATION,
                True,
            ),
        ],
    )
    def test_each_provider_declares_what_it_is(
        self, provider: Any, key: str, capability: Capability, personal: bool
    ) -> None:
        assert (
            provider.spec.key,
            provider.spec.capability,
            provider.spec.may_receive_personal_data,
        ) == (key, capability, personal)
        assert provider.spec.free_tier is True
        assert provider.configured is True

    def test_every_provider_without_a_key_is_not_configured(self) -> None:
        for cls in (
            GeoapifyProvider,
            HunterProvider,
            TavilyProvider,
            BraveSearchProvider,
            MailboxValidatorProvider,
        ):
            assert cls(api_key=None, client=Wire(ok({})).client(), clock=CLOCK).configured is False
            assert (
                cls(api_key=SecretStr(""), client=Wire(ok({})).client(), clock=CLOCK).configured
                is False
            )

    async def test_a_non_json_answer_is_an_invalid_response_not_a_crash(self) -> None:
        wire = Wire(httpx.Response(200, text="<html>maintenance</html>"))
        with pytest.raises(InvalidResponseError):
            await HunterProvider(api_key=KEY, client=wire.client(), clock=CLOCK).domain_contacts(
                "example.com"
            )

    async def test_providers_only_speak_https(self) -> None:
        provider = GeoapifyProvider(api_key=KEY, client=Wire(ok({})).client(), clock=CLOCK)
        with pytest.raises(ValueError, match="HTTPS"):
            await provider.request("GET", "http://api.geoapify.com/v2/places")
