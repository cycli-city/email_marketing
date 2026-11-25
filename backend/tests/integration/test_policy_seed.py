"""Seeding the default country policies: complete, repeatable, per workspace, and never destructive."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FakeClock
from app.db.models import AuditLog, JurisdictionPolicy
from app.db.session import session_scope
from app.domain.enums import ActorType, ConsentPolicy
from app.domain.policy_defaults import default_policies
from app.services.outreach.policies import (
    LooseningNotAcknowledgedError,
    PolicyChange,
    list_policies,
    seed_default_policies,
    unreviewed_countries,
    update_policy,
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


async def rows(
    factory: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID
) -> dict[str, JurisdictionPolicy]:
    async with factory() as s:
        found = (
            await s.execute(
                select(JurisdictionPolicy).where(JurisdictionPolicy.workspace_id == workspace_id)
            )
        ).scalars()
        return {row.country: row for row in found}


async def test_a_fresh_workspace_gets_every_default(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, workspace_id: uuid.UUID
) -> None:
    added = await seed_default_policies(factory, clock, workspace_id)

    expected = default_policies()
    assert added == len(expected)
    stored = await rows(factory, workspace_id)
    assert set(stored) == {d.policy.country for d in expected}
    de = stored["DE"]
    assert (de.cold_b2b_marketing, de.cold_to_individuals) == (
        ConsentPolicy.REQUIRES_CONSENT,
        ConsentPolicy.REQUIRES_CONSENT,
    )
    assert de.notes
    assert de.reviewed_on is None
    assert de.created_at == clock.now()


async def test_seeding_twice_adds_nothing_the_second_time(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, workspace_id: uuid.UUID
) -> None:
    await seed_default_policies(factory, clock, workspace_id)
    assert await seed_default_policies(factory, clock, workspace_id) == 0
    async with factory() as s:
        total = (await s.execute(select(func.count()).select_from(JurisdictionPolicy))).scalar_one()
    assert total == len(default_policies())


async def test_an_edit_the_owner_made_survives_seeding(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, workspace_id: uuid.UUID
) -> None:
    await seed_default_policies(factory, clock, workspace_id)
    async with session_scope(factory) as s:
        us = (
            await s.execute(select(JurisdictionPolicy).where(JurisdictionPolicy.country == "US"))
        ).scalar_one()
        us.cold_b2b_marketing = ConsentPolicy.BLOCKED
        us.notes = "Owner decided: not now."
        us.reviewed_on = date(2026, 11, 25)
        us.version = 7

    assert await seed_default_policies(factory, clock, workspace_id) == 0

    us_after = (await rows(factory, workspace_id))["US"]
    assert us_after.cold_b2b_marketing is ConsentPolicy.BLOCKED
    assert (us_after.notes, us_after.reviewed_on, us_after.version) == (
        "Owner decided: not now.",
        date(2026, 11, 25),
        7,
    )


async def test_a_country_the_owner_deleted_comes_back_but_the_rest_is_untouched(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, workspace_id: uuid.UUID
) -> None:
    await seed_default_policies(factory, clock, workspace_id)
    async with session_scope(factory) as s:
        fr = (
            await s.execute(select(JurisdictionPolicy).where(JurisdictionPolicy.country == "FR"))
        ).scalar_one()
        await s.delete(fr)
    assert await seed_default_policies(factory, clock, workspace_id) == 1
    assert "FR" in await rows(factory, workspace_id)


async def test_workspaces_are_seeded_independently(
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
    workspace_id: uuid.UUID,
    other_workspace_id: uuid.UUID,
) -> None:
    await seed_default_policies(factory, clock, workspace_id)
    assert await rows(factory, other_workspace_id) == {}
    assert await seed_default_policies(factory, clock, other_workspace_id) == len(
        default_policies()
    )


async def test_unreviewed_countries_lists_only_what_the_owner_has_not_looked_at(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, workspace_id: uuid.UUID
) -> None:
    await seed_default_policies(factory, clock, workspace_id)
    async with session_scope(factory) as s:
        for code in ("US", "GB"):
            row = (
                await s.execute(
                    select(JurisdictionPolicy).where(JurisdictionPolicy.country == code)
                )
            ).scalar_one()
            row.reviewed_on = date(2026, 11, 25)
    pending = await unreviewed_countries(factory, workspace_id)
    assert "US" not in pending
    assert "GB" not in pending
    assert "DE" in pending
    assert pending == sorted(pending)
    assert len(pending) == len(default_policies()) - 2


# ───────────────────────────── the owner's edits ─────────────────────────────

OWNER = uuid.UUID(int=1)


async def edit(
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
    workspace_id: uuid.UUID,
    country: str,
    change: PolicyChange,
    *,
    ack: bool = False,
) -> JurisdictionPolicy | None:
    return await update_policy(
        factory,
        clock,
        workspace_id,
        actor_id=OWNER,
        country=country,
        change=change,
        acknowledged_loosening=ack,
    )


async def audits(factory: async_sessionmaker[AsyncSession]) -> list[AuditLog]:
    async with factory() as s:
        return list((await s.execute(select(AuditLog).order_by(AuditLog.created_at))).scalars())


async def test_listing_seeds_the_defaults_and_puts_everywhere_else_last(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, workspace_id: uuid.UUID
) -> None:
    listed = await list_policies(factory, clock, workspace_id)
    assert len(listed) == len(default_policies())
    assert listed[-1].country == "ZZ"
    assert [p.country for p in listed[:-1]] == sorted(p.country for p in listed[:-1])


async def test_tightening_a_policy_needs_no_acknowledgement_and_is_audited(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, workspace_id: uuid.UUID
) -> None:
    await seed_default_policies(factory, clock, workspace_id)
    clock.advance(days=2)

    row = await edit(
        factory,
        clock,
        workspace_id,
        "us",
        PolicyChange(cold_to_individuals=ConsentPolicy.REQUIRES_CONSENT),
    )

    assert row is not None
    assert row.cold_to_individuals is ConsentPolicy.REQUIRES_CONSENT
    assert row.cold_b2b_marketing is ConsentPolicy.ALLOWED  # untouched
    assert row.reviewed_on == clock.now().date()
    assert row.version == 2
    [entry] = await audits(factory)
    assert (entry.action, entry.actor_type, entry.actor_id) == (
        "policy.updated",
        ActorType.USER,
        str(OWNER),
    )
    assert entry.details["country"] == "US"
    assert entry.details["before"]["cold_to_individuals"] == "allowed"
    assert entry.details["after"]["cold_to_individuals"] == "requires_consent"
    assert entry.details["loosened"] == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (PolicyChange(cold_b2b_marketing=ConsentPolicy.ALLOWED), ["cold_b2b_marketing"]),
        (PolicyChange(cold_to_individuals=ConsentPolicy.ALLOWED), ["cold_to_individuals"]),
        (PolicyChange(requires_unsubscribe=False), ["requires_unsubscribe"]),
        (PolicyChange(requires_postal_address=False), ["requires_postal_address"]),
        (
            PolicyChange(cold_b2b_marketing=ConsentPolicy.ALLOWED, requires_postal_address=False),
            ["cold_b2b_marketing", "requires_postal_address"],
        ),
    ],
)
async def test_loosening_is_refused_until_the_owner_acknowledges_it_and_nothing_changes_meanwhile(
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
    workspace_id: uuid.UUID,
    change: PolicyChange,
    expected: list[str],
) -> None:
    await seed_default_policies(factory, clock, workspace_id)
    before = (await rows(factory, workspace_id))["DE"]  # Germany: both rules are "requires consent"

    with pytest.raises(LooseningNotAcknowledgedError) as refused:
        await edit(factory, clock, workspace_id, "DE", change)

    assert refused.value.loosened == expected
    after = (await rows(factory, workspace_id))["DE"]
    assert (after.cold_b2b_marketing, after.cold_to_individuals, after.version) == (
        before.cold_b2b_marketing,
        before.cold_to_individuals,
        before.version,
    )
    assert await audits(factory) == []

    row = await edit(factory, clock, workspace_id, "DE", change, ack=True)
    assert row is not None
    [entry] = await audits(factory)
    assert entry.details["loosened"] == expected  # the audit trail records that it was a loosening


async def test_making_a_requirement_stricter_or_keeping_it_is_not_loosening(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, workspace_id: uuid.UUID
) -> None:
    await seed_default_policies(factory, clock, workspace_id)
    row = await edit(
        factory,
        clock,
        workspace_id,
        "US",
        PolicyChange(
            requires_unsubscribe=True,
            requires_postal_address=True,
            cold_b2b_marketing=ConsentPolicy.ALLOWED,
        ),
    )
    assert row is not None


async def test_reviewing_without_changing_anything_marks_the_row_reviewed(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, workspace_id: uuid.UUID
) -> None:
    await seed_default_policies(factory, clock, workspace_id)
    row = await edit(factory, clock, workspace_id, "FR", PolicyChange())
    assert row is not None
    assert row.reviewed_on == clock.now().date()
    assert "FR" not in await unreviewed_countries(factory, workspace_id)


async def test_an_edit_can_leave_the_row_unreviewed(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, workspace_id: uuid.UUID
) -> None:
    await seed_default_policies(factory, clock, workspace_id)
    row = await edit(
        factory,
        clock,
        workspace_id,
        "FR",
        PolicyChange(requires_unsubscribe=True, mark_reviewed=False),
    )
    assert row is not None
    assert row.reviewed_on is None


async def test_an_unknown_country_is_not_found_and_not_audited(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, workspace_id: uuid.UUID
) -> None:
    await seed_default_policies(factory, clock, workspace_id)
    assert await edit(factory, clock, workspace_id, "XX", PolicyChange()) is None
    assert await audits(factory) == []


async def test_another_workspaces_policy_cannot_be_edited(
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
    workspace_id: uuid.UUID,
    other_workspace_id: uuid.UUID,
) -> None:
    await seed_default_policies(factory, clock, workspace_id)
    assert (
        await edit(
            factory, clock, other_workspace_id, "US", PolicyChange(requires_unsubscribe=True)
        )
        is None
    )
    assert (await rows(factory, workspace_id))["US"].version == 1


async def test_the_version_counts_every_applied_edit(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, workspace_id: uuid.UUID
) -> None:
    await seed_default_policies(factory, clock, workspace_id)
    for _ in range(3):
        await edit(factory, clock, workspace_id, "US", PolicyChange())
    assert (await rows(factory, workspace_id))["US"].version == 4
