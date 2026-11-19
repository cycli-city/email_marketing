"""Exactly-once sending against a real database and a fake Gmail that remembers every message it was given.

Every test that matters ends the same way: ask the fake how many copies of the message really exist.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FakeClock
from app.db.models import (
    ActivityEvent,
    AuditLog,
    ConnectedEmailAccount,
    EmailMessage,
    EmailThread,
)
from app.db.session import session_scope
from app.domain.enums import (
    AccountProvider,
    AccountStatus,
    ActivityLevel,
    CampaignMode,
    MessageKind,
    MessageStatus,
)
from app.integrations.gmail.api import GmailApi
from app.integrations.gmail.errors import AccountNeedsReauthError, GoogleUnavailableError
from app.services.outreach.sender import (
    MAX_SEND_ATTEMPTS,
    QUOTA_PARK,
    RATE_LIMIT_PARK,
    SEND_LEASE,
    SETTLE,
    TRANSIENT_PARK,
    Clearance,
    MailboxSender,
    Refusal,
    SendOutcome,
    SendResult,
)
from tests.gmail_fake import FakeGmail, Fate, alias

Factory = async_sessionmaker[AsyncSession]
NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)


class Credentials:
    """Stands in for GmailService: hands out a token, or says the mailbox needs reconnecting."""

    def __init__(self, factory: Factory) -> None:
        self.factory = factory
        self.token = "ya29.a-fake-access-token-for-tests"
        self.reauth = False
        self.google_down = False
        self.marked: list[uuid.UUID] = []

    async def access_token(self, workspace_id: uuid.UUID, account_id: uuid.UUID) -> str:
        if self.reauth:
            raise AccountNeedsReauthError("reconnect")
        if self.google_down:
            raise GoogleUnavailableError("down")
        return self.token

    async def mark_needs_reauth(self, workspace_id: uuid.UUID, account_id: uuid.UUID) -> None:
        self.marked.append(account_id)
        async with session_scope(self.factory) as s:
            account = await s.get(ConnectedEmailAccount, account_id)
            assert account is not None
            account.status = AccountStatus.NEEDS_REAUTH


@dataclass
class Gate:
    clearance: Clearance = field(
        default_factory=lambda: Clearance(
            unsubscribe_https="https://api.example.com/u/token123",
            unsubscribe_mailto="unsubscribe@example.com",
            footer_text="NEXORA - reply STOP to opt out.",
        )
    )
    refuse: dict[uuid.UUID, str] = field(default_factory=dict)
    seen: list[MessageStatus] = field(default_factory=list)
    explode: bool = False

    async def check(
        self, session: AsyncSession, message: EmailMessage, account: ConnectedEmailAccount
    ) -> Clearance | Refusal:
        self.seen.append(message.status)  # the gate must run AFTER the claim
        if self.explode:
            raise RuntimeError("gate exploded")
        if message.id in self.refuse:
            return Refusal(self.refuse[message.id])
        return self.clearance


class World:
    def __init__(self, factory: Factory, workspace_id: uuid.UUID) -> None:
        self.factory = factory
        self.workspace_id = workspace_id
        self.clock = FakeClock(NOW)
        self.gmail = FakeGmail(clock=self.clock)
        self.credentials = Credentials(factory)
        self.gate = Gate()
        self.sender = MailboxSender(
            session_factory=factory,
            clock=self.clock,
            gmail=GmailApi(httpx.AsyncClient(transport=self.gmail.transport()), self.clock),
            credentials=self.credentials,
            gate=self.gate,
        )

    async def account(self, **fields: Any) -> uuid.UUID:
        email = fields.pop("email_address", f"sender.{uuid.uuid4().hex[:8]}@example.com")
        defaults: dict[str, Any] = {
            "provider": AccountProvider.GMAIL,
            "mode": CampaignMode.NEXORA,
            "status": AccountStatus.ACTIVE,
            "daily_limit": 50,
            "hourly_limit": 50,
        }
        async with session_scope(self.factory) as s:
            account = ConnectedEmailAccount(
                workspace_id=self.workspace_id, email_address=email, **{**defaults, **fields}
            )
            s.add(account)
            await s.flush()
            account_id = account.id
        self.gmail.email = email
        return account_id

    async def message(self, account_id: uuid.UUID | None, **fields: Any) -> uuid.UUID:
        token = uuid.uuid4().hex
        defaults: dict[str, Any] = {
            "kind": MessageKind.INITIAL,
            "status": MessageStatus.QUEUED,
            "to_email": f"lead.{token[:8]}@example.org",
            "to_name": "Pat Lead",
            "subject": "A quick idea for Acme",
            "body_text": "Hi Pat,\n\nA short note.\n\nDivyansh",
            "idempotency_key": f"key-{token}",
            "rfc_message_id": f"<{token}@example.com>",
        }
        async with session_scope(self.factory) as s:
            message = EmailMessage(
                workspace_id=self.workspace_id, account_id=account_id, **{**defaults, **fields}
            )
            s.add(message)
            await s.flush()
            return message.id

    async def row(self, message_id: uuid.UUID) -> EmailMessage:
        async with self.factory() as s:
            row = await s.get(EmailMessage, message_id)
            assert row is not None
            return row

    async def account_row(self, account_id: uuid.UUID) -> ConnectedEmailAccount:
        async with self.factory() as s:
            row = await s.get(ConnectedEmailAccount, account_id)
            assert row is not None
            return row

    async def all(self, model: Any) -> list[Any]:
        async with self.factory() as s:
            return list((await s.execute(select(model))).scalars())

    async def send(self, message_id: uuid.UUID) -> Any:
        return await self.sender.send(self.workspace_id, message_id)

    async def reconcile(self) -> Any:
        return await self.sender.reconcile(self.workspace_id)

    def copies(self, row: EmailMessage) -> int:
        return self.gmail.copies_of(row.rfc_message_id)


@pytest.fixture
def world(factory: Factory, workspace_id: uuid.UUID) -> World:
    return World(factory, workspace_id)


# ───────────────────────────── the happy path ─────────────────────────────


class TestSending:
    async def test_a_queued_message_is_sent_once_and_recorded(self, world: World) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)

        result = await world.send(message_id)

        assert result.outcome == SendOutcome.SENT
        row = await world.row(message_id)
        assert row.status == MessageStatus.SENT
        assert row.sent_at == NOW
        assert row.gmail_message_id == world.gmail.stored[0].id
        assert row.gmail_thread_id == world.gmail.stored[0].thread_id
        assert row.claim_token is None
        assert row.attempt_count == 1
        assert world.copies(row) == 1

    async def test_the_gate_runs_after_the_claim(self, world: World) -> None:
        account_id = await world.account()
        await world.send(await world.message(account_id))
        assert world.gate.seen == [MessageStatus.SENDING]

    async def test_the_message_on_the_wire_is_complete_and_compliant(self, world: World) -> None:
        account_id = await world.account()
        account = await world.account_row(account_id)
        message_id = await world.message(account_id, reply_to="owner@example.net")
        await world.send(message_id)

        row = await world.row(message_id)
        wire = world.gmail.stored[0].message
        assert wire["Message-ID"] == row.rfc_message_id
        assert wire["X-AOI-Message"] == row.rfc_message_id[1:].split("@")[0]
        assert str(wire["From"]) == account.email_address
        assert wire["To"] == "Pat Lead <" + row.to_email + ">"
        assert wire["Reply-To"] == "owner@example.net"
        assert wire["Subject"] == "A quick idea for Acme"
        assert " ".join(str(wire["List-Unsubscribe"]).split()) == (
            "<mailto:unsubscribe@example.com>, <https://api.example.com/u/token123>"
        )
        assert wire["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        body = wire.get_content()
        assert body.rstrip().endswith("NEXORA - reply STOP to opt out.")

    async def test_a_thread_is_created_and_linked(self, world: World) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id, subject="Hello thread")
        await world.send(message_id)

        (thread,) = await world.all(EmailThread)
        row = await world.row(message_id)
        assert row.thread_id == thread.id
        assert thread.gmail_thread_id == row.gmail_thread_id
        assert thread.account_id == account_id
        assert thread.message_count == 1
        assert thread.subject == "Hello thread"

    async def test_a_follow_up_joins_the_thread_and_carries_the_reply_headers(
        self, world: World
    ) -> None:
        account_id = await world.account()
        first = await world.message(account_id)
        await world.send(first)
        first_row = await world.row(first)
        world.clock.advance(days=3)

        follow = await world.message(
            account_id, kind=MessageKind.FOLLOW_UP, step_no=1, thread_id=first_row.thread_id
        )
        await world.send(follow)

        wire = world.gmail.stored[1]
        assert wire.thread_id == first_row.gmail_thread_id  # sent into the same Gmail thread
        assert wire.message["In-Reply-To"] == first_row.rfc_message_id
        assert wire.message["References"] == first_row.rfc_message_id
        (thread,) = await world.all(EmailThread)
        assert thread.message_count == 2
        assert (await world.row(follow)).thread_id == thread.id

    async def test_activity_and_audit_trail_are_written_with_the_injected_clock(
        self, world: World
    ) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        await world.send(message_id)

        (event,) = [e for e in await world.all(ActivityEvent) if e.event_type == "email_sent"]
        assert event.created_at == NOW
        assert event.level == ActivityLevel.INFO
        (audit,) = await world.all(AuditLog)
        assert audit.action == "email.sent"
        assert audit.entity_id == str(message_id)
        assert audit.created_at == NOW

    async def test_nothing_secret_is_written_anywhere(self, world: World) -> None:
        account_id = await world.account()
        await world.send(await world.message(account_id))
        dump = repr([e.data for e in await world.all(ActivityEvent)]) + repr(
            [a.details for a in await world.all(AuditLog)]
        )
        assert world.credentials.token not in dump


# ───────────────────────────── claiming ─────────────────────────────


class TestClaiming:
    async def test_sending_twice_sends_once(self, world: World) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        first = await world.send(message_id)
        second = await world.send(message_id)
        assert (first.outcome, second.outcome) == (SendOutcome.SENT, SendOutcome.NOT_CLAIMABLE)
        assert len(world.gmail.sends()) == 1

    async def test_twenty_workers_racing_for_one_message_send_it_exactly_once(
        self, world: World
    ) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)

        results = await asyncio.gather(*(world.send(message_id) for _ in range(20)))

        assert sorted(r.outcome for r in results).count(SendOutcome.SENT) == 1
        assert {r.outcome for r in results} <= {SendOutcome.SENT, SendOutcome.NOT_CLAIMABLE}
        assert len(world.gmail.sends()) == 1
        assert world.copies(await world.row(message_id)) == 1

    async def test_racing_workers_can_never_overspend_the_mailbox_limit(self, world: World) -> None:
        account_id = await world.account(daily_limit=5)
        ids = [await world.message(account_id) for _ in range(12)]

        results = await asyncio.gather(*(world.send(i) for i in ids))

        assert [r.outcome for r in results].count(SendOutcome.SENT) == 5
        assert [r.outcome for r in results].count(SendOutcome.DEFERRED) == 7
        assert len(world.gmail.sends()) == 5

    async def test_a_message_that_does_not_exist_or_belongs_elsewhere_is_not_claimable(
        self, world: World, other_workspace_id: uuid.UUID
    ) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        result = await world.sender.send(other_workspace_id, message_id)
        assert result.outcome == SendOutcome.NOT_CLAIMABLE
        assert (
            await world.sender.send(world.workspace_id, uuid.uuid4())
        ).outcome == SendOutcome.NOT_CLAIMABLE
        assert world.gmail.requests == []
        assert (await world.row(message_id)).status == MessageStatus.QUEUED

    @pytest.mark.parametrize(
        "status",
        [
            MessageStatus.DRAFT,
            MessageStatus.PENDING_APPROVAL,
            MessageStatus.APPROVED,
            MessageStatus.SENDING,
            MessageStatus.SENT,
            MessageStatus.SEND_UNCERTAIN,
            MessageStatus.FAILED,
            MessageStatus.CANCELLED,
            MessageStatus.SKIPPED,
        ],
    )
    async def test_only_a_queued_message_can_be_sent(
        self, world: World, status: MessageStatus
    ) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id, status=status)
        result = await world.send(message_id)
        assert result.outcome == SendOutcome.NOT_CLAIMABLE
        assert world.gmail.requests == []

    async def test_a_message_scheduled_for_later_waits(self, world: World) -> None:
        account_id = await world.account()
        later = NOW + timedelta(hours=2)
        message_id = await world.message(account_id, scheduled_for=later)
        result = await world.send(message_id)
        assert (result.outcome, result.retry_at) == (SendOutcome.DEFERRED, later)
        assert world.gmail.requests == []
        world.clock.advance(hours=2)
        assert (await world.send(message_id)).outcome == SendOutcome.SENT

    async def test_a_message_without_a_mailbox_is_skipped_not_guessed(self, world: World) -> None:
        message_id = await world.message(None)
        result = await world.send(message_id)
        assert result.outcome == SendOutcome.REFUSED
        row = await world.row(message_id)
        assert row.status == MessageStatus.SKIPPED
        assert "mailbox" in (row.skip_reason or "")


# ───────────────────────────── mailbox limits ─────────────────────────────


class TestLimits:
    async def test_the_daily_limit_defers_until_the_oldest_send_leaves_the_window(
        self, world: World
    ) -> None:
        account_id = await world.account(daily_limit=3)
        first_at = world.clock.now()
        for _ in range(3):
            assert (await world.send(await world.message(account_id))).outcome == SendOutcome.SENT
            world.clock.advance(minutes=1)
        fourth = await world.message(account_id)

        result = await world.send(fourth)

        assert result.outcome == SendOutcome.DEFERRED
        assert result.retry_at == first_at + timedelta(hours=24)
        row = await world.row(fourth)
        assert (row.status, row.attempt_count) == (MessageStatus.QUEUED, 0)
        assert len(world.gmail.sends()) == 3

        assert result.retry_at is not None
        world.clock.set(result.retry_at)
        assert (await world.send(fourth)).outcome == SendOutcome.SENT

    async def test_the_hourly_limit_is_separate(self, world: World) -> None:
        account_id = await world.account(hourly_limit=2, daily_limit=50)
        for _ in range(2):
            await world.send(await world.message(account_id))
        third = await world.message(account_id)
        result = await world.send(third)
        assert result.outcome == SendOutcome.DEFERRED
        assert result.retry_at == NOW + timedelta(hours=1)
        assert "hourly" in (result.detail or "")

    async def test_a_limit_of_zero_switches_sending_off(self, world: World) -> None:
        account_id = await world.account(daily_limit=0)
        result = await world.send(await world.message(account_id))
        assert (result.outcome, result.retry_at) == (SendOutcome.DEFERRED, None)
        assert world.gmail.requests == []

    async def test_an_unconfirmed_send_still_spends_a_slot(self, world: World) -> None:
        account_id = await world.account(daily_limit=1)
        world.gmail.script.append(Fate.ACCEPT_THEN_TIMEOUT)
        assert (await world.send(await world.message(account_id))).outcome == SendOutcome.UNCERTAIN
        second = await world.send(await world.message(account_id))
        assert second.outcome == SendOutcome.DEFERRED
        assert len(world.gmail.sends()) == 1

    async def test_skipped_and_failed_messages_do_not_spend_slots(self, world: World) -> None:
        account_id = await world.account(daily_limit=1)
        refused = await world.message(account_id)
        world.gate.refuse[refused] = "suppressed"
        await world.send(refused)
        world.gmail.script.append(Fate.BAD_REQUEST)
        await world.send(await world.message(account_id))
        assert (await world.send(await world.message(account_id))).outcome == SendOutcome.SENT

    async def test_limits_are_per_mailbox(self, world: World) -> None:
        busy = await world.account(daily_limit=1)
        free = await world.account(daily_limit=1)
        await world.send(await world.message(busy))
        assert (await world.send(await world.message(busy))).outcome == SendOutcome.DEFERRED
        assert (await world.send(await world.message(free))).outcome == SendOutcome.SENT


# ───────────────────────────── the gate ─────────────────────────────


class TestGate:
    async def test_a_refusal_skips_the_message_and_sends_nothing(self, world: World) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.gate.refuse[message_id] = "recipient replied since this was queued"

        result = await world.send(message_id)

        assert (result.outcome, result.detail) == (
            SendOutcome.REFUSED,
            "recipient replied since this was queued",
        )
        row = await world.row(message_id)
        assert row.status == MessageStatus.SKIPPED
        assert row.skip_reason == "recipient replied since this was queued"
        assert row.claim_token is None
        assert world.gmail.requests == []

    async def test_a_crashing_gate_releases_the_message_and_the_error_surfaces(
        self, world: World
    ) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.gate.explode = True

        with pytest.raises(RuntimeError, match="gate exploded"):
            await world.send(message_id)

        row = await world.row(message_id)
        assert (row.status, row.attempt_count, row.claim_token) == (MessageStatus.QUEUED, 0, None)
        assert row.scheduled_for == NOW + TRANSIENT_PARK
        assert world.gmail.requests == []
        world.gate.explode = False
        world.clock.advance(minutes=6)
        assert (await world.send(message_id)).outcome == SendOutcome.SENT


# ───────────────────────────── mailbox state ─────────────────────────────


class TestMailboxState:
    @pytest.mark.parametrize(
        "status", [AccountStatus.NEEDS_REAUTH, AccountStatus.ERROR, AccountStatus.DISCONNECTED]
    )
    async def test_an_inactive_mailbox_never_sends(
        self, world: World, status: AccountStatus
    ) -> None:
        account_id = await world.account(status=status)
        message_id = await world.message(account_id)
        result = await world.send(message_id)
        assert (result.outcome, result.retry_at) == (SendOutcome.DEFERRED, None)
        assert (await world.row(message_id)).status == MessageStatus.QUEUED
        assert world.gmail.requests == []

    async def test_credentials_that_need_reconnecting_defer_without_burning_an_attempt(
        self, world: World
    ) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.credentials.reauth = True
        result = await world.send(message_id)
        assert result.outcome == SendOutcome.DEFERRED
        row = await world.row(message_id)
        assert (row.status, row.attempt_count) == (MessageStatus.QUEUED, 0)
        assert world.gmail.requests == []

    async def test_a_google_outage_while_getting_a_token_retries_later(self, world: World) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.credentials.google_down = True
        result = await world.send(message_id)
        assert (result.outcome, result.retry_at) == (SendOutcome.DEFERRED, NOW + TRANSIENT_PARK)
        assert (await world.row(message_id)).attempt_count == 0

    async def test_gmail_rejecting_the_token_marks_the_mailbox_for_reconnecting(
        self, world: World
    ) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.gmail.script.append(Fate.UNAUTHORIZED)

        result = await world.send(message_id)

        assert result.outcome == SendOutcome.DEFERRED
        assert world.credentials.marked == [account_id]
        assert (await world.account_row(account_id)).status == AccountStatus.NEEDS_REAUTH
        row = await world.row(message_id)
        assert (row.status, row.attempt_count) == (MessageStatus.QUEUED, 0)
        # and the mailbox is not used again until it is reconnected
        assert (await world.send(message_id)).outcome == SendOutcome.DEFERRED
        assert len(world.gmail.sends()) == 1

    async def test_an_administrator_block_stops_the_mailbox_and_tells_the_owner(
        self, world: World
    ) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.gmail.script.append(Fate.DOMAIN_POLICY)

        result = await world.send(message_id)

        assert result.outcome == SendOutcome.DEFERRED
        account = await world.account_row(account_id)
        assert account.status == AccountStatus.ERROR
        assert "administrator" in (account.last_error or "")
        assert (await world.row(message_id)).status == MessageStatus.QUEUED
        errors = [e for e in await world.all(ActivityEvent) if e.level == ActivityLevel.ERROR]
        assert [e.event_type for e in errors] == ["mailbox_error"]


# ───────────────────────────── failures ─────────────────────────────


class TestFailures:
    async def test_a_refused_request_fails_the_message_for_good(self, world: World) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.gmail.script.append(Fate.BAD_REQUEST)

        result = await world.send(message_id)

        assert result.outcome == SendOutcome.FAILED
        row = await world.row(message_id)
        assert (row.status, row.error_class) == (MessageStatus.FAILED, "gmail_rejected")
        assert row.claim_token is None
        assert world.copies(row) == 0
        assert (await world.send(message_id)).outcome == SendOutcome.NOT_CLAIMABLE

    async def test_the_sending_limit_is_waited_out_and_never_routed_around(
        self, world: World
    ) -> None:
        account_id = await world.account()
        spare = await world.account()  # another ACTIVE mailbox in the same workspace
        message_id = await world.message(account_id)
        world.gmail.script.append(Fate.SENDING_LIMIT)

        result = await world.send(message_id)

        assert (result.outcome, result.retry_at) == (SendOutcome.DEFERRED, NOW + QUOTA_PARK)
        row = await world.row(message_id)
        assert (row.status, row.account_id, row.attempt_count) == (
            MessageStatus.QUEUED,
            account_id,
            0,
        )
        assert row.account_id != spare
        warnings = [e for e in await world.all(ActivityEvent) if e.level == ActivityLevel.WARNING]
        assert [e.event_type for e in warnings] == ["email_send_limit"]
        assert len(world.gmail.sends()) == 1

    async def test_a_429_waits_for_the_retry_after_it_was_given(self, world: World) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.gmail.script.append(Fate.RATE_LIMITED)
        result = await world.send(message_id)
        assert (result.outcome, result.retry_at) == (
            SendOutcome.DEFERRED,
            NOW + timedelta(seconds=45),
        )

    async def test_a_429_without_a_hint_uses_the_default_pause(self, world: World) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.gmail.script.append(Fate.RATE_LIMITED_NO_HINT)
        result = await world.send(message_id)
        assert (result.outcome, result.retry_at) == (SendOutcome.DEFERRED, NOW + RATE_LIMIT_PARK)

    async def test_a_connection_that_never_opened_is_safe_to_retry(self, world: World) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.gmail.script.append(Fate.CONNECT_ERROR)

        result = await world.send(message_id)

        assert (result.outcome, result.retry_at) == (SendOutcome.DEFERRED, NOW + TRANSIENT_PARK)
        row = await world.row(message_id)
        assert (row.status, row.attempt_count) == (MessageStatus.QUEUED, 0)
        world.clock.advance(minutes=6)
        assert (await world.send(message_id)).outcome == SendOutcome.SENT
        assert world.copies(await world.row(message_id)) == 1

    @pytest.mark.parametrize(
        "fate",
        [
            Fate.ACCEPT_THEN_TIMEOUT,
            Fate.ACCEPT_THEN_GARBAGE,
            Fate.ACCEPT_THEN_503,
            Fate.REJECT_503,
        ],
    )
    async def test_anything_that_may_have_gone_out_is_never_retried_blindly(
        self, world: World, fate: Fate
    ) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.gmail.script.append(fate)

        result = await world.send(message_id)

        assert result.outcome == SendOutcome.UNCERTAIN
        row = await world.row(message_id)
        assert row.status == MessageStatus.SEND_UNCERTAIN
        assert row.claim_token is not None  # kept: it fences the reconciler
        # a second send() must not try again
        assert (await world.send(message_id)).outcome == SendOutcome.NOT_CLAIMABLE
        assert len(world.gmail.sends()) == 1

    async def test_a_header_injection_attempt_is_refused_and_nothing_is_sent(
        self, world: World
    ) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id, subject="Hi\r\nBcc: victim@example.net")
        result = await world.send(message_id)
        assert result.outcome == SendOutcome.FAILED
        row = await world.row(message_id)
        assert (row.status, row.error_class) == (MessageStatus.FAILED, "invalid_message")
        assert world.gmail.requests == []

    async def test_the_access_token_never_reaches_a_stored_error(self, world: World) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.gmail.script.append(Fate.ACCEPT_THEN_TIMEOUT)
        await world.send(message_id)
        row = await world.row(message_id)
        assert world.credentials.token not in (row.error_detail or "")


# ───────────────────────────── sender identity ─────────────────────────────


class TestIdentity:
    async def test_a_verified_alias_is_used_as_the_from_address(self, world: World) -> None:
        account_id = await world.account()
        world.gmail.aliases = [alias("founder@example.com")]
        message_id = await world.message(account_id, from_email="Founder@Example.com")
        assert (await world.send(message_id)).outcome == SendOutcome.SENT
        assert str(world.gmail.stored[0].message["From"]) == "Founder@Example.com"

    async def test_the_alias_list_is_fetched_once_not_per_message(self, world: World) -> None:
        account_id = await world.account()
        world.gmail.aliases = [alias("founder@example.com")]
        for _ in range(3):
            await world.send(await world.message(account_id, from_email="founder@example.com"))
        lookups = [r for r in world.gmail.requests if r.url.path.endswith("/settings/sendAs")]
        assert len(lookups) == 1

    @pytest.mark.parametrize("verified", [False, None])
    async def test_gmail_is_never_allowed_to_silently_rewrite_the_sender(
        self, world: World, verified: bool | None
    ) -> None:
        account_id = await world.account()
        if verified is False:
            world.gmail.aliases = [alias("founder@example.com", verified=False)]  # still pending
        message_id = await world.message(account_id, from_email="founder@example.com")

        result = await world.send(message_id)

        assert result.outcome == SendOutcome.FAILED
        assert "send-as" in (result.detail or "")
        assert world.gmail.sends() == []


# ───────────────────────────── reconciling ─────────────────────────────


class TestReconciler:
    async def test_an_unconfirmed_send_that_did_go_out_is_marked_sent_and_never_resent(
        self, world: World
    ) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.gmail.script.append(Fate.ACCEPT_THEN_TIMEOUT)
        await world.send(message_id)
        world.clock.advance(minutes=1)

        report = await world.reconcile()

        assert report.confirmed_sent == 1
        row = await world.row(message_id)
        assert row.status == MessageStatus.SENT
        assert row.gmail_message_id == world.gmail.stored[0].id
        assert row.sent_at == NOW  # when Gmail stored it, not when we noticed
        assert row.claim_token is None
        assert world.copies(row) == 1
        assert len(world.gmail.sends()) == 1
        assert (await world.all(EmailThread))[0].message_count == 1

    async def test_an_unconfirmed_send_that_did_not_go_out_is_retried_once_after_the_settle_window(
        self, world: World
    ) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.gmail.script.append(Fate.REJECT_503)
        await world.send(message_id)

        world.clock.advance(minutes=5)
        assert (await world.reconcile()).waiting == 1  # too early to conclude anything
        assert (await world.row(message_id)).status == MessageStatus.SEND_UNCERTAIN

        world.clock.advance(seconds=SETTLE.total_seconds())
        assert (await world.reconcile()).requeued == 1
        row = await world.row(message_id)
        assert (row.status, row.claim_token) == (MessageStatus.QUEUED, None)

        assert (await world.send(message_id)).outcome == SendOutcome.SENT
        assert world.copies(await world.row(message_id)) == 1
        assert len(world.gmail.sends()) == 2  # one lost attempt, one real send

    async def test_after_two_unconfirmed_attempts_a_human_decides(self, world: World) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        for _ in range(MAX_SEND_ATTEMPTS):
            world.gmail.script.append(Fate.REJECT_503)
            await world.send(message_id)
            world.clock.advance(seconds=SETTLE.total_seconds() + 1)
            await world.reconcile()

        row = await world.row(message_id)
        assert (row.status, row.error_class) == (MessageStatus.FAILED, "send_unconfirmed")
        assert row.attempt_count == MAX_SEND_ATTEMPTS
        assert len(world.gmail.sends()) == MAX_SEND_ATTEMPTS
        errors = [e for e in await world.all(ActivityEvent) if e.event_type == "send_unconfirmed"]
        assert len(errors) == 1

    async def test_a_worker_that_died_after_claiming_is_recovered_without_a_duplicate(
        self, world: World
    ) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        await world.sender._claim(world.workspace_id, message_id)  # the process dies right here
        assert (await world.row(message_id)).status == MessageStatus.SENDING

        world.clock.advance(minutes=1)
        report = await world.reconcile()  # still inside the lease: another worker may be mid-send
        assert (report.requeued, report.failed, report.confirmed_sent, report.waiting) == (
            0,
            0,
            0,
            0,
        )
        assert (await world.row(message_id)).status == MessageStatus.SENDING

        world.clock.advance(seconds=SEND_LEASE.total_seconds() + SETTLE.total_seconds())
        assert (await world.reconcile()).requeued == 1
        assert (await world.send(message_id)).outcome == SendOutcome.SENT
        assert world.copies(await world.row(message_id)) == 1

    async def test_a_worker_that_died_after_gmail_accepted_is_recovered_without_a_duplicate(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)

        async def database_went_away(*_: object, **__: object) -> bool:
            raise ConnectionError("database connection lost")

        monkeypatch.setattr(world.sender, "_record_sent", database_went_away)
        with pytest.raises(ConnectionError):
            await world.send(message_id)

        # Gmail has the message; our row still says SENDING, and was NOT released for another attempt.
        row = await world.row(message_id)
        assert row.status == MessageStatus.SENDING
        assert world.copies(row) == 1

        monkeypatch.undo()
        world.clock.advance(seconds=SEND_LEASE.total_seconds() + 1)
        assert (await world.reconcile()).confirmed_sent == 1
        row = await world.row(message_id)
        assert row.status == MessageStatus.SENT
        assert len(world.gmail.sends()) == 1
        assert world.copies(row) == 1

    async def test_a_slow_worker_that_lost_its_claim_cannot_overwrite_the_new_owner(
        self, world: World
    ) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        zombie = await world.sender._claim(world.workspace_id, message_id)
        assert not isinstance(zombie, SendResult)  # a real claim, not a refusal

        world.clock.advance(seconds=SEND_LEASE.total_seconds() + SETTLE.total_seconds())
        await world.reconcile()  # requeued: nobody found it in Sent
        assert (await world.send(message_id)).outcome == SendOutcome.SENT
        good = await world.row(message_id)

        # The zombie wakes up and tries to record its own, stale, outcome.
        recorded = await world.sender._record_sent(
            zombie, "zombie-id", "zombie-thread", sent_at=NOW
        )
        assert recorded is False
        after = await world.row(message_id)
        assert (after.gmail_message_id, after.status) == (good.gmail_message_id, MessageStatus.SENT)

    async def test_two_copies_in_sent_are_reported_loudly(self, world: World) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.gmail.script.append(Fate.ACCEPT_THEN_TIMEOUT)
        await world.send(message_id)
        # something outside our control put a second copy in Sent
        duplicate = world.gmail.stored[0]
        world.gmail.stored.append(type(duplicate)(**{**duplicate.__dict__, "id": "msg9999"}))

        assert (await world.reconcile()).confirmed_sent == 1

        errors = [e for e in await world.all(ActivityEvent) if e.event_type == "duplicate_send"]
        assert len(errors) == 1
        assert (await world.row(message_id)).status == MessageStatus.SENT

    async def test_a_mailbox_that_needs_reconnecting_cannot_be_checked_so_nothing_is_decided(
        self, world: World
    ) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.gmail.script.append(Fate.REJECT_503)
        await world.send(message_id)
        world.credentials.reauth = True
        world.clock.advance(hours=3)

        report = await world.reconcile()

        assert (report.waiting, report.requeued, report.failed) == (1, 0, 0)
        assert (await world.row(message_id)).status == MessageStatus.SEND_UNCERTAIN

    async def test_it_only_looks_at_its_own_workspace(
        self, world: World, other_workspace_id: uuid.UUID
    ) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.gmail.script.append(Fate.ACCEPT_THEN_TIMEOUT)
        await world.send(message_id)
        report = await world.sender.reconcile(other_workspace_id)
        assert (report.confirmed_sent, report.waiting) == (0, 0)
        assert (await world.row(message_id)).status == MessageStatus.SEND_UNCERTAIN

    async def test_nothing_to_do_is_cheap(self, world: World) -> None:
        await world.account()
        report = await world.reconcile()
        assert (report.confirmed_sent, report.requeued, report.failed, report.waiting) == (
            0,
            0,
            0,
            0,
        )
        assert world.gmail.requests == []
