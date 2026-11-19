"""Behavioural tests for the abuse-resistance and transport-security layers, run through the real app."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from pydantic import BaseModel

from tests.helpers import make_production_settings, make_settings, running


class Blob(BaseModel):
    data: str = ""


class TestRateLimiting:
    async def test_requests_over_the_limit_get_429_with_headers_and_a_json_envelope(self) -> None:
        async with running(make_settings(rate_limit_per_minute=3)) as (_, client):
            codes = [(await client.get("/nope")).status_code for _ in range(3)]
            blocked = await client.get("/nope")
        assert codes == [404, 404, 404]
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"].isdigit()
        assert blocked.headers["ratelimit-remaining"] == "0"
        error = blocked.json()["error"]
        assert error["code"] == "rate_limited"
        assert error["request_id"] == blocked.headers["x-request-id"]

    async def test_successful_responses_advertise_remaining_budget(self) -> None:
        async with running(make_settings(rate_limit_per_minute=10)) as (_, client):
            response = await client.get("/nope")
        assert response.headers["ratelimit-limit"] == "10"
        assert response.headers["ratelimit-remaining"] == "9"

    async def test_liveness_probe_is_never_rate_limited(self) -> None:
        async with running(make_settings(rate_limit_per_minute=1)) as (_, client):
            statuses = {(await client.get("/healthz")).status_code for _ in range(10)}
        assert statuses == {200}

    async def test_different_clients_have_separate_budgets(self) -> None:
        settings = make_settings(rate_limit_per_minute=2)
        async with running(settings, client_addr=("203.0.113.1", 1)) as (_, a):
            for _ in range(2):
                await a.get("/nope")
            assert (await a.get("/nope")).status_code == 429
        async with running(settings, client_addr=("203.0.113.2", 1)) as (_, b):
            assert (await b.get("/nope")).status_code == 404

    async def test_forging_x_forwarded_for_cannot_evade_the_limit(self) -> None:
        settings = make_settings(rate_limit_per_minute=2, trusted_proxy_count=1)
        async with running(settings) as (_, client):
            # Same real client (the last entry, appended by our proxy); attacker varies the forged prefix.
            codes = [
                (
                    await client.get(
                        "/nope", headers={"X-Forwarded-For": f"10.9.9.{i}, 198.51.100.7"}
                    )
                ).status_code
                for i in range(4)
            ]
        assert codes == [404, 404, 429, 429]

    async def test_forwarded_for_is_ignored_when_no_proxy_is_trusted(self) -> None:
        settings = make_settings(rate_limit_per_minute=2, trusted_proxy_count=0)
        async with running(settings) as (_, client):
            codes = [
                (await client.get("/nope", headers={"X-Forwarded-For": f"10.9.9.{i}"})).status_code
                for i in range(3)
            ]
        assert codes == [404, 404, 429]

    async def test_sensitive_paths_use_the_stricter_limit(self) -> None:
        settings = make_settings(rate_limit_per_minute=100, rate_limit_sensitive_per_minute=2)
        async with running(settings) as (_, client):
            codes = [(await client.get("/api/v1/auth/login")).status_code for _ in range(3)]
        assert codes == [404, 404, 429]

    async def test_limits_hold_with_the_database_down(self) -> None:
        """The limiter is in-process, so protection never depends on another service being up."""
        async with running(make_settings(rate_limit_per_minute=2)) as (_, client):  # DB unreachable
            codes = [(await client.get("/nope")).status_code for _ in range(3)]
        assert codes == [404, 404, 429]


class TestBodySizeLimit:
    async def test_declared_oversize_body_is_rejected_before_reading(self) -> None:
        async with running(make_settings(max_request_bytes=1024)) as (app, client):

            @app.post("/_blob")
            async def blob(payload: Blob) -> Blob:
                return payload

            response = await client.post(
                "/_blob", content=b"x" * 5000, headers={"Content-Type": "application/json"}
            )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"

    async def test_streamed_oversize_body_without_content_length_is_rejected(self) -> None:
        async with running(make_settings(max_request_bytes=1024)) as (app, client):

            @app.post("/_blob")
            async def blob(payload: Blob) -> Blob:
                return payload

            async def chunks() -> AsyncIterator[bytes]:
                for _ in range(10):
                    yield b"x" * 500

            response = await client.post(
                "/_blob", content=chunks(), headers={"Content-Type": "application/json"}
            )
        assert response.status_code == 413

    async def test_normal_bodies_pass(self) -> None:
        async with running(make_settings(max_request_bytes=1024)) as (app, client):

            @app.post("/_blob")
            async def blob(payload: Blob) -> Blob:
                return payload

            response = await client.post("/_blob", json={"data": "hello"})
        assert response.status_code == 200
        assert response.json() == {"data": "hello"}


class TestConcurrencyLimit:
    async def test_excess_concurrent_requests_get_503_and_health_stays_available(self) -> None:
        async with running(make_settings(max_in_flight_requests=1)) as (app, client):
            entered, release = asyncio.Event(), asyncio.Event()

            @app.get("/_slow")
            async def slow() -> dict[str, str]:
                entered.set()
                await release.wait()
                return {"done": "yes"}

            first = asyncio.create_task(client.get("/_slow"))
            await asyncio.wait_for(entered.wait(), timeout=5)
            rejected = await client.get("/nope")
            health = await client.get("/healthz")
            release.set()
            completed = await first

        assert rejected.status_code == 503
        assert rejected.headers["retry-after"] == "1"
        assert rejected.json()["error"]["code"] == "server_busy"
        assert health.status_code == 200  # liveness must survive saturation
        assert completed.status_code == 200

    async def test_capacity_is_released_after_each_request(self) -> None:
        async with running(make_settings(max_in_flight_requests=1)) as (_, client):
            codes = [(await client.get("/nope")).status_code for _ in range(5)]
        assert codes == [404] * 5


class TestHttpsEnforcement:
    async def test_plain_http_is_redirected_to_https_on_the_configured_host(self) -> None:
        settings = make_production_settings()
        async with running(settings, base_url="http://api.example.com") as (_, client):
            response = await client.get("/readyz?x=1", follow_redirects=False)
        assert response.status_code == 308  # method-preserving
        assert response.headers["location"] == "https://api.example.com/readyz?x=1"

    async def test_https_requests_pass_through(self) -> None:
        async with running(make_production_settings(), base_url="https://api.example.com") as (
            _,
            client,
        ):
            assert (
                await client.get("/nope")
            ).status_code == 404  # reached the router, not redirected

    async def test_trusted_proxy_forwarded_proto_is_honoured(self) -> None:
        settings = make_production_settings(trusted_proxy_count=1)
        async with running(settings, base_url="http://api.example.com") as (_, client):
            response = await client.get("/nope", headers={"X-Forwarded-Proto": "https"})
        assert response.status_code == 404

    async def test_forwarded_proto_is_ignored_without_a_trusted_proxy(self) -> None:
        settings = make_production_settings(trusted_proxy_count=0)
        async with running(settings, base_url="http://api.example.com") as (_, client):
            response = await client.get(
                "/nope", headers={"X-Forwarded-Proto": "https"}, follow_redirects=False
            )
        assert response.status_code == 308

    async def test_liveness_probe_is_exempt_so_platform_health_checks_work(self) -> None:
        async with running(make_production_settings(), base_url="http://api.example.com") as (
            _,
            client,
        ):
            assert (await client.get("/healthz")).status_code == 200

    async def test_redirect_cannot_be_steered_by_the_host_header(self) -> None:
        async with running(make_production_settings(), base_url="http://api.example.com") as (
            _,
            client,
        ):
            response = await client.get(
                "/x", headers={"Host": "evil.example"}, follow_redirects=False
            )
        assert (
            response.status_code == 400
        )  # rejected by the trusted-host check, never redirected to evil.example

    async def test_not_enforced_outside_production(self) -> None:
        async with running(make_settings()) as (_, client):
            assert (await client.get("/nope")).status_code == 404


class TestHeaders:
    async def test_hsts_only_in_production(self) -> None:
        async with running(make_production_settings(), base_url="https://api.example.com") as (
            _,
            client,
        ):
            hsts = (await client.get("/healthz")).headers["strict-transport-security"]
        assert "max-age=63072000" in hsts
        assert "includeSubDomains" in hsts
        async with running(make_settings()) as (_, dev):
            assert "strict-transport-security" not in (await dev.get("/healthz")).headers

    async def test_api_responses_carry_a_locked_down_csp(self) -> None:
        async with running(make_settings()) as (_, client):
            headers = (await client.get("/healthz")).headers
        assert headers["content-security-policy"] == (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        assert headers["cross-origin-opener-policy"] == "same-origin"
        assert headers["x-permitted-cross-domain-policies"] == "none"

    async def test_swagger_docs_are_exempt_from_the_api_csp_in_development(self) -> None:
        async with running(make_settings()) as (_, client):
            assert "content-security-policy" not in (await client.get("/docs")).headers


class TestTrustedHost:
    async def test_unknown_host_header_is_rejected(self) -> None:
        async with running(make_settings()) as (_, client):
            response = await client.get("/healthz", headers={"Host": "evil.example"})
        assert response.status_code == 400

    async def test_configured_host_is_accepted(self) -> None:
        settings = make_production_settings()
        async with running(settings, base_url="https://api.example.com") as (_, client):
            assert (await client.get("/healthz")).status_code == 200

    @pytest.mark.parametrize("host", ["api.example.com.evil.io", "evil.io", "api.example.org"])
    async def test_lookalike_hosts_are_rejected_in_production(self, host: str) -> None:
        async with running(make_production_settings(), base_url="https://api.example.com") as (
            _,
            client,
        ):
            response = await client.get("/healthz", headers={"Host": host})
        assert response.status_code == 400
