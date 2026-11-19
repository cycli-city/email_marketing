"""Workspaces and users (single-owner deployment, but tenant-shaped — ADR-012)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, WorkspaceScopedMixin


class Workspace(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", server_default="UTC")
    # Emergency stop: while active, nothing is sent and every campaign is paused (ARCHITECTURE §3.2).
    kill_switch_active: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    kill_switch_reason: Mapped[str | None] = mapped_column(Text)
    kill_switch_at: Mapped[datetime | None]
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("workspace_id", "email"),)

    supabase_user_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(200))
    last_seen_at: Mapped[datetime | None]
