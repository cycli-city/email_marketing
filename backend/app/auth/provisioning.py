"""Mapping a verified Supabase identity to our ``users`` / ``workspaces`` rows (ADR-012: single owner).

The first login of an allow-listed owner creates the one workspace and the owner row; later logins just look them up.
Everything is idempotent and safe if two first requests race (unique constraints + ``ON CONFLICT``).

The allow-list is checked against the *token* on every request by :func:`app.auth.deps.require_owner`; this module only
supplies ids, so a short cache of the lookup is safe (it can never grant access by itself).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.claims import Principal
from app.core.clock import Clock
from app.db.models import AuditLog, User, Workspace
from app.db.session import session_scope
from app.domain.enums import ActorType

log = structlog.get_logger("auth.provisioning")

DEFAULT_WORKSPACE_SLUG = "personal"
DEFAULT_WORKSPACE_NAME = "Personal"


@dataclass(frozen=True, slots=True)
class ProvisionedOwner:
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    created: bool = False


class OwnerDirectory:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        *,
        cache_seconds: float = 60.0,
        last_seen_interval_seconds: float = 300.0,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock
        self._cache_ttl = timedelta(seconds=cache_seconds)
        self._last_seen_interval = timedelta(seconds=last_seen_interval_seconds)
        self._cache: dict[uuid.UUID, tuple[ProvisionedOwner, datetime]] = {}

    async def resolve(self, principal: Principal) -> ProvisionedOwner:
        now = self._clock.now()
        cached = self._cache.get(principal.user_id)
        if cached is not None and cached[1] > now:
            return cached[0]
        async with session_scope(self._sessions) as session:
            owner = await self._ensure(session, principal, now)
        if len(self._cache) >= 32:  # bounded: the allow-list is tiny, this is only a guard
            self._cache.clear()
        self._cache[principal.user_id] = (owner, now + self._cache_ttl)
        return owner

    # ───────────────────────────── internals ─────────────────────────────

    async def _ensure(
        self, session: AsyncSession, principal: Principal, now: datetime
    ) -> ProvisionedOwner:
        user = (
            await session.execute(select(User).where(User.supabase_user_id == principal.user_id))
        ).scalar_one_or_none()
        if user is not None:
            self._touch(user, principal, now)
            return ProvisionedOwner(user.id, user.workspace_id)

        workspace_id = await self._default_workspace(session)
        # The same person with a recreated Supabase account: re-link instead of failing the (workspace, email) constraint.
        existing = (
            await session.execute(
                select(User).where(User.workspace_id == workspace_id, User.email == principal.email)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.supabase_user_id == principal.user_id:
                # A concurrent first request created this exact row between our two lookups: nothing to do (and
                # nothing to audit — the append-only log must not record a link that never changed).
                return ProvisionedOwner(existing.id, workspace_id)
            existing.supabase_user_id = principal.user_id
            existing.last_seen_at = now
            self._audit(
                session,
                workspace_id=workspace_id,
                principal=principal,
                action="auth.owner_relinked",
                user_id=existing.id,
                now=now,
            )
            log.info("auth.owner_relinked")
            return ProvisionedOwner(existing.id, workspace_id)

        inserted = await session.execute(
            pg_insert(User)
            .values(
                workspace_id=workspace_id,
                supabase_user_id=principal.user_id,
                email=principal.email,
                last_seen_at=now,
            )
            .on_conflict_do_nothing(index_elements=["supabase_user_id"])
            .returning(User.id)
        )
        new_id = inserted.scalar_one_or_none()
        if new_id is None:  # lost a race with a concurrent first request: it created the row
            winner = (
                await session.execute(
                    select(User).where(User.supabase_user_id == principal.user_id)
                )
            ).scalar_one()
            return ProvisionedOwner(winner.id, winner.workspace_id)
        self._audit(
            session,
            workspace_id=workspace_id,
            principal=principal,
            action="auth.owner_provisioned",
            user_id=new_id,
            now=now,
        )
        log.info("auth.owner_provisioned")
        return ProvisionedOwner(new_id, workspace_id, created=True)

    async def _default_workspace(self, session: AsyncSession) -> uuid.UUID:
        await session.execute(
            pg_insert(Workspace)
            .values(name=DEFAULT_WORKSPACE_NAME, slug=DEFAULT_WORKSPACE_SLUG)
            .on_conflict_do_nothing(index_elements=["slug"])
        )
        return (
            await session.execute(
                select(Workspace.id).where(Workspace.slug == DEFAULT_WORKSPACE_SLUG)
            )
        ).scalar_one()

    def _touch(self, user: User, principal: Principal, now: datetime) -> None:
        """Keep email current and ``last_seen_at`` fresh — but write at most every few minutes."""
        if user.email != principal.email:
            user.email = principal.email
        if user.last_seen_at is None or now - user.last_seen_at >= self._last_seen_interval:
            user.last_seen_at = now

    @staticmethod
    def _audit(
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        principal: Principal,
        action: str,
        user_id: uuid.UUID,
        now: datetime,
    ) -> None:
        session.add(
            AuditLog(
                workspace_id=workspace_id,
                created_at=now,
                actor_type=ActorType.USER,
                actor_id=str(principal.user_id),
                action=action,
                entity_type="user",
                entity_id=str(user_id),
                details={"aal": principal.aal},
            )
        )


__all__ = ["DEFAULT_WORKSPACE_SLUG", "OwnerDirectory", "ProvisionedOwner"]
