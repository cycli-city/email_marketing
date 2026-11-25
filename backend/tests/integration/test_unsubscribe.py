"""Acting on an unsubscribe link: the address is never mailed again, and everything queued for it stops."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FakeClock
from app.db.models import (
    ActivityEvent,
    AuditLog,
    Campaign,
    EmailMessage,
    Lead,
    SuppressionEntry,
    Workspace,
)
from app.db.session import session_scope
from app.domain.enums import (
    FollowUpStatus,
    LeadStage,
    MessageKind,
    MessageStatus,
    SuppressionReason,
)
from app.services.outreach.unsubscribe import Target, UnsubscribeService
from tests.factories import make_campaign, make_company, make_contact, make_lead, make_workspace

ADDRESS = "pat@example.com"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def service(factory: async_sessionmaker[AsyncSession], clock: FakeClock) -> UnsubscribeService:
    return UnsubscribeService(factory, clock)


def new_message(
    workspace_id: uuid.UUID,
    to: str,
    status: MessageStatus,
    *,
    campaign_id: uuid.UUID | None = None,
    lead_id: uuid.UUID | None = None,
    step: int = 0,
) -> EmailMessage:
    token = uuid.uuid4().hex
    return EmailMessage(
        workspace_id=workspace_id,
        campaign_id=campaign_id,
        lead_id=lead_id,
        kind=MessageKind.INITIAL,
        step_no=step,
        status=status,
        to_email=to,
        subject="Hello",
        body_text="Hi there",
        idempotency_key=token,
        rfc_message_id=f"<{token}@example.com>",
    )


class World:
    """One workspace with a campaign, a company, a contact (the unsubscriber) and one lead for them."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], workspace_id: uuid.UUID) -> None:
        self.factory = factory
        self.workspace_id = workspace_id
        self.campaign_id: uuid.UUID
        self.company_id: uuid.UUID
        self.contact_id: uuid.UUID
        self.lead_id: uuid.UUID

    async def add(
        self, to: str, status: MessageStatus, *, step: int = 0, lead: bool = True
    ) -> uuid.UUID:
        async with session_scope(self.factory) as s:
            message = new_message(
                self.workspace_id,
                to,
                status,
                campaign_id=self.campaign_id,
                lead_id=self.lead_id if lead else None,
                step=step,
            )
            s.add(message)
            await s.flush()
            return message.id

    async def status(self, message_id: uuid.UUID) -> MessageStatus:
        async with self.factory() as s:
            row = await s.get(EmailMessage, message_id)
            assert row is not None
            return row.status

    async def lead(self) -> Lead:
        async with self.factory() as s:
            row = await s.get(Lead, self.lead_id)
            assert row is not None
            return row


@pytest.fixture
async def world(factory: async_sessionmaker[AsyncSession]) -> World:
    async with session_scope(factory) as s:
        ws = await make_workspace(s)
        campaign = await make_campaign(s, ws)
        company = await make_company(s, ws)
        contact = await make_contact(s, ws, company, email=ADDRESS)
        lead = await make_lead(s, ws, campaign, company)
        lead.primary_contact_id = contact.id
        lead.stage = LeadStage.SENT
        lead.followup_status = FollowUpStatus.SCHEDULED
        built = World(factory, ws.id)
        built.campaign_id, built.company_id = campaign.id, company.id
        built.contact_id, built.lead_id = contact.id, lead.id
    return built


async def suppressions(factory: async_sessionmaker[AsyncSession]) -> list[SuppressionEntry]:
    async with factory() as s:
        return list((await s.execute(select(SuppressionEntry))).scalars())


async def count(factory: async_sessionmaker[AsyncSession], model: type) -> int:
    async with factory() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_the_address_is_put_on_the_do_not_contact_list(
    service: UnsubscribeService,
    world: World,
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
) -> None:
    sent = await world.add(ADDRESS, MessageStatus.SENT)

    outcome = await service.unsubscribe(Target(world.workspace_id, sent))

    assert outcome is not None
    assert outcome.already is False
    [entry] = await suppressions(factory)
    assert (entry.email, entry.reason, entry.source) == (
        ADDRESS,
        SuppressionReason.UNSUBSCRIBE,
        "unsubscribe_link",
    )
    assert entry.expires_at is None  # an opt-out never lapses
    assert entry.workspace_id == world.workspace_id
    assert entry.created_at == clock.now()


async def test_everything_not_yet_sent_to_that_address_is_cancelled_and_nothing_else(
    service: UnsubscribeService, world: World
) -> None:
    sent = await world.add(ADDRESS, MessageStatus.SENT, step=0)
    pending = [
        await world.add(ADDRESS, MessageStatus.DRAFT, step=1),
        await world.add(ADDRESS, MessageStatus.PENDING_APPROVAL, step=2),
        await world.add(ADDRESS, MessageStatus.APPROVED, step=3),
        await world.add(ADDRESS, MessageStatus.QUEUED, step=4),
    ]
    failed = await world.add(ADDRESS, MessageStatus.FAILED, step=5)
    someone_else = await world.add("other@example.com", MessageStatus.QUEUED, step=6)

    outcome = await service.unsubscribe(Target(world.workspace_id, sent))

    assert outcome is not None
    assert outcome.cancelled_messages == 4
    assert [await world.status(m) for m in pending] == [MessageStatus.CANCELLED] * 4
    assert await world.status(sent) is MessageStatus.SENT  # history is never rewritten
    assert await world.status(failed) is MessageStatus.FAILED
    assert await world.status(someone_else) is MessageStatus.QUEUED


async def test_messages_to_a_differently_capitalised_spelling_are_cancelled_too(
    service: UnsubscribeService, world: World, factory: async_sessionmaker[AsyncSession]
) -> None:
    sent = await world.add("Pat@Example.COM", MessageStatus.SENT)
    queued = await world.add("PAT@example.com", MessageStatus.QUEUED, step=1)

    await service.unsubscribe(Target(world.workspace_id, sent))

    assert await world.status(queued) is MessageStatus.CANCELLED
    assert [e.email for e in await suppressions(factory)] == [ADDRESS]  # stored in its normal form


async def test_the_lead_stops_and_its_follow_ups_with_it(
    service: UnsubscribeService, world: World
) -> None:
    sent = await world.add(ADDRESS, MessageStatus.SENT)
    await service.unsubscribe(Target(world.workspace_id, sent))
    lead = await world.lead()
    assert lead.stage is LeadStage.UNSUBSCRIBED
    assert lead.followup_status is FollowUpStatus.STOPPED
    assert lead.stop_reason == "the recipient unsubscribed"


async def test_a_lead_for_the_same_contact_in_another_campaign_stops_too(
    service: UnsubscribeService, world: World, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_scope(factory) as s:
        first = await s.get(Campaign, world.campaign_id)
        assert first is not None
        second_campaign = Campaign(
            workspace_id=world.workspace_id, name=f"Second {uuid.uuid4().hex[:6]}", mode=first.mode
        )
        s.add(second_campaign)
        await s.flush()
        second = Lead(
            workspace_id=world.workspace_id,
            campaign_id=second_campaign.id,
            company_id=world.company_id,
            primary_contact_id=world.contact_id,
            stage=LeadStage.QUALIFIED,
        )
        s.add(second)
        await s.flush()
        second_id = second.id
    sent = await world.add(ADDRESS, MessageStatus.SENT)

    await service.unsubscribe(Target(world.workspace_id, sent))

    async with factory() as s:
        row = await s.get(Lead, second_id)
        assert row is not None
        assert row.stage is LeadStage.UNSUBSCRIBED


@pytest.mark.parametrize(
    "final", [LeadStage.SUPPRESSED, LeadStage.DUPLICATE, LeadStage.UNSUBSCRIBED]
)
async def test_a_lead_that_is_already_finished_keeps_its_stage(
    service: UnsubscribeService,
    world: World,
    factory: async_sessionmaker[AsyncSession],
    final: LeadStage,
) -> None:
    async with session_scope(factory) as s:
        lead = await s.get(Lead, world.lead_id)
        assert lead is not None
        lead.stage = final
    sent = await world.add(ADDRESS, MessageStatus.SENT)

    await service.unsubscribe(Target(world.workspace_id, sent))

    assert (await world.lead()).stage is final


async def test_other_contacts_leads_are_untouched(
    service: UnsubscribeService, world: World, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_scope(factory) as s:
        ws = await s.get(Workspace, world.workspace_id)
        assert ws is not None
        company = await make_company(s, ws, name="Another Clinic")
        bystander = await make_contact(s, ws, company, email="bystander@example.com")
        other = Lead(
            workspace_id=world.workspace_id,
            campaign_id=world.campaign_id,
            company_id=company.id,
            primary_contact_id=bystander.id,
            stage=LeadStage.SENT,
            followup_status=FollowUpStatus.SCHEDULED,
        )
        s.add(other)
        await s.flush()
        other_id = other.id
    sent = await world.add(ADDRESS, MessageStatus.SENT)

    await service.unsubscribe(Target(world.workspace_id, sent))

    async with factory() as s:
        row = await s.get(Lead, other_id)
        assert row is not None
        assert (row.stage, row.followup_status) == (LeadStage.SENT, FollowUpStatus.SCHEDULED)


async def test_clicking_twice_changes_nothing_the_second_time(
    service: UnsubscribeService, world: World, factory: async_sessionmaker[AsyncSession]
) -> None:
    sent = await world.add(ADDRESS, MessageStatus.SENT)
    queued = await world.add(ADDRESS, MessageStatus.QUEUED, step=1)

    first = await service.unsubscribe(Target(world.workspace_id, sent))
    again = await service.unsubscribe(Target(world.workspace_id, sent))

    assert first is not None
    assert again is not None
    assert (first.already, again.already) == (False, True)
    assert again.cancelled_messages == 0
    assert len(await suppressions(factory)) == 1
    assert await world.status(queued) is MessageStatus.CANCELLED
    assert await count(factory, ActivityEvent) == 1  # one opt-out, one entry in the feed
    assert await count(factory, AuditLog) == 1


async def test_a_message_queued_after_the_opt_out_is_cancelled_by_a_later_click(
    service: UnsubscribeService, world: World
) -> None:
    sent = await world.add(ADDRESS, MessageStatus.SENT)
    await service.unsubscribe(Target(world.workspace_id, sent))
    late = await world.add(ADDRESS, MessageStatus.QUEUED, step=1)  # something queued it anyway

    again = await service.unsubscribe(Target(world.workspace_id, sent))

    assert again is not None
    assert again.cancelled_messages == 1
    assert await world.status(late) is MessageStatus.CANCELLED


async def test_simultaneous_clicks_record_the_opt_out_exactly_once(
    service: UnsubscribeService, world: World, factory: async_sessionmaker[AsyncSession]
) -> None:
    sent = await world.add(ADDRESS, MessageStatus.SENT)
    outcomes = await asyncio.gather(
        *(service.unsubscribe(Target(world.workspace_id, sent)) for _ in range(6))
    )

    assert len(await suppressions(factory)) == 1
    assert sum(1 for o in outcomes if o is not None and not o.already) == 1
    assert await count(factory, ActivityEvent) == 1


async def test_an_unknown_message_unsubscribes_nobody(
    service: UnsubscribeService, world: World, factory: async_sessionmaker[AsyncSession]
) -> None:
    assert await service.unsubscribe(Target(world.workspace_id, uuid.uuid4())) is None
    assert await suppressions(factory) == []


async def test_a_link_cannot_be_used_against_another_workspace(
    service: UnsubscribeService,
    world: World,
    other_workspace_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sent = await world.add(ADDRESS, MessageStatus.SENT)
    queued = await world.add(ADDRESS, MessageStatus.QUEUED, step=1)

    assert await service.unsubscribe(Target(other_workspace_id, sent)) is None

    assert await suppressions(factory) == []
    assert await world.status(queued) is MessageStatus.QUEUED


async def test_the_same_address_in_another_workspace_is_unaffected(
    service: UnsubscribeService,
    world: World,
    other_workspace_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(factory) as s:
        theirs = new_message(other_workspace_id, ADDRESS, MessageStatus.QUEUED)
        s.add(theirs)
        await s.flush()
        theirs_id = theirs.id
    sent = await world.add(ADDRESS, MessageStatus.SENT)

    await service.unsubscribe(Target(world.workspace_id, sent))

    async with factory() as s:
        theirs_row = await s.get(EmailMessage, theirs_id)
        assert theirs_row is not None
        assert theirs_row.status is MessageStatus.QUEUED
    assert [e.workspace_id for e in await suppressions(factory)] == [world.workspace_id]


async def test_the_feed_and_the_audit_log_never_contain_the_address(
    service: UnsubscribeService, world: World, factory: async_sessionmaker[AsyncSession]
) -> None:
    sent = await world.add(ADDRESS, MessageStatus.SENT)
    await service.unsubscribe(Target(world.workspace_id, sent))
    async with factory() as s:
        events = (await s.execute(select(ActivityEvent))).scalars().all()
        audits = (await s.execute(select(AuditLog))).scalars().all()
    text = json.dumps(
        [[e.message, e.data] for e in events] + [[a.details, a.entity_id] for a in audits]
    )
    assert ADDRESS not in text
    assert str(sent) in text  # but the message that led to it is identifiable
    [event] = events
    assert event.campaign_id == world.campaign_id
    assert event.lead_id == world.lead_id


async def test_the_lead_a_message_belongs_to_stops_even_if_its_primary_contact_is_someone_else(
    service: UnsubscribeService, world: World, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_scope(factory) as s:
        ws = await s.get(Workspace, world.workspace_id)
        assert ws is not None
        colleague = await make_contact(s, ws, None, email="colleague@example.com")
        lead = await s.get(Lead, world.lead_id)
        assert lead is not None
        lead.primary_contact_id = colleague.id  # the contact was changed after the message went out
    sent = await world.add(ADDRESS, MessageStatus.SENT)

    await service.unsubscribe(Target(world.workspace_id, sent))

    assert (await world.lead()).stage is LeadStage.UNSUBSCRIBED
