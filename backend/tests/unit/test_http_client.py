"""The shared outbound HTTP client: HTTPS only, with a narrow, explicit loopback exception for development."""

from __future__ import annotations

import httpx
import pytest

from app.auth.jwks import JwksCache
from app.core.clock import FakeClock
from app.core.http import InsecureRequestError, build_http_client, is_loopback_http
from tests.helpers import make_production_settings, running

OK = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))


@pytest.mark.parametrize("url", ["http://localhost:9/", "http://127.0.0.1:9/", "http://[::1]:9/"])
async def test_plain_http_is_refused_by_default_even_to_localhost(url: str) -> None:
    async with build_http_client(transport=OK) as client:
        with pytest.raises(InsecureRequestError):
            await client.get(url)


@pytest.mark.parametrize("url", ["http://localhost:9/", "http://127.0.0.1:9/", "http://[::1]:9/"])
async def test_loopback_http_is_allowed_only_when_explicitly_enabled(url: str) -> None:
    async with build_http_client(transport=OK, allow_loopback_http=True) as client:
        assert (await client.get(url)).status_code == 200


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example/",
        "http://localhost.evil.example/",  # looks like localhost, is not
        "http://127.0.0.1.evil.example/",
        "http://10.0.0.5/",  # private network is not loopback
        "http://169.254.169.254/",  # cloud metadata endpoint
    ],
)
async def test_the_exception_never_extends_beyond_this_machine(url: str) -> None:
    async with build_http_client(transport=OK, allow_loopback_http=True) as client:
        with pytest.raises(InsecureRequestError):
            await client.get(url)


def test_loopback_detection_is_exact() -> None:
    assert is_loopback_http("http://localhost:8000/x")
    assert not is_loopback_http("https://localhost:8000/x")  # https needs no exception
    assert not is_loopback_http("http://localhost.evil.example/")


class TestJwksUrl:
    def test_https_is_always_fine(self) -> None:
        JwksCache(
            "https://x.supabase.co/auth/v1/.well-known/jwks.json", build_http_client(), FakeClock()
        )

    def test_loopback_http_needs_the_flag(self) -> None:
        url = "http://127.0.0.1:54321/auth/v1/.well-known/jwks.json"
        with pytest.raises(ValueError, match="HTTPS"):
            JwksCache(url, build_http_client(), FakeClock())
        JwksCache(url, build_http_client(), FakeClock(), allow_loopback_http=True)

    def test_other_http_hosts_are_refused_even_with_the_flag(self) -> None:
        with pytest.raises(ValueError, match="HTTPS"):
            JwksCache(
                "http://evil.example/jwks.json",
                build_http_client(),
                FakeClock(),
                allow_loopback_http=True,
            )


async def test_production_never_enables_the_loopback_exception() -> None:
    async with running(make_production_settings()) as (app, _):
        with pytest.raises(InsecureRequestError):
            await app.state.http_client.get("http://127.0.0.1:9/")
