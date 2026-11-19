"""Shared fixtures.

* Unit tests never touch a database.
* Integration tests (``tests/integration``, auto-marked) run against a REAL PostgreSQL:
  - ``DATABASE_URL_TEST`` if set (GitHub Actions service container), else
  - a throwaway local cluster from the pip-installed ``pgserver`` (no Docker needed).
  Set ``REQUIRE_INTEGRATION=1`` to turn "no Postgres available" into a failure instead of a skip.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

# The environment must be prepared BEFORE app.core.config is imported anywhere.
os.environ["AOI_DISABLE_DOTENV"] = "1"
os.environ["APP_ENV"] = "test"
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import to_async_database_url
from app.db.session import create_session_factory

BACKEND_DIR = Path(__file__).resolve().parents[1]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.integration)


# ───────────────────────── PostgreSQL provisioning ─────────────────────────


@pytest.fixture(scope="session")
def pg_server_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """asyncpg-style URL of a PostgreSQL *server* (database ``postgres``)."""
    explicit = os.environ.get("DATABASE_URL_TEST")
    if explicit:
        yield to_async_database_url(explicit)
        return
    try:
        import pgserver
    except ImportError:
        if os.environ.get("REQUIRE_INTEGRATION"):
            pytest.fail("pgserver not installed and DATABASE_URL_TEST not set")
        pytest.skip("no PostgreSQL available (install dev deps or set DATABASE_URL_TEST)")
    data_dir = tmp_path_factory.mktemp("pgdata")
    server = pgserver.get_server(data_dir, cleanup_mode="delete")
    try:
        yield server.get_uri().replace("postgresql://", "postgresql+asyncpg://", 1)
    finally:
        server.cleanup()


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _admin(server_url: str, sql: str) -> None:
    async def run() -> None:
        conn = await asyncpg.connect(_dsn(server_url))
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(run())


def db_url(server_url: str, name: str) -> str:
    return make_url(server_url).set(database=name).render_as_string(hide_password=False)


def run_alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env["AOI_DISABLE_DOTENV"] = "1"
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def fresh_database(pg_server_url: str) -> Iterator[str]:
    """A brand-new empty database (for migration tests). Dropped afterwards."""
    name = f"aoi_{uuid.uuid4().hex[:12]}"
    _admin(pg_server_url, f'CREATE DATABASE "{name}"')
    try:
        yield db_url(pg_server_url, name)
    finally:
        _admin(pg_server_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def migrated_database(pg_server_url: str) -> Iterator[str]:
    """One database migrated to ``head`` once per test session."""
    name = f"aoi_test_{uuid.uuid4().hex[:8]}"
    _admin(pg_server_url, f'CREATE DATABASE "{name}"')
    url = db_url(pg_server_url, name)
    result = run_alembic(url, "upgrade", "head")
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}")
    try:
        yield url
    finally:
        _admin(pg_server_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest_asyncio.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_database, poolclass=NullPool)
    async with eng.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        )
        tables = ", ".join(f'public."{r[0]}"' for r in rows)
        await conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = create_session_factory(engine)
    async with factory() as s:
        yield s
        await s.rollback()
