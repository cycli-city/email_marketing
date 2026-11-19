"""ASGI middleware that bounds what a single client can cost us, and enforces HTTPS.

Order in ``create_app`` (outermost first): request context → trusted host → HTTPS → CORS →
concurrency → rate limit → body size → app. CORS sits outside the limiters so 429/503 responses still
carry CORS headers and the browser can read ``Retry-After``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.net import resolve_client_ip, resolve_scheme
from app.core.ratelimit import Limit, RateLimiter

HEALTH_PATHS = frozenset({"/healthz"})


def _error(
    scope: Scope,
    status: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> Response:
    request_id = scope.get("state", {}).get("request_id")
    body = {"error": {"code": code, "message": message, "details": {}, "request_id": request_id}}
    return JSONResponse(body, status_code=status, headers=headers)


class RequestTooLargeError(Exception):
    """Raised from the wrapped ``receive`` when a streamed body exceeds the limit."""


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await _error(scope, 413, "payload_too_large", "Request body is too large")(
                scope, receive, send
            )
            return

        received = 0
        exceeded = False
        started = False  # has *a* response (ours or the app's) begun?

        async def send_413() -> None:
            nonlocal started
            started = True
            await _error(scope, 413, "payload_too_large", "Request body is too large")(
                scope, receive, send
            )

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    raise RequestTooLargeError  # stop reading immediately
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if exceeded and not started:
                # Frameworks (FastAPI) swallow the exception above and answer with their own generic
                # 400. Replace whatever the app says with the correct 413, and drop the app's remaining messages.
                await send_413()
                return
            if exceeded:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except RequestTooLargeError:
            if not started:
                await send_413()
            elif not exceeded:
                raise


class ConcurrencyLimitMiddleware:
    """Caps in-flight requests so a burst degrades into fast 503s instead of memory exhaustion."""

    def __init__(
        self, app: ASGIApp, max_in_flight: int, exempt: frozenset[str] = HEALTH_PATHS
    ) -> None:
        self.app = app
        self.max_in_flight = max_in_flight
        self.exempt = exempt
        self._in_flight = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.exempt:
            await self.app(scope, receive, send)
            return
        if self._in_flight >= self.max_in_flight:
            response = _error(
                scope, 503, "server_busy", "Server is busy, retry shortly", {"Retry-After": "1"}
            )
            await response(scope, receive, send)
            return
        self._in_flight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self._in_flight -= 1


@dataclass(frozen=True, slots=True)
class Rule:
    prefix: str
    limit: Limit


class RateLimitMiddleware:
    """Per-client-IP limits. The first rule whose prefix matches wins, else ``default`` applies."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: RateLimiter,
        rules: Sequence[Rule],
        default: Limit,
        trusted_proxies: int,
        exempt: frozenset[str] = HEALTH_PATHS,
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.rules = tuple(rules)
        self.default = default
        self.trusted_proxies = trusted_proxies
        self.exempt = exempt

    def _limit_for(self, path: str) -> Limit:
        for rule in self.rules:
            if path.startswith(rule.prefix):
                return rule.limit
        return self.default

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.exempt:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        peer = scope["client"][0] if scope.get("client") else None
        client_ip = resolve_client_ip(peer, headers.get("x-forwarded-for"), self.trusted_proxies)
        limit = self._limit_for(scope["path"])
        decision = await self.limiter.hit(client_ip, limit)

        rate_headers = {
            "RateLimit-Limit": str(decision.limit),
            "RateLimit-Remaining": str(decision.remaining),
        }
        if not decision.allowed:
            response = _error(
                scope,
                429,
                "rate_limited",
                "Too many requests",
                {"Retry-After": str(decision.retry_after), **rate_headers},
            )
            await response(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for key, value in rate_headers.items():
                    response_headers[key] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


class HTTPSEnforcementMiddleware:
    """Redirect plain-HTTP requests to HTTPS (308 keeps the method).

    The destination host comes from configuration (``BACKEND_URL``), never from the request's Host
    header, so this can't be turned into an open redirect.
    """

    def __init__(
        self,
        app: ASGIApp,
        public_host: str,
        trusted_proxies: int,
        exempt: frozenset[str] = HEALTH_PATHS,
    ) -> None:
        self.app = app
        self.public_host = public_host
        self.trusted_proxies = trusted_proxies
        self.exempt = exempt

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"} or scope["path"] in self.exempt:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        scheme = resolve_scheme(
            scope["scheme"], headers.get("x-forwarded-proto"), self.trusted_proxies
        )
        if scheme in {"https", "wss"}:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return

        path: Any = scope.get("raw_path") or scope["path"].encode()
        target = path.decode("latin-1") if isinstance(path, bytes) else str(path)
        query = scope.get("query_string", b"").decode("latin-1")
        location = f"https://{self.public_host}{target}" + (f"?{query}" if query else "")
        await Response(status_code=308, headers={"Location": location})(scope, receive, send)
