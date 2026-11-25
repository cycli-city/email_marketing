"""The settings API end to end: real ASGI app, real PostgreSQL, real ES256 login tokens."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.clock import FakeClock
from app.db.models import AuditLog
from app.db.session import create_session_factory
from tests.auth_helpers import OWNER_EMAIL, SUPABASE_URL, FakeIdp
from tests.helpers import make_settings, running

NOW = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
PROFILES = "/api/v1/settings/sender-profiles"
POLICIES = "/api/v1/settings/policies"


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Requesting ``engine`` also truncates every table, so each test starts from an empty database."""
    return create_session_factory(engine)


@asynccontextmanager
async def api(
    migrated_database: str, idp: FakeIdp, clock: FakeClock, **settings: Any
) -> AsyncIterator[httpx.AsyncClient]:
    base: dict[str, Any] = {
        "database_url": migrated_database,
        "supabase_url": SUPABASE_URL,
        "owner_emails": [OWNER_EMAIL],
    }
    async with running(
        make_settings(**{**base, **settings}), clock=clock, http_transport=idp.transport()
    ) as (_, client):
        yield client


def auth(idp: FakeIdp) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {idp.token(NOW, email=OWNER_EMAIL, sub=str(uuid.UUID(int=1)))}"
    }


FULL = {
    "display_name": "Alex Founder",
    "postal_address": "1 Example Street\nExample City",
    "reply_to": "founder@example.com",
    "signature_text": "Alex",
    "website": "https://example.com",
}


class TestAccess:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", PROFILES),
            ("PUT", f"{PROFILES}/nexora"),
            ("GET", POLICIES),
            ("PATCH", f"{POLICIES}/US"),
        ],
    )
    async def test_every_route_needs_the_signed_in_owner(
        self,
        migrated_database: str,
        idp: FakeIdp,
        clock: FakeClock,
        factory: Any,
        method: str,
        path: str,
    ) -> None:
        async with api(migrated_database, idp, clock) as client:
            response = await client.request(method, path, json={})
        assert response.status_code == 401


class TestSenderProfiles:
    async def test_nothing_is_configured_at_first_and_the_missing_pieces_are_named(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as client:
            body = (await client.get(PROFILES, headers=auth(idp))).json()
        by_mode = {p["mode"]: p for p in body["profiles"]}
        assert set(by_mode) == {"nexora", "job_hunter"}
        assert by_mode["nexora"]["profile"] is None
        assert by_mode["nexora"]["missing"] == ["the sender's name", "the sender's postal address"]
        assert by_mode["job_hunter"]["missing"] == ["the sender's name"]

    async def test_saving_a_profile_makes_the_mode_ready(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as client:
            saved = await client.put(f"{PROFILES}/nexora", json=FULL, headers=auth(idp))
            listed = (await client.get(PROFILES, headers=auth(idp))).json()
        assert saved.status_code == 200
        assert saved.json()["missing"] == []
        assert saved.json()["profile"]["postal_address"] == "1 Example Street\nExample City"
        nexora = next(p for p in listed["profiles"] if p["mode"] == "nexora")
        assert nexora["profile"]["display_name"] == "Alex Founder"
        assert nexora["profile"]["website"] == "https://example.com"

    async def test_a_bad_field_is_a_422_that_names_the_field_and_saves_nothing(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as client:
            response = await client.put(
                f"{PROFILES}/nexora", json={**FULL, "reply_to": "not-an-address"}, headers=auth(idp)
            )
            listed = (await client.get(PROFILES, headers=auth(idp))).json()
        assert response.status_code == 422
        assert response.json()["error"]["details"]["field"] == "reply_to"
        assert next(p for p in listed["profiles"] if p["mode"] == "nexora")["profile"] is None

    async def test_oversized_input_is_refused_before_it_reaches_the_service(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as client:
            response = await client.put(
                f"{PROFILES}/nexora", json={**FULL, "postal_address": "x" * 5000}, headers=auth(idp)
            )
        assert response.status_code == 422

    async def test_an_unknown_mode_is_refused(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as client:
            response = await client.put(f"{PROFILES}/enterprise", json=FULL, headers=auth(idp))
        assert response.status_code == 422


class TestPolicies:
    async def test_the_defaults_appear_on_first_use_with_nothing_reviewed(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as client:
            body = (await client.get(POLICIES, headers=auth(idp))).json()
        countries = [p["country"] for p in body["policies"]]
        assert "DE" in countries
        assert countries[-1] == "ZZ"
        assert body["unreviewed"] == len(countries)
        germany = next(p for p in body["policies"] if p["country"] == "DE")
        assert germany["cold_b2b_marketing"] == "requires_consent"
        assert germany["requires_sender_identification"] is True
        assert germany["reviewed_on"] is None

    async def test_listing_twice_does_not_duplicate_anything(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as client:
            first = (await client.get(POLICIES, headers=auth(idp))).json()
            second = (await client.get(POLICIES, headers=auth(idp))).json()
        assert len(first["policies"]) == len(second["policies"])

    async def test_marking_a_country_reviewed_changes_nothing_else(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as client:
            await client.get(POLICIES, headers=auth(idp))
            response = await client.patch(
                f"{POLICIES}/de", json={}, headers=auth(idp)
            )  # case-insensitive
            body = (await client.get(POLICIES, headers=auth(idp))).json()
        assert response.status_code == 200
        assert response.json()["reviewed_on"] == "2026-11-25"
        assert response.json()["cold_b2b_marketing"] == "requires_consent"
        assert body["unreviewed"] == len(body["policies"]) - 1

    async def test_loosening_is_refused_with_the_reason_until_acknowledged(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as client:
            await client.get(POLICIES, headers=auth(idp))
            refused = await client.patch(
                f"{POLICIES}/DE", json={"cold_b2b_marketing": "allowed"}, headers=auth(idp)
            )
            unchanged = next(
                p
                for p in (await client.get(POLICIES, headers=auth(idp))).json()["policies"]
                if p["country"] == "DE"
            )
            accepted = await client.patch(
                f"{POLICIES}/DE",
                json={"cold_b2b_marketing": "allowed", "acknowledge_loosening": True},
                headers=auth(idp),
            )
        assert refused.status_code == 422
        assert refused.json()["error"]["details"]["loosens"] == ["cold_b2b_marketing"]
        assert unchanged["cold_b2b_marketing"] == "requires_consent"
        assert accepted.status_code == 200
        assert accepted.json()["cold_b2b_marketing"] == "allowed"
        assert accepted.json()["version"] == 2

    async def test_a_change_is_audited_with_the_owner_as_actor(
        self,
        migrated_database: str,
        idp: FakeIdp,
        clock: FakeClock,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with api(migrated_database, idp, clock) as client:
            owner_id = (await client.get("/api/v1/me", headers=auth(idp))).json()["user_id"]
            await client.get(POLICIES, headers=auth(idp))
            await client.patch(
                f"{POLICIES}/US",
                json={"cold_to_individuals": "requires_consent"},
                headers=auth(idp),
            )
        async with factory() as s:
            entries = list(
                (
                    await s.execute(select(AuditLog).where(AuditLog.action == "policy.updated"))
                ).scalars()
            )
        assert len(entries) == 1
        assert entries[0].actor_id == owner_id  # the owner's own user id, not a service account
        assert entries[0].details["after"]["cold_to_individuals"] == "requires_consent"

    async def test_an_unknown_country_is_a_404(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as client:
            await client.get(POLICIES, headers=auth(idp))
            response = await client.patch(f"{POLICIES}/XX", json={}, headers=auth(idp))
        assert response.status_code == 404

    @pytest.mark.parametrize("country", ["USA", "U", "1A", "u%20"])
    async def test_a_malformed_country_is_refused(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any, country: str
    ) -> None:
        async with api(migrated_database, idp, clock) as client:
            response = await client.patch(f"{POLICIES}/{country}", json={}, headers=auth(idp))
        assert response.status_code in (404, 422)

    async def test_an_invalid_value_is_refused(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as client:
            await client.get(POLICIES, headers=auth(idp))
            response = await client.patch(
                f"{POLICIES}/US", json={"cold_b2b_marketing": "whenever"}, headers=auth(idp)
            )
        assert response.status_code == 422

    async def test_the_sender_identification_requirement_cannot_be_switched_off(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as client:
            await client.get(POLICIES, headers=auth(idp))
            response = await client.patch(
                f"{POLICIES}/US", json={"requires_sender_identification": False}, headers=auth(idp)
            )
        assert response.status_code == 200  # an unknown field is ignored…
        assert (
            response.json()["requires_sender_identification"] is True
        )  # …and the requirement stands
