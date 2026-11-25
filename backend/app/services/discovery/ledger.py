"""The lookup ledger (ADR-028 §3): never spend a provider credit on a question that was already answered.

Callers ask ``through(workspace, operation, input_key, ask)``. If the same question was answered recently — by *any* provider —
the stored answer is returned and ``ask`` is never called. Otherwise ``ask`` runs once, and its answer (including a
**negative** one: "nothing found for this domain") is stored with a time-to-live: long for something found, shorter for
nothing found, so a "no" is eventually retried.

Callers are responsible for normalising ``input_key`` (a registrable domain, a lower-cased address) so that spellings of
one question collapse to one key. Concurrent callers for the same key in one process share a single call; across several
processes the database's unique constraint keeps one row (a race there could, rarely, ask twice).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.db.models import ProviderLookup
from app.db.session import session_scope

POSITIVE_TTL = timedelta(days=90)
NEGATIVE_TTL = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class Asked:
    """What a provider call produced, in a form that can be stored."""

    found: bool
    result: dict[str, Any]
    answered_by: str


@dataclass(frozen=True, slots=True)
class Answer:
    found: bool
    result: dict[str, Any]
    answered_by: str
    cached: bool  # True → no provider was called


@dataclass
class LookupLedger:
    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock
    _locks: dict[tuple[uuid.UUID, str, str], asyncio.Lock] = field(default_factory=dict)

    async def recall(
        self, workspace_id: uuid.UUID, operation: str, input_key: str
    ) -> Answer | None:
        """The stored, still-valid answer, or ``None``."""
        async with self.session_factory() as s:
            row = (
                await s.execute(
                    select(ProviderLookup).where(
                        ProviderLookup.workspace_id == workspace_id,
                        ProviderLookup.operation == operation,
                        ProviderLookup.input_key == input_key,
                        ProviderLookup.expires_at > self.clock.now(),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return Answer(row.found, dict(row.result), row.answered_by, cached=True)

    async def remember(
        self,
        workspace_id: uuid.UUID,
        operation: str,
        input_key: str,
        asked: Asked,
        *,
        positive_ttl: timedelta = POSITIVE_TTL,
        negative_ttl: timedelta = NEGATIVE_TTL,
    ) -> None:
        now = self.clock.now()
        expires = now + (positive_ttl if asked.found else negative_ttl)
        values = {
            "answered_by": asked.answered_by,
            "found": asked.found,
            "result": asked.result,
            "looked_up_at": now,
            "expires_at": expires,
        }
        async with session_scope(self.session_factory) as s:
            await s.execute(
                pg_insert(ProviderLookup)
                .values(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    operation=operation,
                    input_key=input_key,
                    **values,
                )
                .on_conflict_do_update(
                    index_elements=["workspace_id", "operation", "input_key"], set_=values
                )
            )

    async def through(
        self,
        workspace_id: uuid.UUID,
        operation: str,
        input_key: str,
        ask: Callable[[], Awaitable[Asked]],
        *,
        positive_ttl: timedelta = POSITIVE_TTL,
        negative_ttl: timedelta = NEGATIVE_TTL,
    ) -> Answer:
        """Answer from the ledger, or ask once and remember. An exception from ``ask`` is **not** stored (a failed call is
        not an answer) and propagates unchanged."""
        lock = self._locks.setdefault((workspace_id, operation, input_key), asyncio.Lock())
        async with lock:
            cached = await self.recall(workspace_id, operation, input_key)
            if cached is not None:
                return cached
            asked = await ask()
            await self.remember(
                workspace_id,
                operation,
                input_key,
                asked,
                positive_ttl=positive_ttl,
                negative_ttl=negative_ttl,
            )
            return Answer(asked.found, asked.result, asked.answered_by, cached=False)
