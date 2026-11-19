"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from app import __version__
from app.api import health
from app.api.errors import install_error_handlers
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.db.session import create_engine, create_session_factory

logger = structlog.get_logger("app")


def create_app(settings: Settings | None = None, *, redis: Redis | None = None) -> FastAPI:
    """Build the app. ``redis`` may be injected (tests); otherwise it is created from ``REDIS_URL``."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.app_env != "development")
    production = settings.app_env == "production"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        owns_redis = redis is None
        app.state.redis = redis if redis is not None else Redis.from_url(settings.redis_url)
        logger.info(
            "app.started", env=settings.app_env, role=settings.app_role, version=__version__
        )
        try:
            yield
        finally:
            if owns_redis:
                await app.state.redis.aclose()
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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,  # auth is a bearer token, never cookies ⇒ no CSRF surface
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
        max_age=600,
    )
    app.add_middleware(
        RequestContextMiddleware
    )  # outermost: sees every response, incl. CORS preflight
    install_error_handlers(app)

    app.include_router(health.router)
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn app.main:app_factory --factory``."""
    return create_app()
