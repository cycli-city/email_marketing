"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.api import health, me
from app.api.errors import install_error_handlers
from app.auth.jwks import JwksCache
from app.auth.provisioning import OwnerDirectory
from app.auth.verifier import TokenVerifier, VerifierConfig
from app.core.clock import SYSTEM_CLOCK, Clock
from app.core.config import Settings, get_settings
from app.core.hardening import (
    BodySizeLimitMiddleware,
    ConcurrencyLimitMiddleware,
    HTTPSEnforcementMiddleware,
    RateLimitMiddleware,
    Rule,
)
from app.core.http import build_http_client
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.ratelimit import InMemoryRateLimiter, Limit
from app.db.session import create_engine, create_session_factory

logger = structlog.get_logger("app")

HSTS_MAX_AGE_SECONDS = 63_072_000  # two years


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the app (single process: API + in-process workers — ADR-008/024).

    ``clock`` and ``http_transport`` exist so tests can control time and fake the identity provider; production
    passes neither.
    """
    settings = settings or get_settings()
    clock = clock or SYSTEM_CLOCK
    configure_logging(settings.log_level, json_output=settings.app_env != "development")
    production = settings.app_env == "production"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        app.state.clock = clock
        # Plain HTTP is allowed to localhost only, and never in production (dev + the CI fake identity provider).
        loopback_http = not production
        http_client = build_http_client(transport=http_transport, allow_loopback_http=loopback_http)
        app.state.http_client = http_client
        if settings.supabase_jwks_url and settings.supabase_issuer:
            app.state.token_verifier = TokenVerifier(
                JwksCache(
                    settings.supabase_jwks_url,
                    http_client,
                    clock,
                    ttl_seconds=settings.auth_jwks_ttl_seconds,
                    allow_loopback_http=loopback_http,
                ),
                VerifierConfig(
                    issuer=settings.supabase_issuer, leeway_seconds=settings.auth_leeway_seconds
                ),
                clock,
            )
            app.state.owner_directory = OwnerDirectory(app.state.session_factory, clock)
        logger.info(
            "app.started",
            env=settings.app_env,
            role=settings.app_role,
            version=__version__,
            auth_configured=settings.supabase_auth_configured,
        )
        try:
            yield
        finally:
            await http_client.aclose()
            await engine.dispose()
            logger.info("app.stopped")

    app = FastAPI(
        title="AI Outreach Intelligence API",
        version=__version__,
        lifespan=lifespan,
        # Interactive docs are a dev convenience only; the OpenAPI schema itself stays available to
        # the frontend type generator via `scripts/export_openapi.py`.
        docs_url=None if production else "/docs",
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
    )
    app.state.settings = settings

    trusted = settings.effective_trusted_proxies
    limiter = InMemoryRateLimiter()  # exact for a single process; no shared store needed (ADR-024)
    sensitive = Limit("sensitive", settings.rate_limit_sensitive_per_minute)
    rules = [
        Rule("/api/v1/auth", sensitive),
        Rule("/api/v1/integrations/gmail/callback", sensitive),
        Rule("/u/", Limit("unsubscribe", settings.rate_limit_sensitive_per_minute)),
        Rule("/webhooks/", Limit("webhook", 600)),
    ]

    # Middleware stack — add_middleware() wraps, so the LAST added is the OUTERMOST layer.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(
        RateLimitMiddleware,
        limiter=limiter,
        rules=rules,
        default=Limit("api", settings.rate_limit_per_minute),
        trusted_proxies=trusted,
    )
    app.add_middleware(ConcurrencyLimitMiddleware, max_in_flight=settings.max_in_flight_requests)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,  # auth is a bearer token, never cookies ⇒ no CSRF surface
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After", "RateLimit-Limit", "RateLimit-Remaining"],
        max_age=600,
    )
    if production:
        app.add_middleware(
            HTTPSEnforcementMiddleware, public_host=settings.public_host, trusted_proxies=trusted
        )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(  # outermost: sees every response, incl. CORS preflight and rejections
        RequestContextMiddleware, hsts_max_age=HSTS_MAX_AGE_SECONDS if production else None
    )
    install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(me.router)
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn app.main:app_factory --factory``."""
    return create_app()
