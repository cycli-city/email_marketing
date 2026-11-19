"""The one place outbound HTTP clients are built (provider adapters, token-key fetches, OAuth).

Rules (CLAUDE.md §1b): HTTPS only, certificates verified, **no redirects** (a redirect could carry an API key to
another host), bounded timeouts and connections, and a generic User-Agent that carries no personal details.
"""

from __future__ import annotations

import httpx

USER_AGENT = "aoi-outreach/0.1"


class InsecureRequestError(RuntimeError):
    """An adapter tried to call a non-HTTPS URL. This is a bug, never a runtime condition to handle."""


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_loopback_http(url: str) -> bool:
    """``http://`` to this machine only — used for local development and the CI fake identity provider."""
    parsed = httpx.URL(url)
    return parsed.scheme == "http" and parsed.host in LOOPBACK_HOSTS


def build_http_client(
    *,
    timeout_seconds: float = 30.0,
    transport: httpx.AsyncBaseTransport | None = None,
    allow_loopback_http: bool = False,
) -> httpx.AsyncClient:
    """Shared client for provider adapters. ``transport`` is only for tests (``httpx.MockTransport``).

    ``allow_loopback_http`` permits plain HTTP **to localhost only** (never to any other host). The application passes
    it only outside production.
    """

    async def require_https(request: httpx.Request) -> None:
        if request.url.scheme == "https":
            return
        if allow_loopback_http and is_loopback_http(str(request.url)):
            return
        raise InsecureRequestError(f"refusing non-HTTPS request to {request.url.host}")

    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds, connect=10.0),
        follow_redirects=False,
        verify=True,
        headers={"User-Agent": USER_AGENT},
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        event_hooks={"request": [require_https]},
        transport=transport,
    )
