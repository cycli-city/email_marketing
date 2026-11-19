"""Campaigns, their settings, and email templates."""

from __future__ import annotations

import uuid
from datetime import datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, WorkspaceScopedMixin
from app.db.types import enum_col
from app.domain.enums import ApprovalMode, CampaignMode, CampaignStatus, TemplateKind

_EMPTY_OBJ = text("'{}'::jsonb")


class EmailTemplate(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Editable template. Messages snapshot the rendered content, so edits never rewrite history."""

    __tablename__ = "email_templates"
    __table_args__ = (UniqueConstraint("workspace_id", "mode", "name"),)

    mode: Mapped[CampaignMode] = mapped_column(enum_col(CampaignMode))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[TemplateKind] = mapped_column(enum_col(TemplateKind), default=TemplateKind.INITIAL)
    subject_template: Mapped[str] = mapped_column(Text)
    body_template: Mapped[str] = mapped_column(Text)
    # Free-text steering for the AI personalisation pass (tone, angle, do/don't).
    instructions: Mapped[str | None] = mapped_column(Text)
    ai_personalize: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))


class Campaign(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    __tablename__ = "campaigns"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        CheckConstraint("target_qualified > 0", name="target_positive"),
    )

    name: Mapped[str] = mapped_column(String(200))
    mode: Mapped[CampaignMode] = mapped_column(enum_col(CampaignMode))
    objective: Mapped[str | None] = mapped_column(Text)
    status: Mapped[CampaignStatus] = mapped_column(
        enum_col(CampaignStatus), default=CampaignStatus.DRAFT
    )
    target_audience: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_OBJ
    )
    discovery_criteria: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_OBJ
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_templates.id", ondelete="SET NULL")
    )
    sender_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("connected_email_accounts.id", ondelete="SET NULL")
    )
    target_qualified: Mapped[int] = mapped_column(Integer, default=100, server_default=text("100"))
    max_emails_total: Mapped[int | None] = mapped_column(Integer)
    approval_mode: Mapped[ApprovalMode] = mapped_column(
        enum_col(ApprovalMode), default=ApprovalMode.MANUAL_EACH
    )
    # One-time authorisation (REQUIREMENTS_AUTONOMY §I): what exactly the owner approved, and when.
    authorized_at: Mapped[datetime | None]
    authorized_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    authorization_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_OBJ
    )
    pause_reason: Mapped[str | None] = mapped_column(Text)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    failed_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    last_tick_at: Mapped[datetime | None]


class CampaignSettings(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    __tablename__ = "campaign_settings"
    __table_args__ = (
        CheckConstraint("daily_limit >= 0 AND hourly_limit >= 0", name="limits_non_negative"),
        CheckConstraint("max_followups BETWEEN 0 AND 5", name="followups_range"),
        CheckConstraint("min_lead_score BETWEEN 0 AND 100", name="min_score_range"),
        CheckConstraint("bounce_pause_threshold BETWEEN 0 AND 1", name="bounce_threshold_range"),
    )

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), unique=True
    )
    daily_limit: Mapped[int] = mapped_column(Integer, default=30, server_default=text("30"))
    hourly_limit: Mapped[int] = mapped_column(Integer, default=8, server_default=text("8"))
    min_gap_seconds: Mapped[int] = mapped_column(Integer, default=90, server_default=text("90"))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", server_default="UTC")
    send_window_start: Mapped[time] = mapped_column(
        Time, default=time(9, 0), server_default=text("'09:00'")
    )
    send_window_end: Mapped[time] = mapped_column(
        Time, default=time(17, 0), server_default=text("'17:00'")
    )
    # ISO weekdays, Monday=1 … Sunday=7.
    send_days: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), default=lambda: [1, 2, 3, 4, 5], server_default=text("'{1,2,3,4,5}'")
    )
    followup_enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    max_followups: Mapped[int] = mapped_column(SmallInteger, default=2, server_default=text("2"))
    followup_1_delay_business_days: Mapped[int] = mapped_column(
        SmallInteger, default=4, server_default=text("4")
    )
    followup_2_delay_business_days: Mapped[int] = mapped_column(
        SmallInteger, default=7, server_default=text("7")
    )
    bounce_pause_threshold: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), default=Decimal("0.05"), server_default=text("0.05")
    )
    bounce_min_sample: Mapped[int] = mapped_column(Integer, default=20, server_default=text("20"))
    require_verified_email: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    allow_role_addresses: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    min_lead_score: Mapped[int] = mapped_column(SmallInteger, default=50, server_default=text("50"))
    resume_role_key: Mapped[str | None] = mapped_column(String(64))  # Job Hunter: which CV variant
    jurisdiction_overrides: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_OBJ
    )
