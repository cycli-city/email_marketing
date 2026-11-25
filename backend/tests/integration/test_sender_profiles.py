"""Saving the sender profile against a real database: one per mode, replaced as a whole, and readiness reported honestly."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FakeClock
from app.db.models import SenderProfile
from app.domain.enums import CampaignMode
from app.services.outreach.profile import ProfileError, ProfileInput, SenderProfiles

FULL = ProfileInput(
    display_name="Alex Founder",
    postal_address="1 Example Street\nExample City",
    reply_to="founder@example.com",
    signature_text="Alex",
    website="https://example.com/about",
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def profiles(factory: async_sessionmaker[AsyncSession], clock: FakeClock) -> SenderProfiles:
    return SenderProfiles(factory, clock)


async def total(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as s:
        return (await s.execute(select(func.count()).select_from(SenderProfile))).scalar_one()


async def test_a_saved_profile_is_stored_cleaned(
    profiles: SenderProfiles, workspace_id: uuid.UUID, clock: FakeClock
) -> None:
    saved = await profiles.save(workspace_id, CampaignMode.NEXORA, FULL)
    assert (saved.display_name, saved.postal_address, saved.reply_to, saved.website) == (
        "Alex Founder",
        "1 Example Street\nExample City",
        "founder@example.com",
        "https://example.com",
    )
    assert saved.created_at == clock.now()
    stored = await profiles.get(workspace_id, CampaignMode.NEXORA)
    assert stored is not None
    assert stored.id == saved.id


async def test_saving_again_replaces_the_whole_profile_instead_of_adding_one(
    profiles: SenderProfiles,
    workspace_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
) -> None:
    first = await profiles.save(workspace_id, CampaignMode.NEXORA, FULL)
    clock.advance(hours=1)
    second = await profiles.save(
        workspace_id, CampaignMode.NEXORA, ProfileInput(display_name="Alex F.")
    )

    assert await total(factory) == 1
    assert second.id == first.id
    assert (second.display_name, second.postal_address, second.reply_to, second.website) == (
        "Alex F.",
        None,
        None,
        None,
    )
    assert second.updated_at == clock.now()
    assert second.created_at == first.created_at


async def test_each_mode_has_its_own_profile(
    profiles: SenderProfiles, workspace_id: uuid.UUID
) -> None:
    await profiles.save(workspace_id, CampaignMode.NEXORA, FULL)
    await profiles.save(
        workspace_id, CampaignMode.JOB_HUNTER, ProfileInput(display_name="Jo Applicant")
    )
    nexora = await profiles.get(workspace_id, CampaignMode.NEXORA)
    job = await profiles.get(workspace_id, CampaignMode.JOB_HUNTER)
    assert nexora is not None
    assert job is not None
    assert (nexora.display_name, job.display_name) == ("Alex Founder", "Jo Applicant")


async def test_workspaces_never_see_each_others_profile(
    profiles: SenderProfiles, workspace_id: uuid.UUID, other_workspace_id: uuid.UUID
) -> None:
    await profiles.save(workspace_id, CampaignMode.NEXORA, FULL)
    assert await profiles.get(other_workspace_id, CampaignMode.NEXORA) is None


async def test_an_invalid_profile_is_refused_and_nothing_is_stored(
    profiles: SenderProfiles, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    with pytest.raises(ProfileError):
        await profiles.save(
            workspace_id, CampaignMode.NEXORA, ProfileInput(display_name="x", reply_to="bad")
        )
    assert await total(factory) == 0


async def test_an_invalid_edit_leaves_the_existing_profile_untouched(
    profiles: SenderProfiles, workspace_id: uuid.UUID
) -> None:
    await profiles.save(workspace_id, CampaignMode.NEXORA, FULL)
    with pytest.raises(ProfileError):
        await profiles.save(
            workspace_id, CampaignMode.NEXORA, ProfileInput(display_name="Alex", reply_to="bad")
        )
    kept = await profiles.get(workspace_id, CampaignMode.NEXORA)
    assert kept is not None
    assert kept.reply_to == "founder@example.com"


async def test_marketing_is_not_ready_without_a_name_and_a_postal_address(
    profiles: SenderProfiles, workspace_id: uuid.UUID
) -> None:
    assert await profiles.missing(workspace_id, CampaignMode.NEXORA) == [
        "the sender's name",
        "the sender's postal address",
    ]
    await profiles.save(
        workspace_id, CampaignMode.NEXORA, ProfileInput(display_name="Alex Founder")
    )
    assert await profiles.missing(workspace_id, CampaignMode.NEXORA) == [
        "the sender's postal address"
    ]
    await profiles.save(workspace_id, CampaignMode.NEXORA, FULL)
    assert await profiles.missing(workspace_id, CampaignMode.NEXORA) == []


async def test_a_job_application_needs_only_a_name(
    profiles: SenderProfiles, workspace_id: uuid.UUID
) -> None:
    assert await profiles.missing(workspace_id, CampaignMode.JOB_HUNTER) == ["the sender's name"]
    await profiles.save(
        workspace_id, CampaignMode.JOB_HUNTER, ProfileInput(display_name="Jo Applicant")
    )
    assert await profiles.missing(workspace_id, CampaignMode.JOB_HUNTER) == []
