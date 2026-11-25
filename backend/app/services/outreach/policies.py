"""Seeding and reading the per-country sending policies (ADR-010, ADR-043).

Seeding is *additive*: it inserts the defaults for countries that have no row and leaves every existing row exactly as it is,
so running it again — at every start-up, say — can never undo a decision the owner made in Settings. A seeded row has
``reviewed_on = NULL`` until the owner reviews it, which is what the Settings page shows as "not reviewed yet".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.db.models import AuditLog, JurisdictionPolicy
from app.db.session import session_scope
from app.domain.enums import ActorType, ConsentPolicy
from app.domain.policy_defaults import default_policies


async def seed_default_policies(
    factory: async_sessionmaker[AsyncSession], clock: Clock, workspace_id: uuid.UUID
) -> int:
    """Insert the default policy for every country that has none yet. Returns how many rows were added."""
    now = clock.now()
    added = 0
    async with session_scope(factory) as s:
        for default in default_policies():
            policy = default.policy
            inserted = (
                await s.execute(
                    pg_insert(JurisdictionPolicy)
                    .values(
                        id=uuid.uuid4(),
                        workspace_id=workspace_id,
                        country=policy.country,
                        cold_b2b_marketing=policy.cold_b2b_marketing,
                        cold_to_individuals=policy.cold_to_individuals,
                        requires_unsubscribe=policy.requires_unsubscribe,
                        requires_postal_address=policy.requires_postal_address,
                        requires_sender_identification=policy.requires_sender_identification,
                        notes=default.note,
                        reviewed_on=None,
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=["workspace_id", "country"])
                    .returning(JurisdictionPolicy.id)
                )
            ).scalar_one_or_none()
            added += inserted is not None
    return added


async def unreviewed_countries(
    factory: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID
) -> list[str]:
    """Countries whose policy is still the seeded default (the owner has not marked it reviewed)."""
    async with factory() as s:
        rows = await s.execute(
            select(JurisdictionPolicy.country)
            .where(
                JurisdictionPolicy.workspace_id == workspace_id,
                JurisdictionPolicy.reviewed_on.is_(None),
            )
            .order_by(JurisdictionPolicy.country)
        )
        return list(rows.scalars())


# ───────────────────────────── the owner's edits ─────────────────────────────

_STRICTNESS = {
    ConsentPolicy.ALLOWED: 0,
    ConsentPolicy.REQUIRES_CONSENT: 1,
    ConsentPolicy.BLOCKED: 2,
}


@dataclass(frozen=True, slots=True)
class PolicyChange:
    """A partial edit; ``None`` leaves a field as it is. Sender identification is not editable: it is always required."""

    cold_b2b_marketing: ConsentPolicy | None = None
    cold_to_individuals: ConsentPolicy | None = None
    requires_unsubscribe: bool | None = None
    requires_postal_address: bool | None = None
    mark_reviewed: bool = True


class LooseningNotAcknowledgedError(Exception):
    """The edit makes sending *easier* and the owner has not said they understand that."""

    def __init__(self, loosened: list[str]) -> None:
        super().__init__(
            "loosening a policy needs an explicit acknowledgement: " + ", ".join(loosened)
        )
        self.loosened = loosened


def _snapshot(row: JurisdictionPolicy) -> dict[str, object]:
    return {
        "cold_b2b_marketing": row.cold_b2b_marketing.value,
        "cold_to_individuals": row.cold_to_individuals.value,
        "requires_unsubscribe": row.requires_unsubscribe,
        "requires_postal_address": row.requires_postal_address,
    }


def _loosened(row: JurisdictionPolicy, change: PolicyChange) -> list[str]:
    loosened = [
        field
        for field in ("cold_b2b_marketing", "cold_to_individuals")
        if getattr(change, field) is not None
        and _STRICTNESS[getattr(change, field)] < _STRICTNESS[getattr(row, field)]
    ]
    loosened += [
        field
        for field in ("requires_unsubscribe", "requires_postal_address")
        if getattr(change, field) is False and getattr(row, field) is True
    ]
    return loosened


async def list_policies(
    factory: async_sessionmaker[AsyncSession], clock: Clock, workspace_id: uuid.UUID
) -> list[JurisdictionPolicy]:
    """Every country's policy, the defaults seeded first if any are missing. ``ZZ`` (everywhere else) comes last."""
    await seed_default_policies(factory, clock, workspace_id)
    async with factory() as s:
        rows = (
            await s.execute(
                select(JurisdictionPolicy).where(JurisdictionPolicy.workspace_id == workspace_id)
            )
        ).scalars()
        return sorted(rows, key=lambda r: (r.country == "ZZ", r.country))


async def update_policy(
    factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    workspace_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    country: str,
    change: PolicyChange,
    acknowledged_loosening: bool,
) -> JurisdictionPolicy | None:
    """Apply an edit. Loosening (a stricter rule made weaker, or a requirement dropped) is refused unless acknowledged, and
    every applied edit leaves an audit entry with the before and after. ``None`` when the country has no policy."""
    code = country.strip().upper()
    now = clock.now()
    async with session_scope(factory) as s:
        row = (
            await s.execute(
                select(JurisdictionPolicy)
                .where(
                    JurisdictionPolicy.workspace_id == workspace_id,
                    JurisdictionPolicy.country == code,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        loosened = _loosened(row, change)
        if loosened and not acknowledged_loosening:
            raise LooseningNotAcknowledgedError(loosened)
        before = _snapshot(row)
        for field in (
            "cold_b2b_marketing",
            "cold_to_individuals",
            "requires_unsubscribe",
            "requires_postal_address",
        ):
            value = getattr(change, field)
            if value is not None:
                setattr(row, field, value)
        if change.mark_reviewed:
            row.reviewed_on = now.date()
        row.version += 1
        row.updated_at = now
        s.add(
            AuditLog(
                workspace_id=workspace_id,
                created_at=now,
                actor_type=ActorType.USER,
                actor_id=str(actor_id),
                action="policy.updated",
                entity_type="jurisdiction_policy",
                entity_id=str(row.id),
                details={
                    "country": code,
                    "before": before,
                    "after": _snapshot(row),
                    "loosened": loosened,
                },
            )
        )
        await s.flush()
        s.expunge(row)
        return row
