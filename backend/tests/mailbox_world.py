"""Shared fixtures-as-objects for the mailbox tests: a fake Gmail, fake credentials, a scriptable send gate, and
helpers to create mailboxes and queued messages in the real database."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FakeClock
from app.db.models import ConnectedEmailAccount, EmailMessage
from app.db.session import session_scope
from app.domain.enums import (
    AccountProvider,
    AccountStatus,
    CampaignMode,
    MessageKind,
    MessageStatus,
)
from app.integrations.gmail.api import GmailApi
from app.integrations.gmail.errors import AccountNeedsReauthError, GoogleUnavailableError
from app.services.inbox.sync import InboxSync
from app.services.outreach.sender import Clearance, MailboxSender, Refusal
from tests.gmail_fake import FakeGmail

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
        self.sync = InboxSync(
            session_factory=factory,
            clock=self.clock,
            gmail=GmailApi(httpx.AsyncClient(transport=self.gmail.transport()), self.clock),
            credentials=self.credentials,
        )
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

    async def poll(self, account_id: uuid.UUID) -> Any:
        return await self.sync.poll(self.workspace_id, account_id)

    def copies(self, row: EmailMessage) -> int:
        return self.gmail.copies_of(row.rfc_message_id)
