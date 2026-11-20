"""Opt-in checks of the discovery adapters against the REAL services (a handful of free-tier credits in total).

    RUN_LIVE=1 GEOAPIFY_API_KEY=… HUNTER_API_KEY=… … pytest -m live tests/live/test_discovery_live.py

Never part of CI. Keys come from the environment only (load them from `.local/SECRETS.local.md` into the process, never
into a file), and a test is skipped when its key is absent.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from app.core.clock import FakeClock
from app.providers.adapters.abstract import AbstractEmailProvider
from app.providers.adapters.geoapify import GeoapifyProvider
from app.providers.adapters.hunter import HunterProvider
from app.providers.adapters.mailboxvalidator import MailboxValidatorProvider
from app.providers.adapters.search import BraveSearchProvider, TavilyProvider
from app.providers.adapters.verifalia import VerifaliaProvider
from app.providers.adapters.zerobounce import ZeroBounceProvider
from app.providers.discovery import PlaceQuery

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE") != "1", reason="live test: set RUN_LIVE=1 and the provider's key"
    ),
]
CLOCK = FakeClock(datetime(2026, 11, 20, 9, 0, tzinfo=UTC))


def secret(name: str) -> SecretStr:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not set")
    return SecretStr(value)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as http:
        yield http


async def test_geoapify_finds_real_businesses(client: httpx.AsyncClient) -> None:
    provider = GeoapifyProvider(api_key=secret("GEOAPIFY_API_KEY"), client=client, clock=CLOCK)
    result = await provider.search_places(
        PlaceQuery(("dentist",), 30.2672, -97.7431, radius_m=4000, limit=5)
    )
    assert result.value, "expected at least one dentist within 4 km of downtown Austin"
    assert all(hit.name and hit.source_id for hit in result.value)


async def test_hunter_returns_contacts_for_a_known_domain(client: httpx.AsyncClient) -> None:
    provider = HunterProvider(api_key=secret("HUNTER_API_KEY"), client=client, clock=CLOCK)
    result = await provider.domain_contacts("stripe.com", limit=1)
    assert result.value.domain == "stripe.com"
    assert result.units_used is not None  # the credit header was read


async def test_verifalia_gives_a_verdict(client: httpx.AsyncClient) -> None:
    provider = VerifaliaProvider(
        username=secret("VERIFALIA_USERNAME"),
        password=secret("VERIFALIA_PASSWORD"),
        client=client,
        clock=CLOCK,
    )
    outcome = (await provider.verify_email("contact@example.com")).value
    assert outcome.verdict in {"deliverable", "undeliverable", "risky", "unknown"}
    assert (
        outcome.verdict == "undeliverable"
    )  # example.com has a null MX: it can never receive mail


async def test_mailboxvalidator_gives_a_verdict(client: httpx.AsyncClient) -> None:
    provider = MailboxValidatorProvider(
        api_key=secret("MAILBOXVALIDATOR_API_KEY"), client=client, clock=CLOCK
    )
    outcome = (await provider.verify_email("contact@example.com")).value
    assert outcome.verdict in {"deliverable", "undeliverable", "risky", "unknown"}


async def test_tavily_searches(client: httpx.AsyncClient) -> None:
    provider = TavilyProvider(api_key=secret("TAVILY_API_KEY"), client=client, clock=CLOCK)
    hits = (await provider.web_search("dental clinic Austin Texas", limit=3)).value
    assert hits
    assert all(h.url.startswith("https://") for h in hits)


async def test_brave_searches(client: httpx.AsyncClient) -> None:
    provider = BraveSearchProvider(
        api_key=secret("BRAVE_SEARCH_API_KEY"), client=client, clock=CLOCK
    )
    hits = (await provider.web_search("dental clinic austin", limit=3)).value
    assert hits
    assert all(h.url.startswith("http") for h in hits)


async def test_zerobounce_gives_a_verdict_and_reports_credits(client: httpx.AsyncClient) -> None:
    provider = ZeroBounceProvider(api_key=secret("ZEROBOUNCE_API_KEY"), client=client, clock=CLOCK)
    assert await provider.credits() >= 0  # a bad key would be -1
    outcome = (await provider.verify_email("contact@example.com")).value
    assert outcome.verdict == "undeliverable"  # example.com accepts no mail


async def test_abstract_email_reputation_gives_a_verdict(client: httpx.AsyncClient) -> None:
    provider = AbstractEmailProvider(
        api_key=secret("ABSTRACT_EMAIL_API_KEY"), client=client, clock=CLOCK
    )
    outcome = (await provider.verify_email("contact@example.com")).value
    assert outcome.verdict == "undeliverable"
