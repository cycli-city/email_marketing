"""The Gmail connection API: real ASGI app, real PostgreSQL, real ES256 login tokens, fake Google."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.clock import FakeClock
from tests.auth_helpers import OWNER_EMAIL, SUPABASE_URL, FakeIdp
from tests.gmail_helpers import CLIENT_ID, CLIENT_SECRET, PERSONAL_EMAIL, REDIRECT_URI, FakeGoogle
from tests.helpers import make_settings, running

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
SECOND_OWNER = "second.owner@example.com"
PREFIX = "/api/v1/integrations/gmail"


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def clean_db(engine: AsyncEngine) -> None:
    """Requesting ``engine`` truncates every table, so each test starts empty."""


@asynccontextmanager
async def api(
    migrated_database: str, idp: FakeIdp, google: FakeGoogle, clock: FakeClock, **settings: Any
) -> AsyncIterator[httpx.AsyncClient]:
    idp_transport = idp.transport()

    def dispatch(request: httpx.Request) -> httpx.Response:
        handler = (
            idp_transport.handler if request.url.host == "abcdefghij.supabase.co" else google.handle
        )
        response = handler(request)
        assert isinstance(response, httpx.Response)
        return response

    base: dict[str, Any] = {
        "database_url": migrated_database,
        "supabase_url": SUPABASE_URL,
        "owner_emails": [OWNER_EMAIL, SECOND_OWNER],
        "google_client_id": CLIENT_ID,
        "google_client_secret": CLIENT_SECRET,
        "google_redirect_uri": REDIRECT_URI,
    }
    async with running(
        make_settings(**{**base, **settings}),
        clock=clock,
        http_transport=httpx.MockTransport(dispatch),
    ) as (_, client):
        yield client


def auth(
    idp: FakeIdp, *, email: str = OWNER_EMAIL, sub: int = 1, at: datetime = NOW
) -> dict[str, str]:
    return {"Authorization": f"Bearer {idp.token(at, email=email, sub=str(uuid.UUID(int=sub)))}"}


async def start(
    client: httpx.AsyncClient, headers: dict[str, str], mode: str = "job_hunter"
) -> str:
    response = await client.post(f"{PREFIX}/connect", json={"mode": mode}, headers=headers)
    assert response.status_code == 200, response.text
    url = response.json()["authorization_url"]
    return str(parse_qs(urlsplit(url).query)["state"][0])


class TestAccess:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/connect"),
            ("POST", "/exchange"),
            ("GET", "/accounts"),
            ("DELETE", f"/accounts/{uuid.uuid4()}"),
            ("POST", f"/accounts/{uuid.uuid4()}/test"),
        ],
    )
    async def test_every_route_needs_a_signed_in_owner(
        self,
        migrated_database: str,
        idp: FakeIdp,
        google: FakeGoogle,
        clock: FakeClock,
        clean_db: None,
        method: str,
        path: str,
    ) -> None:
        async with api(migrated_database, idp, google, clock) as client:
            response = await client.request(
                method, PREFIX + path, json={} if method == "POST" else None
            )
        assert response.status_code == 401

    async def test_a_login_that_is_not_the_owner_is_refused(
        self,
        migrated_database: str,
        idp: FakeIdp,
        google: FakeGoogle,
        clock: FakeClock,
        clean_db: None,
    ) -> None:
        async with api(migrated_database, idp, google, clock) as client:
            response = await client.get(
                f"{PREFIX}/accounts", headers=auth(idp, email="stranger@example.com")
            )
        assert response.status_code == 403

    async def test_without_google_credentials_the_feature_reports_that_it_is_not_configured(
        self,
        migrated_database: str,
        idp: FakeIdp,
        google: FakeGoogle,
        clock: FakeClock,
        clean_db: None,
    ) -> None:
        async with api(migrated_database, idp, google, clock, google_client_id=None) as client:
            response = await client.get(f"{PREFIX}/accounts", headers=auth(idp))
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "integration_not_configured"


class TestFullFlow:
    async def test_connect_list_test_and_disconnect_and_no_token_is_ever_returned(
        self,
        migrated_database: str,
        idp: FakeIdp,
        google: FakeGoogle,
        clock: FakeClock,
        clean_db: None,
    ) -> None:
        headers = auth(idp)
        bodies: list[str] = []
        async with api(migrated_database, idp, google, clock) as client:
            connect = await client.post(
                f"{PREFIX}/connect", json={"mode": "job_hunter"}, headers=headers
            )
            bodies.append(connect.text)
            url = connect.json()["authorization_url"]
            assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
            state = parse_qs(urlsplit(url).query)["state"][0]

            exchange = await client.post(
                f"{PREFIX}/exchange", json={"code": "the-code", "state": state}, headers=headers
            )
            bodies.append(exchange.text)
            assert exchange.status_code == 200, exchange.text
            account = exchange.json()
            assert (account["email"], account["mode"], account["status"]) == (
                PERSONAL_EMAIL,
                "job_hunter",
                "active",
            )
            assert account["account_type"] == "personal_gmail"

            listing = await client.get(f"{PREFIX}/accounts", headers=headers)
            bodies.append(listing.text)
            assert [a["id"] for a in listing.json()["accounts"]] == [account["id"]]

            checked = await client.post(f"{PREFIX}/accounts/{account['id']}/test", headers=headers)
            bodies.append(checked.text)
            assert checked.json() == {
                "ok": True,
                "email": PERSONAL_EMAIL,
                "messages_total": 1234,
            }

            deleted = await client.delete(f"{PREFIX}/accounts/{account['id']}", headers=headers)
            assert deleted.status_code == 204
            assert (await client.get(f"{PREFIX}/accounts", headers=headers)).json() == {
                "accounts": []
            }

        everything = "\n".join(bodies)
        for secret in (google.refresh_token, "ya29.", CLIENT_SECRET):
            assert secret not in everything
        assert not any("token" in key.lower() for key in account), (
            "no token-like field in the account JSON"
        )

    async def test_another_signed_in_user_cannot_finish_someone_elses_connection(
        self,
        migrated_database: str,
        idp: FakeIdp,
        google: FakeGoogle,
        clock: FakeClock,
        clean_db: None,
    ) -> None:
        """Both users are allow-listed owners, but the state belongs to the one who started the flow."""
        async with api(migrated_database, idp, google, clock) as client:
            state = await start(client, auth(idp))
            response = await client.post(
                f"{PREFIX}/exchange",
                json={"code": "x", "state": state},
                headers=auth(idp, email=SECOND_OWNER, sub=2),
            )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "oauth_state_invalid"
        assert google.token_calls == []


class TestErrors:
    async def test_forged_and_expired_states_are_refused(
        self,
        migrated_database: str,
        idp: FakeIdp,
        google: FakeGoogle,
        clock: FakeClock,
        clean_db: None,
    ) -> None:
        async with api(migrated_database, idp, google, clock) as client:
            forged = await client.post(
                f"{PREFIX}/exchange", json={"code": "x", "state": "forged"}, headers=auth(idp)
            )
            state = await start(client, auth(idp))
            clock.advance(minutes=11)
            expired = await client.post(
                f"{PREFIX}/exchange", json={"code": "x", "state": state}, headers=auth(idp)
            )
        assert [forged.status_code, expired.status_code] == [400, 400]
        assert {forged.json()["error"]["code"], expired.json()["error"]["code"]} == {
            "oauth_state_invalid"
        }

    async def test_a_code_google_refuses_is_a_clean_error_that_does_not_echo_the_code(
        self,
        migrated_database: str,
        idp: FakeIdp,
        google: FakeGoogle,
        clock: FakeClock,
        clean_db: None,
    ) -> None:
        google.code_error = "invalid_grant"
        async with api(migrated_database, idp, google, clock) as client:
            state = await start(client, auth(idp))
            response = await client.post(
                f"{PREFIX}/exchange",
                json={"code": "ECHOED-SECRET-CODE", "state": state},
                headers=auth(idp),
            )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "oauth_exchange_failed"
        assert "ECHOED-SECRET-CODE" not in response.text

    async def test_missing_permissions_are_reported_with_what_is_missing(
        self,
        migrated_database: str,
        idp: FakeIdp,
        google: FakeGoogle,
        clock: FakeClock,
        clean_db: None,
    ) -> None:
        google.granted_scopes = "openid email https://www.googleapis.com/auth/gmail.send"
        async with api(migrated_database, idp, google, clock) as client:
            state = await start(client, auth(idp))
            response = await client.post(
                f"{PREFIX}/exchange", json={"code": "c", "state": state}, headers=auth(idp)
            )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "gmail_scopes_missing"
        assert response.json()["error"]["details"]["missing"] == [
            "https://www.googleapis.com/auth/gmail.readonly"
        ]

    async def test_a_mailbox_cannot_be_connected_for_both_modes(
        self,
        migrated_database: str,
        idp: FakeIdp,
        google: FakeGoogle,
        clock: FakeClock,
        clean_db: None,
    ) -> None:
        headers = auth(idp)
        async with api(migrated_database, idp, google, clock) as client:
            first = await start(client, headers, "job_hunter")
            assert (
                await client.post(
                    f"{PREFIX}/exchange", json={"code": "1", "state": first}, headers=headers
                )
            ).status_code == 200
            second = await start(client, headers, "nexora")
            response = await client.post(
                f"{PREFIX}/exchange", json={"code": "2", "state": second}, headers=headers
            )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "mailbox_already_connected"

    async def test_a_revoked_connection_is_reported_as_needing_reconnection(
        self,
        migrated_database: str,
        idp: FakeIdp,
        google: FakeGoogle,
        clock: FakeClock,
        clean_db: None,
    ) -> None:
        headers = auth(idp)
        async with api(migrated_database, idp, google, clock) as client:
            state = await start(client, headers)
            account = (
                await client.post(
                    f"{PREFIX}/exchange", json={"code": "c", "state": state}, headers=headers
                )
            ).json()
            google.refresh_error = "invalid_grant"
            clock.advance(hours=2)
            headers = auth(idp, at=clock.now())  # the login itself expired too: sign in again
            response = await client.post(f"{PREFIX}/accounts/{account['id']}/test", headers=headers)
            listed = await client.get(f"{PREFIX}/accounts", headers=headers)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "gmail_needs_reauth"
        assert listed.json()["accounts"][0]["status"] == "needs_reauth"
        assert listed.json()["accounts"][0]["last_error"]

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"code": "", "state": "s"},
            {"code": "c", "state": ""},
            {"code": "x" * 3000, "state": "s"},
            {"code": "c", "state": "s" * 5000},
        ],
    )
    async def test_bad_exchange_input_is_rejected_before_any_work(
        self,
        migrated_database: str,
        idp: FakeIdp,
        google: FakeGoogle,
        clock: FakeClock,
        clean_db: None,
        body: dict[str, str],
    ) -> None:
        async with api(migrated_database, idp, google, clock) as client:
            response = await client.post(f"{PREFIX}/exchange", json=body, headers=auth(idp))
        assert response.status_code == 422
        assert google.requests == []

    async def test_an_unknown_mode_is_rejected(
        self,
        migrated_database: str,
        idp: FakeIdp,
        google: FakeGoogle,
        clock: FakeClock,
        clean_db: None,
    ) -> None:
        async with api(migrated_database, idp, google, clock) as client:
            response = await client.post(
                f"{PREFIX}/connect", json={"mode": "everything"}, headers=auth(idp)
            )
        assert response.status_code == 422


class TestContract:
    async def test_the_api_description_lists_the_routes_with_bearer_auth(
        self,
        migrated_database: str,
        idp: FakeIdp,
        google: FakeGoogle,
        clock: FakeClock,
        clean_db: None,
    ) -> None:
        async with api(migrated_database, idp, google, clock) as client:
            spec = (await client.get("/openapi.json")).json()
        for path in (f"{PREFIX}/connect", f"{PREFIX}/exchange", f"{PREFIX}/accounts"):
            assert path in spec["paths"]
        assert spec["paths"][f"{PREFIX}/connect"]["post"]["security"] == [{"HTTPBearer": []}]
