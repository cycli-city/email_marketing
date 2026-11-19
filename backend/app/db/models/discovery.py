"""Discovery jobs and their per-record results."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, WorkspaceScopedMixin
from app.db.types import enum_col
from app.domain.enums import DiscoveryJobStatus, DiscoveryOutcome

_EMPTY_OBJ = text("'{}'::jsonb")


class DiscoveryJob(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """A resumable discovery run. ``cursor`` records provider/page progress so a restart or a provider
    switch continues instead of starting over — no collected data is ever lost."""

    __tablename__ = "discovery_jobs"
    __table_args__ = (
        CheckConstraint("target_count > 0", name="target_positive"),
        Index("ix_discovery_jobs_status", "workspace_id", "status"),
    )

    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(200))
    prompt: Mapped[str | None] = mapped_column(Text)  # free-text request as typed by the owner
    criteria: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_OBJ)
    target_count: Mapped[int] = mapped_column(Integer, default=50, server_default=text("50"))
    status: Mapped[DiscoveryJobStatus] = mapped_column(
        enum_col(DiscoveryJobStatus), default=DiscoveryJobStatus.PENDING
    )
    cursor: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_OBJ)
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_OBJ)
    companies_found: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    contacts_found: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    duplicates_skipped: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    errors: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    waiting_until: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(default=False, server_default=text("false"))


class DiscoveryJobResult(Base, UUIDPrimaryKeyMixin, WorkspaceScopedMixin):
    __tablename__ = "discovery_job_results"
    __table_args__ = (Index("ix_discovery_job_results_job", "job_id"),)

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("discovery_jobs.id", ondelete="CASCADE"))
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL")
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    outcome: Mapped[DiscoveryOutcome] = mapped_column(enum_col(DiscoveryOutcome))
    provider_key: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_OBJ)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
