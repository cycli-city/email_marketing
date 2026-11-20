"""Shared plumbing for HTTP-API providers: one request helper that translates every failure into the provider taxonomy
(``app.providers.errors``), and keeps credentials out of anything that is logged or raised.

Some vendors only accept the key in the query string (``?apiKey=…``). That is why error text is scrubbed of the actual
secret values before it is raised, and why the HTTP client's own loggers are pinned to WARNING (see ``core.logging``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from app.core.clock import Clock
from app.core.redact import scrub_error
from app.providers.errors import (
    AuthError,
    ForbiddenReason,
    InvalidResponseError,
    PermissionDeniedError,
    ProviderError,
    TransientProviderError,
    error_from_status,
    parse_retry_after,
)
from app.providers.registry import Provider
from app.providers.spec import ProviderSpec

_MAX_ERROR_TEXT = 300


class HttpProvider(Provider):
    """Base for adapters that talk to one HTTPS API. Subclasses set ``spec`` and call :meth:`request`."""

    spec: ProviderSpec

    def __init__(self, *, client: httpx.AsyncClient, clock: Clock, secrets: Sequence[str]) -> None:
        self._client = client
        self._clock = clock
        self._secrets = tuple(s for s in secrets if s)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any | None = None,
        headers: Mapping[str, str] | None = None,
        auth: tuple[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> httpx.Response:
        if not url.startswith("https://"):
            raise ValueError(f"{self.spec.key}: provider URLs must be HTTPS")
        try:
            response = await self._client.request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
                auth=auth,
                timeout=timeout_seconds,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise TransientProviderError(
                f"cannot reach {self.spec.display_name}: {type(exc).__name__}"
            ) from exc
        except httpx.TransportError as exc:
            raise TransientProviderError(
                f"connection to {self.spec.display_name} failed: {type(exc).__name__}",
                request_may_have_been_processed=True,
            ) from exc
        if response.status_code >= 400:
            raise self.error_for(response)
        return response

    def error_for(self, response: httpx.Response) -> ProviderError:
        """Default mapping; adapters override :meth:`refine_error` to recognise vendor-specific bodies."""
        text = scrub_error(response.text[:_MAX_ERROR_TEXT], known_secrets=self._secrets)
        retry_after = parse_retry_after(response.headers.get("retry-after"), now=self._clock.now())
        refined = self.refine_error(response.status_code, text)
        if refined is not None:
            refined.provider_key = self.spec.key
            return refined
        status = response.status_code
        message = f"{self.spec.display_name} answered HTTP {status}: {text}".strip()
        reason = ForbiddenReason.PERMISSIONS if status == 403 else ForbiddenReason.UNKNOWN
        error = error_from_status(status, message, retry_after=retry_after, forbidden_reason=reason)
        error.provider_key = self.spec.key
        return error

    def refine_error(self, status: int, text: str) -> ProviderError | None:
        """Vendor-specific recognition (e.g. a 402 that means 'out of credits'). Default: no opinion."""
        return None

    @staticmethod
    def json_body(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise InvalidResponseError(
                "the provider answered with something that is not JSON"
            ) from exc

    @staticmethod
    def auth_problem(message: str) -> AuthError:
        return AuthError(message)

    @staticmethod
    def blocked(message: str) -> PermissionDeniedError:
        return PermissionDeniedError(message, reason=ForbiddenReason.ACCOUNT_RESTRICTED)
