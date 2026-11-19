"""Threads, outbound messages (the exactly-once state machine), inbound replies, classifications, follow-ups."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, WorkspaceScopedMixin
from app.db.types import enum_col
from app.domain.enums import (
    FollowUpScheduleStatus,
    MessageKind,
    MessageStatus,
    ReplyCategory,
    ReplyKind,
    ReplyProcessingStatus,
)

_EMPTY_OBJ = text("'{}'::jsonb")
_EMPTY_ARR = text("'[]'::jsonb")


class EmailThread(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    __tablename__ = "email_threads"
    __table_args__ = (
        Index(
            "uq_email_threads_account_gmail_thread",
            "account_id",
            "gmail_thread_id",
            unique=True,
            postgresql_where=text("gmail_thread_id IS NOT NULL"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connected_email_accounts.id", ondelete="CASCADE")
    )
    gmail_thread_id: Mapped[str | None] = mapped_column(String(64))
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    subject: Mapped[str | None] = mapped_column(Text)
    first_message_at: Mapped[datetime | None]
    last_message_at: Mapped[datetime | None]
    last_inbound_at: Mapped[datetime | None]
    message_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class EmailMessage(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """An outbound email. Status transitions are the exactly-once protocol (ARCHITECTURE §5)."""

    __tablename__ = "email_messages"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key"),
        UniqueConstraint("rfc_message_id"),
        # One message per (campaign, contact, sequence step); owner-approved replies are exempt.
        Index(
            "uq_email_messages_sequence_step",
            "campaign_id",
            "contact_id",
            "step_no",
            unique=True,
            postgresql_where=text("kind <> 'reply'"),
        ),
        Index("ix_email_messages_campaign_status", "campaign_id", "status"),
        Index(
            "ix_email_messages_due",
            "scheduled_for",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_email_messages_to_email", "workspace_id", "to_email"),
        # Replies and bounce notices quote the Message-ID Gmail assigned, so that is what they are matched on.
        Index(
            "ix_email_messages_sent_message_id",
            "workspace_id",
            "sent_message_id",
            postgresql_where=text("sent_message_id IS NOT NULL"),
        ),
        CheckConstraint("step_no >= 0", name="step_non_negative"),
    )

    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_threads.id", ondelete="SET NULL")
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("connected_email_accounts.id", ondelete="SET NULL")
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_templates.id", ondelete="SET NULL")
    )
    template_version: Mapped[int | None] = mapped_column(Integer)

    kind: Mapped[MessageKind] = mapped_column(enum_col(MessageKind), default=MessageKind.INITIAL)
    step_no: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"))
    status: Mapped[MessageStatus] = mapped_column(
        enum_col(MessageStatus), default=MessageStatus.DRAFT
    )

    to_email: Mapped[str] = mapped_column(
        String(320)
    )  # denormalised: survives lead/contact deletion
    to_name: Mapped[str | None] = mapped_column(String(300))
    from_email: Mapped[str | None] = mapped_column(String(320))
    reply_to: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_ARR
    )
    # {prompt_version, model, provider, fact_ids, verifier:{ok, violations}} — audit + eval.
    generation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_OBJ
    )
    grounding_ok: Mapped[bool | None]

    idempotency_key: Mapped[str] = mapped_column(String(200))
    # Our own Message-ID "<token@domain>", fixed when the message is queued. The token also travels in the
    # `X-AOI-Message` header, which Gmail keeps — unlike the Message-ID itself, which Gmail replaces on send.
    rfc_message_id: Mapped[str] = mapped_column(String(300))
    # The Message-ID header Gmail actually assigned (what replies and bounces quote); known once sent.
    sent_message_id: Mapped[str | None] = mapped_column(String(300))
    approved_at: Mapped[datetime | None]
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    queued_at: Mapped[datetime | None]
    scheduled_for: Mapped[datetime | None]
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime | None]
    sent_at: Mapped[datetime | None]
    gmail_message_id: Mapped[str | None] = mapped_column(String(64))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"))
    error_class: Mapped[str | None] = mapped_column(String(120))
    error_detail: Mapped[str | None] = mapped_column(Text)
    skip_reason: Mapped[str | None] = mapped_column(Text)
    bounced_at: Mapped[datetime | None]
    bounce_kind: Mapped[str | None] = mapped_column(String(16))  # hard | soft


class IncomingReply(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """An inbound message from the mailbox. Unique per (account, Gmail message id) ⇒ never processed twice."""

    __tablename__ = "incoming_replies"
    __table_args__ = (
        UniqueConstraint("account_id", "gmail_message_id"),
        Index("ix_incoming_replies_status", "workspace_id", "processing_status"),
        Index("ix_incoming_replies_received", "workspace_id", "received_at"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connected_email_accounts.id", ondelete="CASCADE")
    )
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_threads.id", ondelete="SET NULL")
    )
    in_response_to_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_messages.id", ondelete="SET NULL")
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    gmail_message_id: Mapped[str] = mapped_column(String(64))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(64))
    rfc_message_id: Mapped[str | None] = mapped_column(Text)
    in_reply_to: Mapped[str | None] = mapped_column(Text)
    from_email: Mapped[str] = mapped_column(String(320))
    from_name: Mapped[str | None] = mapped_column(String(300))
    subject: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime]
    snippet: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)  # sanitised plain text, length-capped
    kind: Mapped[ReplyKind] = mapped_column(enum_col(ReplyKind), default=ReplyKind.UNKNOWN)
    match_method: Mapped[str | None] = mapped_column(
        String(32)
    )  # thread_id | in_reply_to | heuristic | none
    match_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_OBJ)
    processing_status: Mapped[ReplyProcessingStatus] = mapped_column(
        enum_col(ReplyProcessingStatus), default=ReplyProcessingStatus.PENDING
    )
    processed_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)


class ReplyClassification(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    __tablename__ = "reply_classifications"
    __table_args__ = (
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_range"),
        Index("ix_reply_classifications_category", "workspace_id", "category"),
    )

    reply_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incoming_replies.id", ondelete="CASCADE"), unique=True
    )
    category: Mapped[ReplyCategory] = mapped_column(enum_col(ReplyCategory))
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    explanation: Mapped[str] = mapped_column(Text)
    extracted: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_OBJ
    )
    suggested_action: Mapped[str | None] = mapped_column(String(300))
    needs_human: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    deterministic: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    provider_key: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(120))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    owner_override_category: Mapped[ReplyCategory | None] = mapped_column(
        enum_col(ReplyCategory, name="replycategory_override")
    )
    reviewed_at: Mapped[datetime | None]


class FollowUpSchedule(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    __tablename__ = "follow_up_schedules"
    __table_args__ = (
        UniqueConstraint("lead_id", "step_no"),
        CheckConstraint("step_no >= 1", name="step_positive"),
        Index(
            "ix_follow_up_schedules_due",
            "due_at",
            postgresql_where=text("status = 'scheduled'"),
        ),
    )

    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"))
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    anchor_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_messages.id", ondelete="SET NULL")
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_messages.id", ondelete="SET NULL")
    )
    step_no: Mapped[int] = mapped_column(SmallInteger)
    due_at: Mapped[datetime]
    status: Mapped[FollowUpScheduleStatus] = mapped_column(
        enum_col(FollowUpScheduleStatus), default=FollowUpScheduleStatus.SCHEDULED
    )
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    evaluated_at: Mapped[datetime | None]
