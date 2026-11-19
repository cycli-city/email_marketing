"""Connected mailboxes, OAuth tokens, Gmail sync cursors, integration credentials, sender identities, CVs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, WorkspaceScopedMixin
from app.db.types import enum_col
from app.domain.enums import AccountProvider, AccountStatus, AccountType, CampaignMode


class ConnectedEmailAccount(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """A mailbox we can send from / read. One account serves exactly one mode (separate identities)."""

    __tablename__ = "connected_email_accounts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "provider", "email_address"),
        CheckConstraint("daily_limit >= 0 AND hourly_limit >= 0", name="limits_non_negative"),
    )

    provider: Mapped[AccountProvider] = mapped_column(enum_col(AccountProvider))
    mode: Mapped[CampaignMode] = mapped_column(enum_col(CampaignMode))
    email_address: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(200))
    account_type: Mapped[AccountType] = mapped_column(
        enum_col(AccountType), default=AccountType.PERSONAL_GMAIL
    )
    status: Mapped[AccountStatus] = mapped_column(
        enum_col(AccountStatus), default=AccountStatus.ACTIVE
    )
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String), default=list, server_default=text("'{}'")
    )
    # Conservative defaults for a personal Gmail; the sending policy clamps these to hard ceilings.
    daily_limit: Mapped[int] = mapped_column(Integer, default=30, server_default=text("30"))
    hourly_limit: Mapped[int] = mapped_column(Integer, default=8, server_default=text("8"))
    warmup_enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    warmup_started_at: Mapped[datetime | None]
    reply_to: Mapped[str | None] = mapped_column(String(320))
    signature_text: Mapped[str | None] = mapped_column(Text)
    signature_html: Mapped[str | None] = mapped_column(Text)
    connected_at: Mapped[datetime | None]
    last_sync_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None]
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class OAuthToken(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Fernet-encrypted OAuth credentials. Never serialised to any API response."""

    __tablename__ = "oauth_tokens"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connected_email_accounts.id", ondelete="CASCADE"), unique=True
    )
    access_token_enc: Mapped[str | None] = mapped_column(Text)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None]
    scope: Mapped[str | None] = mapped_column(Text)
    token_type: Mapped[str | None] = mapped_column(String(32))
    last_refreshed_at: Mapped[datetime | None]


class WatchState(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Gmail inbox sync cursor: ``history_id`` for History API polling, ``watch_expiration`` for Pub/Sub."""

    __tablename__ = "watch_states"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connected_email_accounts.id", ondelete="CASCADE"), unique=True
    )
    history_id: Mapped[str | None] = mapped_column(String(64))
    watch_expiration: Mapped[datetime | None]
    last_notification_at: Mapped[datetime | None]
    last_polled_at: Mapped[datetime | None]
    last_full_sync_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)


class IntegrationSetting(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """API credentials/config entered through the UI. Environment variables remain the fallback."""

    __tablename__ = "integration_settings"
    __table_args__ = (UniqueConstraint("workspace_id", "provider_key"),)

    provider_key: Mapped[str] = mapped_column(String(64))
    credentials_enc: Mapped[str | None] = mapped_column(Text)  # Fernet(JSON)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )  # non-secret settings (model name, region, …)
    enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))


class ResumeAsset(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """One CV variant per target role, hosted on Google Drive (ADR-011)."""

    __tablename__ = "resume_assets"
    __table_args__ = (UniqueConstraint("workspace_id", "role_key"),)

    role_key: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(200))
    drive_url: Mapped[str] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))


class SenderProfile(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Per-mode sender identity: how we sign, where we're based, which facts we may cite."""

    __tablename__ = "sender_profiles"
    __table_args__ = (UniqueConstraint("workspace_id", "mode"),)

    mode: Mapped[CampaignMode] = mapped_column(enum_col(CampaignMode))
    display_name: Mapped[str] = mapped_column(String(200))
    reply_to: Mapped[str | None] = mapped_column(String(320))
    signature_text: Mapped[str | None] = mapped_column(Text)
    postal_address: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(String(300))
    # Grounding facts the generator may cite (product facts for NEXORA, resume facts for Job Hunter).
    profile_facts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
