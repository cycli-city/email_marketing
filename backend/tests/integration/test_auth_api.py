"""Authentication end to end: real ASGI app, real PostgreSQL, real ES256 signatures, fake identity provider."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.clock import FakeClock
from app.db.models import AuditLog, User, Workspace
from app.db.session import create_session_factory
from tests.auth_helpers import OWNER_EMAIL, SUPABASE_URL, FakeIdp
from tests.helpers import make_settings, running

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
SUB_A = str(uuid.UUID(int=1))
SUB_B = str(uuid.UUID(int=2))


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@asynccontextmanager
async def api(
    migrated_database: str,
    idp: FakeIdp,
    clock: FakeClock,
    **settings: Any,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    base: dict[str, Any] = {
        "database_url": migrated_database,
        "supabase_url": SUPABASE_URL,
        "owner_emails": [OWNER_EMAIL],
    }
    async with running(
        make_settings(**{**base, **settings}), clock=clock, http_transport=idp.transport()
    ) as pair:
        yield pair


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def count(factory: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with factory() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Also truncates every table, so each test starts from an empty database."""
    return create_session_factory(engine)


# ───────────────────────────── rejecting requests ─────────────────────────────


class TestUnauthenticated:
    async def test_no_credentials_is_a_401_with_the_standard_envelope(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as (_, client):
            response = await client.get("/api/v1/me")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json()["error"]["code"] == "unauthorized"

    @pytest.mark.parametrize(
        "headers",
        [
            {"Authorization": "Basic dXNlcjpwYXNz"},
            {"Authorization": "Bearer"},
            {"Authorization": "Bearer not-a-jwt"},
            {"Authorization": "bearer   "},
        ],
    )
    async def test_malformed_credentials_are_rejected_with_a_generic_message(
        self,
        migrated_database: str,
        idp: FakeIdp,
        clock: FakeClock,
        factory: Any,
        headers: dict[str, str],
    ) -> None:
        async with api(migrated_database, idp, clock) as (_, client):
            response = await client.get("/api/v1/me", headers=headers)
        assert response.status_code == 401
        message = response.json()["error"]["message"]
        assert message in {"Authentication required", "Invalid or expired session"}

    async def test_the_reason_for_a_rejection_is_never_revealed_to_the_client(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as (_, client):
            expired = await client.get(
                "/api/v1/me", headers=bearer(idp.token(NOW, exp=int(NOW.timestamp()) - 100))
            )
            forged = await client.get("/api/v1/me", headers=bearer(FakeIdp().token(NOW)))
        assert (expired.status_code, forged.status_code) == (401, 401)
        for response in (expired, forged):  # an attacker learns nothing about *why* a token failed
            assert response.json()["error"]["code"] == "unauthorized"
            assert response.json()["error"]["message"] == "Invalid or expired session"

    async def test_the_public_anon_and_service_role_keys_are_not_logins(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as (_, client):
            for role in ("anon", "service_role"):
                response = await client.get("/api/v1/me", headers=bearer(idp.token(NOW, role=role)))
                assert response.status_code == 401

    async def test_only_the_authorization_header_is_honoured_not_cookies_or_query_strings(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        token = idp.token(NOW)
        async with api(migrated_database, idp, clock) as (_, client):
            by_cookie = await client.get(
                "/api/v1/me", headers={"Cookie": f"sb-access-token={token}; token={token}"}
            )
            by_query = await client.get(
                "/api/v1/me", params={"access_token": token, "token": token}
            )
        assert by_cookie.status_code == by_query.status_code == 401

    async def test_expiry_follows_the_clock(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        token = idp.token(NOW)
        async with api(migrated_database, idp, clock) as (_, client):
            assert (await client.get("/api/v1/me", headers=bearer(token))).status_code == 200
            clock.advance(hours=1, seconds=30)
            assert (await client.get("/api/v1/me", headers=bearer(token))).status_code == 401


class TestAuthorisation:
    async def test_a_valid_login_that_is_not_the_owner_is_forbidden_and_leaves_no_trace(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as (_, client):
            response = await client.get(
                "/api/v1/me", headers=bearer(idp.token(NOW, email="stranger@example.com"))
            )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
        assert (await count(factory, User), await count(factory, Workspace)) == (0, 0)

    async def test_the_allow_list_ignores_case_and_whitespace(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock, owner_emails=["  Owner@Example.COM "]) as (
            _,
            client,
        ):
            response = await client.get(
                "/api/v1/me", headers=bearer(idp.token(NOW, email="OWNER@example.com"))
            )
        assert response.status_code == 200

    async def test_an_empty_allow_list_admits_nobody(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock, owner_emails=[]) as (_, client):
            response = await client.get("/api/v1/me", headers=bearer(idp.token(NOW)))
        assert response.status_code == 403

    async def test_a_second_factor_is_demanded_when_configured(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock, auth_require_mfa=True) as (_, client):
            password_only = await client.get(
                "/api/v1/me", headers=bearer(idp.token(NOW, aal="aal1"))
            )
            with_mfa = await client.get("/api/v1/me", headers=bearer(idp.token(NOW, aal="aal2")))
        assert password_only.status_code == 403
        assert password_only.json()["error"]["code"] == "mfa_required"
        assert with_mfa.status_code == 200
        assert with_mfa.json()["assurance_level"] == "aal2"


class TestFailingClosed:
    async def test_authentication_that_is_not_configured_refuses_everything(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock, supabase_url=None) as (_, client):
            response = await client.get("/api/v1/me", headers=bearer(idp.token(NOW)))
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "auth_unavailable"

    async def test_an_unreachable_identity_provider_refuses_logins_but_not_health_checks(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        idp.outage = True
        async with api(migrated_database, idp, clock) as (_, client):
            login = await client.get("/api/v1/me", headers=bearer(idp.token(NOW)))
            health = await client.get("/healthz")
        assert login.status_code == 503
        assert login.json()["error"]["code"] == "auth_unavailable"
        assert health.status_code == 200


# ───────────────────────────── the happy path & provisioning ─────────────────────────────


class TestOwnerSession:
    async def test_me_describes_the_authenticated_owner(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as (_, client):
            response = await client.get("/api/v1/me", headers=bearer(idp.token(NOW)))
        body = response.json()
        assert response.status_code == 200
        assert body["email"] == OWNER_EMAIL
        assert body["assurance_level"] == "aal1"
        assert body["session_expires_at"].startswith("2026-11-19T11:00:00")
        assert response.headers["cache-control"] == "no-store"

    async def test_the_first_login_provisions_the_workspace_and_owner_once(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as (_, client):
            first = await client.get("/api/v1/me", headers=bearer(idp.token(NOW)))
            clock.advance(
                minutes=5
            )  # past the in-process cache, so the database is consulted again
            second = await client.get("/api/v1/me", headers=bearer(idp.token(NOW)))
        assert first.json()["user_id"] == second.json()["user_id"]
        assert first.json()["workspace_id"] == second.json()["workspace_id"]
        assert (await count(factory, User), await count(factory, Workspace)) == (1, 1)
        async with factory() as s:
            actions = [a.action for a in (await s.execute(select(AuditLog))).scalars()]
        assert actions == ["auth.owner_provisioned"]

    async def test_simultaneous_first_requests_still_create_exactly_one_owner(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        token = idp.token(NOW)
        async with api(migrated_database, idp, clock) as (_, client):
            responses = await asyncio.gather(
                *(client.get("/api/v1/me", headers=bearer(token)) for _ in range(12))
            )
        assert {r.status_code for r in responses} == {200}
        assert len({r.json()["user_id"] for r in responses}) == 1
        assert (
            await count(factory, User),
            await count(factory, Workspace),
            await count(factory, AuditLog),
        ) == (1, 1, 1)

    async def test_a_recreated_account_for_the_same_owner_is_relinked_not_duplicated(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as (_, client):
            original = await client.get("/api/v1/me", headers=bearer(idp.token(NOW, sub=SUB_A)))
            clock.advance(minutes=5)
            recreated = await client.get(
                "/api/v1/me",
                headers=bearer(idp.token(NOW, sub=SUB_B, iat=int(clock.now().timestamp()))),
            )
        assert recreated.status_code == 200
        assert recreated.json()["user_id"] == original.json()["user_id"]
        async with factory() as s:
            user = (await s.execute(select(User))).scalar_one()
            actions = sorted(a.action for a in (await s.execute(select(AuditLog))).scalars())
        assert str(user.supabase_user_id) == SUB_B
        assert actions == ["auth.owner_provisioned", "auth.owner_relinked"]

    async def test_last_seen_is_written_sparingly(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async def last_seen() -> datetime | None:
            async with factory() as s:
                value: datetime | None = (await s.execute(select(User.last_seen_at))).scalar_one()
            return value

        async with api(migrated_database, idp, clock) as (_, client):

            async def call() -> None:
                token = idp.token(clock.now())
                assert (await client.get("/api/v1/me", headers=bearer(token))).status_code == 200

            await call()
            first = await last_seen()
            assert first == NOW
            clock.advance(
                seconds=120
            )  # database consulted again (cache is 60 s) but too soon to rewrite
            await call()
            assert await last_seen() == first
            clock.advance(seconds=200)  # now more than 5 minutes since the last write
            await call()
            assert await last_seen() == NOW + timedelta(seconds=320)


class TestContract:
    async def test_the_api_description_declares_bearer_authentication(
        self, migrated_database: str, idp: FakeIdp, clock: FakeClock, factory: Any
    ) -> None:
        async with api(migrated_database, idp, clock) as (_, client):
            spec = (await client.get("/openapi.json")).json()
        operation = spec["paths"]["/api/v1/me"]["get"]
        assert operation["security"] == [{"HTTPBearer": []}]
        assert spec["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
        assert {"401", "403", "503"} <= set(operation["responses"])
