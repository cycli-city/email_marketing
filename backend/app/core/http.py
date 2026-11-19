"""The one place outbound HTTP clients are built (provider adapters, token-key fetches, OAuth).

Rules (CLAUDE.md §1b): HTTPS only, certificates verified, **no redirects** (a redirect could carry an API key to
another host), bounded timeouts and connections, and a generic User-Agent that carries no personal details.
"""

from __future__ import annotations

import httpx

USER_AGENT = "aoi-outreach/0.1"


class InsecureRequestError(RuntimeError):
    """An adapter tried to call a non-HTTPS URL. This is a bug, never a runtime condition to handle."""


async def _require_https(request: httpx.Request) -> None:
    if request.url.scheme != "https":
        raise InsecureRequestError(f"refusing non-HTTPS request to {request.url.host}")


def build_http_client(
    *, timeout_seconds: float = 30.0, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """Shared client for provider adapters. ``transport`` is only for tests (``httpx.MockTransport``)."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds, connect=10.0),
        follow_redirects=False,
        verify=True,
        headers={"User-Agent": USER_AGENT},
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        event_hooks={"request": [_require_https]},
        transport=transport,
    )
