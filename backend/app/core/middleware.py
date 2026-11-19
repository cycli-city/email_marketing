"""Pure-ASGI middleware: request ids + structured access log + security headers."""

from __future__ import annotations

import re
import time
import uuid

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_QUIET_PATHS = frozenset({"/healthz", "/readyz"})

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-site",
    "Cross-Origin-Opener-Policy": "same-origin",
    "X-Permitted-Cross-Domain-Policies": "none",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    # API responses are JSON; never cache authenticated data in shared caches.
    "Cache-Control": "no-store",
}
# The API serves data, not documents: nothing it returns should ever be rendered, framed or scripted.
_API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"

logger = structlog.get_logger("http")


class RequestContextMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        hsts_max_age: int | None = None,
        csp_exempt_prefixes: tuple[str, ...] = ("/docs", "/openapi.json"),
    ) -> None:
        self.app = app
        self.hsts_max_age = hsts_max_age
        self.csp_exempt_prefixes = csp_exempt_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id, method=scope["method"], path=scope["path"]
        )
        started = time.perf_counter()
        status_code = 500
        apply_csp = not scope["path"].startswith(self.csp_exempt_prefixes)

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                for key, value in _SECURITY_HEADERS.items():
                    headers.setdefault(key, value)
                if apply_csp:
                    headers.setdefault("Content-Security-Policy", _API_CSP)
                if self.hsts_max_age:
                    headers.setdefault(
                        "Strict-Transport-Security",
                        f"max-age={self.hsts_max_age}; includeSubDomains",
                    )
                # Don't advertise the server stack.
                if "server" in headers:
                    del headers["server"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            log = logger.debug if scope["path"] in _QUIET_PATHS else logger.info
            log("request.completed", status=status_code, duration_ms=duration_ms)
            structlog.contextvars.clear_contextvars()
