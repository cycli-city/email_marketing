"""Value objects returned by the queue service."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

MAX_ERROR_CHARS = 2000

# Errors are persisted (job_queue.last_error, dead_letters) and shown in the UI, so they must never carry
# credentials. Adapters should not put secrets in exceptions in the first place; this is the safety net.
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|"
        r"authorization|client[_-]?secret)\b\s*[=:]\s*(bearer\s+)?[^\s,;&\"']+"
    ),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._\-~+/]+=*"),
    re.compile(r"\b(?:gsk|sbp|ghp|gho|ghs|github_pat|sk|rk|xox[abp])[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
)


def scrub_error(message: str) -> str:
    """Redact credential-looking substrings and bound the length."""
    cleaned = message
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub(
            lambda m: f"{m.group(1)}=[redacted]" if m.lastindex else "[redacted]", cleaned
        )
    if len(cleaned) > MAX_ERROR_CHARS:
        cleaned = cleaned[:MAX_ERROR_CHARS] + "…[truncated]"
    return cleaned


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
