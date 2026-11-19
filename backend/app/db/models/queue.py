"""The durable job queue. Postgres is the queue (ADR-024): rows are claimed with ``FOR UPDATE SKIP LOCKED``."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, WorkspaceScopedMixin
from app.db.types import enum_col
from app.domain.enums import JobStatus


class Job(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    __tablename__ = "job_queue"
    __table_args__ = (
        # At most ONE active job per dedupe key: re-enqueuing the same deterministic key is a no-op while the job
        # is queued/running, and allowed again once it has finished.
        Index(
            "uq_job_queue_active_dedupe",
            "workspace_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL AND status IN ('queued', 'running')"),
        ),
        Index(
            "ix_job_queue_claim", "priority", "run_at", postgresql_where=text("status = 'queued'")
        ),
        Index("ix_job_queue_lease", "locked_until", postgresql_where=text("status = 'running'")),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="attempts_valid"),
    )

    task_name: Mapped[str] = mapped_column(String(120))
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    dedupe_key: Mapped[str | None] = mapped_column(String(300))
    priority: Mapped[int] = mapped_column(SmallInteger, default=100, server_default=text("100"))
    status: Mapped[JobStatus] = mapped_column(enum_col(JobStatus), default=JobStatus.QUEUED)
    run_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(SmallInteger, default=5, server_default=text("5"))
    # Lease: a running job is owned by `locked_by` until `locked_until`; an expired lease means the worker died.
    locked_by: Mapped[str | None] = mapped_column(String(80))
    locked_until: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_class: Mapped[str | None] = mapped_column(String(160))
    parked_reason: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    cancel_requested: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
