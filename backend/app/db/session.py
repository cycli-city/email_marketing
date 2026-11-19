"""Async engine / session factory."""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_engine(settings: Settings, *, url: str | None = None) -> AsyncEngine:
    target = url or settings.database_url
    connect_args: dict[str, object] = {}
    transaction_pooler = "pooler.supabase.com:6543" in target or ":6543" in target
    if transaction_pooler:
        # pgbouncer/Supavisor transaction mode: no prepared statements, no session-level settings.
        # Prefer the direct / session-mode connection (port 5432) for this always-on service.
        connect_args["statement_cache_size"] = 0
        connect_args["prepared_statement_cache_size"] = 0
    else:
        # A runaway query or a stuck transaction must not be able to hold the database hostage.
        connect_args["server_settings"] = {
            "statement_timeout": str(settings.db_statement_timeout_ms),
            "idle_in_transaction_session_timeout": "60000",
            "application_name": "aoi",
        }
    if settings.database_ssl_ca_file:
        # Verify the server certificate AND hostname (protects against man-in-the-middle).
        connect_args["ssl"] = ssl.create_default_context(cafile=settings.database_ssl_ca_file)
    return create_async_engine(
        target,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, rollback on error."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
