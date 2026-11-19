"""Liveness / readiness probes (used by Render health checks and uptime monitors)."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import __version__
from app.api.deps import get_redis, get_session_factory

router = APIRouter(tags=["health"])
logger = structlog.get_logger("health")

_CHECK_TIMEOUT_SECONDS = 3.0


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str = __version__


class ReadyResponse(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, Literal["ok", "error"]]


@router.get("/healthz", response_model=LiveResponse, summary="Liveness probe")
async def healthz() -> LiveResponse:
    return LiveResponse()


async def _check_database(factory: async_sessionmaker[AsyncSession]) -> bool:
    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_SECONDS), factory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("readyz.database_failed", error=type(exc).__name__)
        return False


async def _check_redis(redis: Redis) -> bool:
    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_SECONDS):
            await redis.ping()
        return True
    except Exception as exc:
        logger.warning("readyz.redis_failed", error=type(exc).__name__)
        return False


@router.get(
    "/readyz",
    response_model=ReadyResponse,
    summary="Readiness probe (database + Redis)",
    responses={503: {"model": ReadyResponse}},
)
async def readyz(
    response: Response,
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> ReadyResponse:
    db_ok, redis_ok = await asyncio.gather(_check_database(factory), _check_redis(redis))
    checks: dict[str, Literal["ok", "error"]] = {
        "database": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else "error",
    }
    if db_ok and redis_ok:
        return ReadyResponse(status="ready", checks=checks)
    response.status_code = 503
    return ReadyResponse(status="degraded", checks=checks)
