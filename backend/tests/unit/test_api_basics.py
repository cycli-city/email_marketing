from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from cryptography.fernet import Fernet
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from pydantic import BaseModel

from app import __version__
from app.core.config import Settings
from app.core.errors import ComplianceBlockedError, NotFoundError, RateLimitedError
from app.main import create_app

UNREACHABLE_DB = "postgresql+asyncpg://nobody:nopass@127.0.0.1:1/none"


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "encryption_key": Fernet.generate_key().decode(),
        "jwt_secret": "j" * 40,
        "database_url": UNREACHABLE_DB,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


@asynccontextmanager
async def running(
    settings: Settings | None = None, redis: object | None = None
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    app = create_app(settings or make_settings(), redis=redis or FakeAsyncRedis())  # type: ignore[arg-type]
    async with app.router.lifespan_context(app):
        # raise_app_exceptions=False lets us observe the 500 response the server would really send.
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client


class Payload(BaseModel):
    name: str
    count: int


async def test_healthz_reports_ok_and_version() -> None:
    async with running() as (_, client):
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


class TestRequestId:
    async def test_generated_when_absent(self) -> None:
        async with running() as (_, client):
            response = await client.get("/healthz")
        assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])

    async def test_valid_incoming_id_is_echoed(self) -> None:
        async with running() as (_, client):
            response = await client.get("/healthz", headers={"X-Request-ID": "trace-abc-12345"})
        assert response.headers["x-request-id"] == "trace-abc-12345"

    @pytest.mark.parametrize("bad", ["short", "has spaces in it!", "x" * 200, "inject\tdata12345"])
    async def test_malformed_incoming_id_is_replaced(self, bad: str) -> None:
        async with running() as (_, client):
            response = await client.get("/healthz", headers={"X-Request-ID": bad})
        assert response.headers["x-request-id"] != bad
        assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


async def test_security_headers_are_set() -> None:
    async with running() as (_, client):
        headers = (await client.get("/healthz")).headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "no-store"


class TestErrorEnvelope:
    async def test_unknown_route_is_404_envelope(self) -> None:
        async with running() as (_, client):
            response = await client.get("/nope")
        body = response.json()
        assert response.status_code == 404
        assert body["error"]["code"] == "not_found"
        assert body["error"]["request_id"] == response.headers["x-request-id"]

    async def test_validation_error_never_echoes_submitted_values(self) -> None:
        async with running() as (app, client):

            @app.post("/_echo")
            async def echo(payload: Payload) -> Payload:
                return payload

            response = await client.post("/_echo", json={"name": "hunter2-secret", "count": "NaN"})
        body = response.json()
        assert response.status_code == 422
        assert body["error"]["code"] == "validation_failed"
        assert body["error"]["details"]["issues"][0]["loc"] == ["body", "count"]
        assert "hunter2-secret" not in response.text

    async def test_app_errors_map_to_status_and_code(self) -> None:
        async with running() as (app, client):

            @app.get("/_missing")
            async def missing() -> None:
                raise NotFoundError("Campaign not found")

            @app.get("/_blocked")
            async def blocked() -> None:
                raise ComplianceBlockedError(
                    "Recipient is suppressed", details={"reason": "unsubscribed"}
                )

            missing_resp = await client.get("/_missing")
            blocked_resp = await client.get("/_blocked")
        assert missing_resp.status_code == 404
        assert missing_resp.json()["error"] == {
            "code": "not_found",
            "message": "Campaign not found",
            "details": {},
            "request_id": missing_resp.headers["x-request-id"],
        }
        assert blocked_resp.status_code == 422
        assert blocked_resp.json()["error"]["details"] == {"reason": "unsubscribed"}

    async def test_rate_limited_sets_retry_after(self) -> None:
        async with running() as (app, client):

            @app.get("/_slow")
            async def slow() -> None:
                raise RateLimitedError("Slow down", retry_after=7)

            response = await client.get("/_slow")
        assert response.status_code == 429
        assert response.headers["retry-after"] == "7"

    async def test_unhandled_exception_returns_generic_500_without_leaking(self) -> None:
        async with running() as (app, client):

            @app.get("/_boom")
            async def boom() -> None:
                raise RuntimeError("db password is hunter2")

            response = await client.get("/_boom")
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        assert "hunter2" not in response.text
        assert "RuntimeError" not in response.text
        assert response.json()["error"]["request_id"]


class TestCors:
    async def test_allowed_origin_is_reflected(self) -> None:
        settings = make_settings(frontend_url="https://app.example.com")
        async with running(settings) as (_, client):
            response = await client.get("/healthz", headers={"Origin": "https://app.example.com"})
        assert response.headers["access-control-allow-origin"] == "https://app.example.com"
        assert "access-control-allow-credentials" not in response.headers

    async def test_foreign_origin_gets_no_cors_headers(self) -> None:
        async with running() as (_, client):
            response = await client.get("/healthz", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in response.headers

    async def test_preflight_allows_authorization_header(self) -> None:
        async with running() as (_, client):
            response = await client.options(
                "/healthz",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization",
                },
            )
        assert response.status_code == 200
        assert "authorization" in response.headers["access-control-allow-headers"].lower()


async def test_docs_are_hidden_in_production() -> None:
    settings = make_settings(
        app_env="production",
        supabase_url="https://x.supabase.co",
        owner_emails="me@example.com",
        frontend_url="https://app.example.com",
        backend_url="https://api.example.com",
    )
    async with running(settings) as (_, client):
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404


async def test_docs_available_in_development() -> None:
    async with running() as (_, client):
        assert (await client.get("/openapi.json")).status_code == 200


class _BrokenRedis:
    async def ping(self) -> bool:
        raise ConnectionError("redis down")

    async def aclose(self) -> None:
        return None


class TestReadiness:
    async def test_degraded_when_database_is_unreachable(self) -> None:
        async with running() as (_, client):
            response = await client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {
            "status": "degraded",
            "checks": {"database": "error", "redis": "ok"},
        }

    async def test_degraded_when_redis_is_down_and_error_details_are_not_leaked(self) -> None:
        async with running(redis=_BrokenRedis()) as (_, client):
            response = await client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["checks"]["redis"] == "error"
        assert "ConnectionError" not in response.text
