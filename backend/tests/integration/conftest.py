"""Fixtures shared by integration tests that need *committed* data (several connections, real transactions)."""

from __future__ import annotations

import uuid

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.session import create_session_factory, session_scope
from tests.factories import make_workspace


@pytest_asyncio.fixture
async def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory on a NullPool engine: every session is its own connection → real concurrency."""
    return create_session_factory(engine)


@pytest_asyncio.fixture
async def workspace_id(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_scope(factory) as s:
        ws = await make_workspace(s)
    return ws.id


@pytest_asyncio.fixture
async def other_workspace_id(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_scope(factory) as s:
        ws = await make_workspace(s)
    return ws.id
