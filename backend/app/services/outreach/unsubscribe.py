"""Unsubscribing: signed links in every message, and what happens when one is used (CAN-SPAM, RFC 8058).

A link carries **no personal data**: a signed token naming the message it came from. The token never expires (a recipient
may open an old e-mail months later, and the opt-out must still work) and is bound to the ``unsubscribe`` purpose, so a token
minted for another flow cannot be replayed here.

Acting on a link is idempotent and has one effect — *this address is never mailed again*:

1. the address goes on the do-not-contact list (once; a second click changes nothing);
2. every message to it that has not left yet is cancelled;
3. every lead it belongs to stops: stage ``UNSUBSCRIBED``, follow-ups stopped.

Opening a link must not unsubscribe anyone by itself: mail scanners pre-fetch links. The web route therefore shows a
confirmation page on GET and only acts on POST (which is also what a mail client's one-click button sends).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import SYSTEM_CLOCK, Clock
from app.core.security import CryptoError, create_action_token, verify_action_token
from app.db.models import ActivityEvent, AuditLog, Contact, EmailMessage, Lead, SuppressionEntry
from app.db.session import session_scope
from app.domain.enums import (
    ActivityLevel,
    ActorType,
    FollowUpStatus,
    LeadStage,
    MessageStatus,
    SuppressionReason,
)
from app.domain.identity import normalise_email

PURPOSE = "unsubscribe"
SOURCE = "unsubscribe_link"
_NOT_SENT_YET = (
    MessageStatus.DRAFT,
    MessageStatus.PENDING_APPROVAL,
    MessageStatus.APPROVED,
    MessageStatus.QUEUED,
)
#: A lead that is already finished one way or another keeps its stage; an unsubscribe overrides everything else.
_FINAL_STAGES = (LeadStage.UNSUBSCRIBED, LeadStage.SUPPRESSED, LeadStage.DUPLICATE)


@dataclass(frozen=True, slots=True)
class Target:
    workspace_id: uuid.UUID
    message_id: uuid.UUID


class UnsubscribeLinks:
    def __init__(self, *, secret: str, backend_url: str, clock: Clock = SYSTEM_CLOCK) -> None:
        self._secret = secret
        self._base = backend_url.rstrip("/")
        self._clock = clock

    def link(self, workspace_id: uuid.UUID, message_id: uuid.UUID) -> str:
        token = create_action_token(
            secret=self._secret,
            purpose=PURPOSE,
            subject=str(message_id),
            ttl=None,
            clock=self._clock,
            claims={"ws": str(workspace_id)},
        )
        return f"{self._base}/u/{token}"

    def resolve(self, token: str) -> Target | None:
        """The message a token was issued for, or ``None`` for anything that is not a genuine unsubscribe token."""
        try:
            claims = verify_action_token(
                secret=self._secret, token=token, purpose=PURPOSE, clock=self._clock
            )
            return Target(
                workspace_id=uuid.UUID(str(claims["ws"])), message_id=uuid.UUID(str(claims["sub"]))
            )
        except (CryptoError, KeyError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class Unsubscribed:
    already: bool  # the address was on the list before this click
    cancelled_messages: int


class UnsubscribeService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._factory = session_factory
        self._clock = clock

    async def unsubscribe(self, target: Target) -> Unsubscribed | None:
        """Suppress the address that message went to. ``None`` if the message (or a usable address) does not exist."""
        now = self._clock.now()
        async with session_scope(self._factory) as s:
            message = (
                await s.execute(
                    select(EmailMessage).where(
                        EmailMessage.workspace_id == target.workspace_id,
                        EmailMessage.id == target.message_id,
                    )
                )
            ).scalar_one_or_none()
            address = normalise_email(message.to_email) if message is not None else None
            if message is None or address is None:
                return None
            inserted = (
                await s.execute(
                    pg_insert(SuppressionEntry)
                    .values(
                        id=uuid.uuid4(),
                        workspace_id=target.workspace_id,
                        email=address,
                        reason=SuppressionReason.UNSUBSCRIBE,
                        source=SOURCE,
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["workspace_id", "email"],
                        index_where=text("email IS NOT NULL"),
                    )
                    .returning(SuppressionEntry.id)
                )
            ).scalar_one_or_none()
            cancelled = await self._cancel_pending(s, target.workspace_id, address)
            await self._stop_leads(s, message, address, now)
            if inserted is not None:
                self._record(s, message, now)
            return Unsubscribed(already=inserted is None, cancelled_messages=cancelled)

    @staticmethod
    async def _cancel_pending(s: AsyncSession, workspace_id: uuid.UUID, address: str) -> int:
        result = await s.execute(
            update(EmailMessage)
            .where(
                EmailMessage.workspace_id == workspace_id,
                func.lower(EmailMessage.to_email) == address,
                EmailMessage.status.in_(_NOT_SENT_YET),
            )
            .values(status=MessageStatus.CANCELLED, skip_reason="the recipient unsubscribed")
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    @staticmethod
    async def _stop_leads(
        s: AsyncSession, message: EmailMessage, address: str, now: datetime
    ) -> None:
        owned_by_address = select(Contact.id).where(
            Contact.workspace_id == message.workspace_id, func.lower(Contact.email) == address
        )
        await s.execute(
            update(Lead)
            .where(
                Lead.workspace_id == message.workspace_id,
                or_(Lead.id == message.lead_id, Lead.primary_contact_id.in_(owned_by_address)),
                Lead.stage.not_in(_FINAL_STAGES),
            )
            .values(
                stage=LeadStage.UNSUBSCRIBED,
                followup_status=FollowUpStatus.STOPPED,
                stop_reason="the recipient unsubscribed",
            )
        )

    @staticmethod
    def _record(s: AsyncSession, message: EmailMessage, now: datetime) -> None:
        """The activity feed and the audit log say *which message* led to the opt-out — never the address itself."""
        data = {"message_id": str(message.id)}
        s.add(
            ActivityEvent(
                workspace_id=message.workspace_id,
                created_at=now,
                level=ActivityLevel.INFO,
                category="email",
                event_type="unsubscribed",
                message="A recipient unsubscribed; the address is on the do-not-contact list",
                campaign_id=message.campaign_id,
                lead_id=message.lead_id,
                data=data,
            )
        )
        s.add(
            AuditLog(
                workspace_id=message.workspace_id,
                created_at=now,
                actor_type=ActorType.SYSTEM,
                action="suppression.added",
                entity_type="email_message",
                entity_id=str(message.id),
                details={"reason": SuppressionReason.UNSUBSCRIBE.value, "source": SOURCE},
            )
        )
