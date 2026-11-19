"""Value objects returned by the queue service."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


def payload_hash(payload: dict[str, Any]) -> str:
    """Stable fingerprint of a payload (dead-letter identity). Key order never matters."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """A job leased to one worker. ``(id, locked_by, attempt)`` is the fence every later write must match."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    task_name: str
    payload: dict[str, Any]
    dedupe_key: str | None
    priority: int
    attempt: int  # 1 for the first run
    max_attempts: int
    locked_by: str

    @property
    def is_last_attempt(self) -> bool:
        return self.attempt >= self.max_attempts


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job_id: uuid.UUID
    created: bool  # False → an active job with the same dedupe key already existed


class FailOutcome(StrEnum):
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD = "dead"
    CANCELLED = "cancelled"
    LOST = "lost"  # we no longer own the job; nothing was written


class CancelOutcome(StrEnum):
    CANCELLED = "cancelled"  # was queued → cancelled immediately
    REQUESTED = "requested"  # is running → the worker is asked to stop; it will not be retried
    NOT_ACTIVE = "not_active"  # unknown id, other workspace, or already finished


@dataclass(frozen=True, slots=True)
class Heartbeat:
    cancel_requested: bool


@dataclass(frozen=True, slots=True)
class ReapResult:
    requeued: int
    dead: int
    cancelled: int

    @property
    def total(self) -> int:
        return self.requeued + self.dead + self.cancelled


@dataclass(frozen=True, slots=True)
class QueueStats:
    queued: int
    due_now: int  # queued and runnable right now
    scheduled: int  # queued but waiting for run_at (backoff / parked)
    running: int
    expired_leases: int  # running with an expired lease → the reaper will recover them
    dead: int
    succeeded: int
    cancelled: int
    oldest_due_age_seconds: (
        float | None
    )  # None when nothing is due; the "is the queue stuck?" signal
    as_of: datetime
