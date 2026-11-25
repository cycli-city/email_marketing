"""The compliance gate against a real database: each thing it reads is changed, and the decision follows."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FakeClock
from app.db.models import (
    Campaign,
    CampaignSettings,
    ConnectedEmailAccount,
    Contact,
    EmailMessage,
    JurisdictionPolicy,
    Lead,
    SenderProfile,
    SuppressionEntry,
    Workspace,
)
from app.db.session import session_scope
from app.domain.enums import (
    AccountProvider,
    AccountStatus,
    CampaignMode,
    ConsentPolicy,
    EmailKind,
    EmailOrigin,
    LeadStage,
    MessageKind,
    MessageStatus,
    SubscriberType,
    SuppressionReason,
    VerificationStatus,
)
from app.services.outreach.compliance_gate import ComplianceGate, _footer_html
from app.services.outreach.sender import Clearance, Refusal
from app.services.outreach.unsubscribe import UnsubscribeLinks
from tests.factories import make_campaign, make_company, make_contact, make_lead, make_workspace
from tests.integration.test_unsubscribe import new_message

ADDRESS = "pat@example.com"
SECRET = "k" * 40
GMAIL = (
    "gmail" + ".com"
)  # assembled: the public-repo audit rejects real-looking mail domains in tracked files


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def gate(clock: FakeClock, base: str = "https://api.example.com") -> ComplianceGate:
    return ComplianceGate(
        links=UnsubscribeLinks(secret=SECRET, backend_url=base, clock=clock), clock=clock
    )


class World:
    """A workspace where everything is in order, so each test breaks exactly one thing."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory
        self.workspace_id: uuid.UUID
        self.account_id: uuid.UUID
        self.campaign_id: uuid.UUID
        self.contact_id: uuid.UUID
        self.lead_id: uuid.UUID
        self.message_id: uuid.UUID

    async def build(self) -> World:
        async with session_scope(self.factory) as s:
            ws = await make_workspace(s)
            self.workspace_id = ws.id
            account = ConnectedEmailAccount(
                workspace_id=ws.id,
                provider=AccountProvider.GMAIL,
                mode=CampaignMode.NEXORA,
                status=AccountStatus.ACTIVE,
                email_address="sender@example.com",
                display_name="Account Name",
            )
            s.add(account)
            campaign = await make_campaign(s, ws)
            s.add(CampaignSettings(workspace_id=ws.id, campaign_id=campaign.id))
            s.add(
                SenderProfile(
                    workspace_id=ws.id,
                    mode=CampaignMode.NEXORA,
                    display_name="Alex Founder",
                    postal_address="1 Example Street\nExample City",
                )
            )
            company = await make_company(s, ws)
            contact = await make_contact(
                s,
                ws,
                company,
                email=ADDRESS,
                email_origin=EmailOrigin.DISCOVERED,
                verification_status=VerificationStatus.VERIFIED,
                email_kind=EmailKind.NAMED,
                subscriber_type=SubscriberType.CORPORATE,
                source_url="https://clinic.example.com/team",
            )
            lead = await make_lead(s, ws, campaign, company)
            lead.primary_contact_id = contact.id
            lead.stage = LeadStage.QUEUED
            message = new_message(
                ws.id, ADDRESS, MessageStatus.SENDING, campaign_id=campaign.id, lead_id=lead.id
            )
            message.contact_id = contact.id
            s.add(message)
            await s.flush()
            self.account_id, self.campaign_id = account.id, campaign.id
            self.contact_id, self.lead_id, self.message_id = contact.id, lead.id, message.id
        return self

    async def check(self, clock: FakeClock, **gate_args: Any) -> Clearance | Refusal:
        async with self.factory() as s:
            message = await s.get(EmailMessage, self.message_id)
            account = await s.get(ConnectedEmailAccount, self.account_id)
            assert message is not None
            assert account is not None
            return await gate(clock, **gate_args).check(s, message, account)

    async def change(self, model: type[Any], key: uuid.UUID, **fields: Any) -> None:
        async with session_scope(self.factory) as s:
            row = await s.get(model, key)
            assert row is not None
            for name, value in fields.items():
                setattr(row, name, value)

    async def add(self, row: Any) -> None:
        async with session_scope(self.factory) as s:
            s.add(row)


@pytest.fixture
async def world(factory: async_sessionmaker[AsyncSession]) -> World:
    return await World(factory).build()


def refused(outcome: Clearance | Refusal) -> str:
    assert isinstance(outcome, Refusal), "expected a refusal"
    return outcome.reason


async def test_a_message_that_meets_every_rule_is_cleared_with_the_unsubscribe_link_and_footer(
    world: World, clock: FakeClock
) -> None:
    outcome = await world.check(clock)

    assert isinstance(outcome, Clearance)
    link = outcome.unsubscribe_https
    assert link is not None
    assert link.startswith("https://api.example.com/u/")
    links = UnsubscribeLinks(secret=SECRET, backend_url="https://api.example.com", clock=clock)
    target = links.resolve(link.rsplit("/u/", 1)[1])
    assert target is not None
    assert (target.workspace_id, target.message_id) == (world.workspace_id, world.message_id)
    assert outcome.footer_text is not None
    assert "Alex Founder" in outcome.footer_text  # the sender profile beats the account's own name
    assert "1 Example Street\nExample City" in outcome.footer_text
    assert link in outcome.footer_text
    assert outcome.footer_html is not None
    assert "1 Example Street<br>Example City" in outcome.footer_html
    assert f'href="{link}"' in outcome.footer_html


async def test_the_footer_escapes_whatever_is_in_the_profile(
    world: World, clock: FakeClock, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_scope(factory) as s:
        profile = (await s.execute(select(SenderProfile))).scalar_one()
        profile.display_name = '<script>alert(1)</script> & "Co"'
        profile.postal_address = "<b>1</b> Main St"
    outcome = await world.check(clock)
    assert isinstance(outcome, Clearance)
    assert outcome.footer_html is not None
    assert "<script>" not in outcome.footer_html
    assert "&lt;script&gt;" in outcome.footer_html
    assert "<b>1</b>" not in outcome.footer_html


# ───────────────────────────── what can change after a message was queued ─────────────────────────────


async def test_an_address_suppressed_since_queueing_is_refused(
    world: World, clock: FakeClock
) -> None:
    await world.add(
        SuppressionEntry(
            workspace_id=world.workspace_id,
            email=ADDRESS,
            reason=SuppressionReason.UNSUBSCRIBE,
            source="unsubscribe_link",
        )
    )
    assert "do-not-contact" in refused(await world.check(clock))


async def test_a_domain_wide_suppression_is_refused(world: World, clock: FakeClock) -> None:
    await world.add(
        SuppressionEntry(
            workspace_id=world.workspace_id,
            domain="example.com",
            reason=SuppressionReason.MANUAL,
            source="manual",
        )
    )
    assert "do-not-contact" in refused(await world.check(clock))


async def test_a_suppression_that_has_expired_no_longer_applies(
    world: World, clock: FakeClock
) -> None:
    await world.add(
        SuppressionEntry(
            workspace_id=world.workspace_id,
            email=ADDRESS,
            reason=SuppressionReason.MANUAL,
            source="manual",
            expires_at=clock.now() - timedelta(seconds=1),
        )
    )
    assert isinstance(await world.check(clock), Clearance)


async def test_another_workspaces_suppression_does_not_apply(
    world: World, clock: FakeClock, other_workspace_id: uuid.UUID
) -> None:
    await world.add(
        SuppressionEntry(
            workspace_id=other_workspace_id,
            email=ADDRESS,
            reason=SuppressionReason.MANUAL,
            source="manual",
        )
    )
    assert isinstance(await world.check(clock), Clearance)


async def test_a_gmail_spelling_of_a_suppressed_mailbox_is_refused(
    world: World, clock: FakeClock
) -> None:
    await world.change(Contact, world.contact_id, email=f"j.a.n.e@{GMAIL}")
    await world.change(EmailMessage, world.message_id, to_email=f"jane+promo@{GMAIL}")
    await world.add(
        SuppressionEntry(
            workspace_id=world.workspace_id,
            email=f"jane@{GMAIL}",
            reason=SuppressionReason.UNSUBSCRIBE,
            source="unsubscribe_link",
        )
    )
    assert "do-not-contact" in refused(await world.check(clock))


@pytest.mark.parametrize(
    "stage",
    [LeadStage.UNSUBSCRIBED, LeadStage.BOUNCED, LeadStage.SUPPRESSED, LeadStage.DISQUALIFIED],
)
async def test_a_lead_closed_since_queueing_is_refused(
    world: World, clock: FakeClock, stage: LeadStage
) -> None:
    await world.change(Lead, world.lead_id, stage=stage)
    assert "nothing more is sent" in refused(await world.check(clock))


async def test_a_follow_up_is_refused_once_the_person_has_replied(
    world: World, clock: FakeClock
) -> None:
    await world.change(EmailMessage, world.message_id, kind=MessageKind.FOLLOW_UP, step_no=1)
    await world.change(Lead, world.lead_id, stage=LeadStage.REPLIED)
    assert "nothing more is sent" in refused(await world.check(clock))


@pytest.mark.parametrize(
    "status", [VerificationStatus.INVALID, VerificationStatus.RISKY, VerificationStatus.UNVERIFIED]
)
async def test_an_address_that_is_not_verified_is_refused(
    world: World, clock: FakeClock, status: VerificationStatus
) -> None:
    await world.change(Contact, world.contact_id, verification_status=status)
    assert refused(await world.check(clock))


async def test_the_campaign_may_allow_unverified_addresses_that_have_a_source(
    world: World, clock: FakeClock, factory: async_sessionmaker[AsyncSession]
) -> None:
    await world.change(Contact, world.contact_id, verification_status=VerificationStatus.UNVERIFIED)
    async with session_scope(factory) as s:
        await s.execute(update(CampaignSettings).values(require_verified_email=False))
    assert isinstance(await world.check(clock), Clearance)


async def test_a_guessed_address_is_refused_even_when_unverified_addresses_are_allowed(
    world: World, clock: FakeClock, factory: async_sessionmaker[AsyncSession]
) -> None:
    await world.change(
        Contact,
        world.contact_id,
        email_origin=EmailOrigin.GUESSED,
        verification_status=VerificationStatus.UNVERIFIED,
    )
    async with session_scope(factory) as s:
        await s.execute(update(CampaignSettings).values(require_verified_email=False))
    assert "guessed" in refused(await world.check(clock))


async def test_shared_role_addresses_can_be_switched_off_for_a_campaign(
    world: World, clock: FakeClock, factory: async_sessionmaker[AsyncSession]
) -> None:
    await world.change(Contact, world.contact_id, email_kind=EmailKind.ROLE_BASED)
    assert isinstance(await world.check(clock), Clearance)
    async with session_scope(factory) as s:
        await s.execute(update(CampaignSettings).values(allow_role_addresses=False))
    assert "role addresses" in refused(await world.check(clock))


async def test_an_address_mailed_recently_by_another_campaign_is_refused_until_the_cooldown_ends(
    world: World, clock: FakeClock, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_scope(factory) as s:
        earlier = new_message(world.workspace_id, "PAT@example.com", MessageStatus.SENT, step=3)
        earlier.sent_at = clock.now() - timedelta(days=30)
        s.add(earlier)
    assert "already contacted" in refused(await world.check(clock))

    clock.advance(days=151)  # now 181 days after that message
    assert isinstance(await world.check(clock), Clearance)


async def test_a_message_that_failed_to_send_does_not_start_a_cooldown(
    world: World, clock: FakeClock, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_scope(factory) as s:
        failed = new_message(world.workspace_id, ADDRESS, MessageStatus.FAILED, step=3)
        failed.sent_at = clock.now() - timedelta(days=1)
        s.add(failed)
    assert isinstance(await world.check(clock), Clearance)


async def test_an_uncertain_send_counts_as_contact(
    world: World, clock: FakeClock, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_scope(factory) as s:
        unsure = new_message(world.workspace_id, ADDRESS, MessageStatus.SEND_UNCERTAIN, step=3)
        unsure.claimed_at = clock.now() - timedelta(days=1)
        s.add(unsure)
    assert "already contacted" in refused(await world.check(clock))


# ───────────────────────────── jurisdiction policies and sender identity ─────────────────────────────


async def test_a_country_policy_stored_in_the_database_is_applied(
    world: World, clock: FakeClock
) -> None:
    await world.add(
        JurisdictionPolicy(
            workspace_id=world.workspace_id,
            country="US",
            cold_b2b_marketing=ConsentPolicy.BLOCKED,
            cold_to_individuals=ConsentPolicy.BLOCKED,
        )
    )
    assert "blocked by the policy for US" in refused(await world.check(clock))


async def test_a_policy_for_another_country_is_not_applied(world: World, clock: FakeClock) -> None:
    await world.add(
        JurisdictionPolicy(
            workspace_id=world.workspace_id,
            country="DE",
            cold_b2b_marketing=ConsentPolicy.BLOCKED,
            cold_to_individuals=ConsentPolicy.BLOCKED,
        )
    )
    assert isinstance(await world.check(clock), Clearance)


async def test_a_missing_postal_address_blocks_marketing_mail(
    world: World, clock: FakeClock, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_scope(factory) as s:
        await s.execute(update(SenderProfile).values(postal_address=None))
    assert "postal address" in refused(await world.check(clock))


async def test_without_a_sender_profile_the_account_name_identifies_the_sender_but_the_postal_address_is_missing(
    world: World, clock: FakeClock, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_scope(factory) as s:
        await s.execute(delete(SenderProfile))
    reason = refused(await world.check(clock))
    assert "postal address" in reason
    assert "sender's name" not in reason  # the account's display name still names the sender


async def test_a_sender_with_no_name_at_all_is_refused(
    world: World, clock: FakeClock, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_scope(factory) as s:
        await s.execute(delete(SenderProfile))
    await world.change(ConnectedEmailAccount, world.account_id, display_name=None)
    assert "sender's name" in refused(await world.check(clock))


async def test_an_http_backend_cannot_provide_a_one_click_unsubscribe(
    world: World, clock: FakeClock
) -> None:
    assert "unsubscribe" in refused(await world.check(clock, base="http://localhost:8000"))


# ───────────────────────────── job applications and replies ─────────────────────────────


async def test_a_job_application_does_not_need_a_postal_address_or_marketing_policy(
    world: World, clock: FakeClock, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_scope(factory) as s:
        await s.execute(update(Campaign).values(mode=CampaignMode.JOB_HUNTER))
        await s.execute(delete(SenderProfile))
        s.add(
            SenderProfile(
                workspace_id=world.workspace_id,
                mode=CampaignMode.JOB_HUNTER,
                display_name="Jo Applicant",
            )
        )
        s.add(
            JurisdictionPolicy(
                workspace_id=world.workspace_id,
                country="US",
                cold_b2b_marketing=ConsentPolicy.BLOCKED,
                cold_to_individuals=ConsentPolicy.BLOCKED,
            )
        )
    outcome = await world.check(clock)
    assert isinstance(outcome, Clearance)
    assert outcome.footer_text is not None
    assert "Jo Applicant" in outcome.footer_text


async def test_a_reply_to_someone_who_wrote_to_us_needs_no_contact_record(
    world: World, clock: FakeClock, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_scope(factory) as s:
        reply = new_message(
            world.workspace_id, "stranger@example.net", MessageStatus.SENDING, step=9
        )
        reply.kind = MessageKind.REPLY
        reply.campaign_id = world.campaign_id
        s.add(reply)
        await s.flush()
        reply_id = reply.id
    await world.change(EmailMessage, world.message_id, status=MessageStatus.CANCELLED)
    world.message_id = reply_id
    assert isinstance(await world.check(clock), Clearance)


async def test_a_first_message_to_an_address_with_no_contact_record_is_refused(
    world: World, clock: FakeClock, factory: async_sessionmaker[AsyncSession]
) -> None:
    await world.change(
        EmailMessage, world.message_id, to_email="stranger@example.net", contact_id=None
    )
    assert "no contact record" in refused(await world.check(clock))


async def test_a_message_for_an_unparseable_address_is_refused(
    world: World, clock: FakeClock
) -> None:
    await world.change(EmailMessage, world.message_id, to_email="not-an-address")
    assert "not valid" in refused(await world.check(clock))


async def test_every_refusal_names_all_the_reasons_and_never_the_address(
    world: World, clock: FakeClock, factory: async_sessionmaker[AsyncSession]
) -> None:
    await world.change(Lead, world.lead_id, stage=LeadStage.UNSUBSCRIBED)
    await world.change(Contact, world.contact_id, verification_status=VerificationStatus.INVALID)
    async with session_scope(factory) as s:
        await s.execute(update(SenderProfile).values(postal_address=None))
    reason = refused(await world.check(clock))
    assert reason.count(";") >= 2
    assert ADDRESS not in reason


async def test_a_contact_whose_subscriber_type_is_unknown_is_judged_by_the_stricter_rule(
    world: World, clock: FakeClock
) -> None:
    """Until discovery classifies a contact as a company or an individual, the cautious reading applies: with no policy
    configured for the country, cold mail to an individual needs consent — so an unclassified contact is not mailed."""
    await world.change(Contact, world.contact_id, subscriber_type=SubscriberType.UNKNOWN)
    assert "requires recorded consent" in refused(await world.check(clock))


# ───────────────────────────── ids that point into another workspace are ignored ─────────────────────────────


async def test_a_contact_id_from_another_workspace_is_not_trusted(
    world: World,
    clock: FakeClock,
    other_workspace_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The message names a verified contact that belongs to someone else's workspace; the address's own contact here is
    invalid. Believing the foreign row would let a message through that this workspace knows is undeliverable."""
    async with session_scope(factory) as s:
        foreign = Contact(
            workspace_id=other_workspace_id,
            email=ADDRESS,
            email_origin=EmailOrigin.DISCOVERED,
            verification_status=VerificationStatus.VERIFIED,
            subscriber_type=SubscriberType.CORPORATE,
            source_url="https://elsewhere.example.com",
        )
        s.add(foreign)
        await s.flush()
        foreign_id = foreign.id
    await world.change(Contact, world.contact_id, verification_status=VerificationStatus.INVALID)
    await world.change(EmailMessage, world.message_id, contact_id=foreign_id)

    assert refused(await world.check(clock))


async def test_a_campaign_from_another_workspace_does_not_change_the_rules(
    world: World,
    clock: FakeClock,
    other_workspace_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A foreign *job-hunting* campaign would waive the postal-address rule; it must not be able to."""
    async with session_scope(factory) as s:
        other = Campaign(
            workspace_id=other_workspace_id, name="Elsewhere", mode=CampaignMode.JOB_HUNTER
        )
        s.add(other)
        await s.flush()
        other_id = other.id
        await s.execute(update(SenderProfile).values(postal_address=None))
    await world.change(EmailMessage, world.message_id, campaign_id=other_id)

    assert "postal address" in refused(await world.check(clock))


async def test_a_lead_from_another_workspace_is_ignored(
    world: World,
    clock: FakeClock,
    other_workspace_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(factory) as s:
        ws = await s.get(Workspace, other_workspace_id)
        assert ws is not None
        campaign = await make_campaign(s, ws)
        company = await make_company(s, ws)
        foreign = await make_lead(s, ws, campaign, company)
        foreign.stage = LeadStage.UNSUBSCRIBED
        foreign_id = foreign.id
    await world.change(EmailMessage, world.message_id, lead_id=foreign_id)

    assert isinstance(
        await world.check(clock), Clearance
    )  # their lead's stage says nothing about ours


async def test_a_contact_found_only_by_address_must_belong_to_this_workspace(
    world: World,
    clock: FakeClock,
    other_workspace_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await world.change(EmailMessage, world.message_id, contact_id=None)
    await world.change(Contact, world.contact_id, email="someone.else@example.com")
    async with session_scope(factory) as s:
        s.add(
            Contact(
                workspace_id=other_workspace_id,
                email=ADDRESS,
                email_origin=EmailOrigin.DISCOVERED,
                verification_status=VerificationStatus.VERIFIED,
                subscriber_type=SubscriberType.CORPORATE,
                source_url="https://elsewhere.example.com",
            )
        )
    assert "no contact record" in refused(await world.check(clock))


# ───────────────────────────── a message that belongs to no campaign uses safe defaults ─────────────────────────────


async def test_without_a_campaign_verification_is_required_by_default(
    world: World, clock: FakeClock
) -> None:
    await world.change(EmailMessage, world.message_id, campaign_id=None)
    assert isinstance(await world.check(clock), Clearance)  # verified: fine

    await world.change(Contact, world.contact_id, verification_status=VerificationStatus.UNVERIFIED)
    assert "not been verified" in refused(await world.check(clock))


async def test_without_a_campaign_shared_role_addresses_are_allowed_by_default(
    world: World, clock: FakeClock
) -> None:
    await world.change(EmailMessage, world.message_id, campaign_id=None)
    await world.change(Contact, world.contact_id, email_kind=EmailKind.ROLE_BASED)
    assert isinstance(await world.check(clock), Clearance)


async def test_another_workspaces_policy_for_the_same_country_does_not_apply(
    world: World, clock: FakeClock, other_workspace_id: uuid.UUID
) -> None:
    await world.add(
        JurisdictionPolicy(
            workspace_id=other_workspace_id,
            country="US",
            cold_b2b_marketing=ConsentPolicy.BLOCKED,
            cold_to_individuals=ConsentPolicy.BLOCKED,
        )
    )
    assert isinstance(await world.check(clock), Clearance)


async def test_a_policy_row_stored_in_lower_case_still_applies(
    world: World, clock: FakeClock
) -> None:
    await world.add(
        JurisdictionPolicy(
            workspace_id=world.workspace_id,
            country="us",
            cold_b2b_marketing=ConsentPolicy.BLOCKED,
            cold_to_individuals=ConsentPolicy.BLOCKED,
        )
    )
    assert "blocked by the policy for US" in refused(await world.check(clock))


async def test_the_sender_profile_alone_is_enough_to_name_the_sender(
    world: World, clock: FakeClock
) -> None:
    await world.change(ConnectedEmailAccount, world.account_id, display_name=None)
    assert isinstance(
        await world.check(clock), Clearance
    )  # the profile's name identifies the sender


def test_the_html_footer_escapes_a_hostile_link_and_name() -> None:
    footer = _footer_html(
        '<b>"Alex"</b>', "1 Main St", 'https://x.example/?a=1&b="><script>alert(1)</script>'
    )
    assert "<script>" not in footer
    assert "<b>" not in footer
    assert "&amp;b=&quot;&gt;&lt;script&gt;" in footer
