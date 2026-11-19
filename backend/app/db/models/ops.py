"""Operational records: activity feed, audit log, dead letters, notification preferences/logs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    ForeignKey,
    Index,
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
    ActivityLevel,
    ActorType,
    DeadLetterStatus,
    NotificationChannel,
    NotificationStatus,
)

_EMPTY_OBJ = text("'{}'::jsonb")


class ActivityEvent(Base, UUIDPrimaryKeyMixin, WorkspaceScopedMixin):
    """Chronological feed shown on the System Operations page. Written by real work, never seeded."""

    __tablename__ = "activity_events"
    __table_args__ = (Index("ix_activity_events_time", "workspace_id", "created_at"),)

    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    level: Mapped[ActivityLevel] = mapped_column(
        enum_col(ActivityLevel), default=ActivityLevel.INFO
    )
    category: Mapped[str] = mapped_column(
        String(40)
    )  # campaign | discovery | provider | email | reply | system
    event_type: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_OBJ)


class AuditLog(Base, UUIDPrimaryKeyMixin, WorkspaceScopedMixin):
    """Append-only record of sensitive actions (a DB trigger forbids UPDATE/DELETE — see migration)."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_time", "workspace_id", "created_at"),)

    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    actor_type: Mapped[ActorType] = mapped_column(enum_col(ActorType))
    actor_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(120))
    entity_type: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_OBJ)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64))


class DeadLetter(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """A task that permanently failed. Surfaced on the Ops page with retry/discard actions."""

    __tablename__ = "dead_letters"
    __table_args__ = (
        UniqueConstraint("workspace_id", "task_name", "payload_hash"),
        Index("ix_dead_letters_status", "workspace_id", "status"),
    )

    task_name: Mapped[str] = mapped_column(String(120))
    job_id: Mapped[str | None] = mapped_column(String(200))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_OBJ)
    payload_hash: Mapped[str] = mapped_column(String(64))
    error_class: Mapped[str] = mapped_column(String(160))
    error_message: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=1, server_default=text("1"))
    first_failed_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    last_failed_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    status: Mapped[DeadLetterStatus] = mapped_column(
        enum_col(DeadLetterStatus), default=DeadLetterStatus.OPEN
    )
    resolved_at: Mapped[datetime | None]
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class NotificationPreference(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    __tablename__ = "notification_preferences"
    __table_args__ = (UniqueConstraint("workspace_id", "channel"),)

    channel: Mapped[NotificationChannel] = mapped_column(enum_col(NotificationChannel))
    enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    event_types: Mapped[list[str]] = mapped_column(
        ARRAY(String), default=list, server_default=text("'{}'")
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_OBJ)
    quiet_hours: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_OBJ
    )


class NotificationLog(Base, UUIDPrimaryKeyMixin, WorkspaceScopedMixin):
    """Every notification attempt. ``dedupe_key`` makes notifying exactly once per event idempotent."""

    __tablename__ = "notification_logs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "dedupe_key"),
        Index("ix_notification_logs_time", "workspace_id", "created_at"),
    )

    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    channel: Mapped[NotificationChannel] = mapped_column(enum_col(NotificationChannel))
    event_type: Mapped[str] = mapped_column(String(80))
    dedupe_key: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[NotificationStatus] = mapped_column(
        enum_col(NotificationStatus), default=NotificationStatus.PENDING
    )
    provider_key: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    reply_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("incoming_replies.id", ondelete="SET NULL")
    )
    sent_at: Mapped[datetime | None]
