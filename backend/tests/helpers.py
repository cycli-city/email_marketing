"""Shared helpers for tests that run the real ASGI app in-process (no network, no database needed)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI

from app.core.clock import Clock
from app.core.config import Settings
from app.main import create_app

UNREACHABLE_DB = "postgresql+asyncpg://nobody:nopass@127.0.0.1:1/none"

PROD_OVERRIDES: dict[str, object] = {
    "app_env": "production",
    "supabase_url": "https://x.supabase.co",
    "owner_emails": "me@example.com",
    "frontend_url": "https://app.example.com",
    "backend_url": "https://api.example.com",
    "database_url": "postgresql://u:p@db.example.com:5432/postgres?sslmode=require",
}


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "encryption_key": Fernet.generate_key().decode(),
        "jwt_secret": "j" * 40,
        "database_url": UNREACHABLE_DB,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def make_production_settings(**overrides: object) -> Settings:
    return make_settings(**{**PROD_OVERRIDES, **overrides})


@asynccontextmanager
async def running(
    settings: Settings | None = None,
    *,
    client_addr: tuple[str, int] = ("127.0.0.1", 123),
    base_url: str = "http://test",
    headers: dict[str, str] | None = None,
    clock: Clock | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    app = create_app(settings or make_settings(), clock=clock, http_transport=http_transport)
    async with app.router.lifespan_context(app):
        # raise_app_exceptions=False lets us observe the 500 response the server would really send.
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=client_addr)
        async with httpx.AsyncClient(
            transport=transport, base_url=base_url, headers=headers
        ) as client:
            yield app, client
