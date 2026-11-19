"""Postgres-backed job queue and in-process worker (ADR-024)."""

from app.queue.errors import (
    JobCancelled,
    JobError,
    LeaseLostError,
    ParkJob,
    PermanentJobError,
    RetryableJobError,
)
from app.queue.service import JobQueue
from app.queue.types import (
    CancelOutcome,
    ClaimedJob,
    EnqueueResult,
    FailOutcome,
    Heartbeat,
    QueueStats,
    ReapResult,
)
from app.queue.worker import (
    JobContext,
    PeriodicSchedule,
    TaskRegistry,
    Worker,
    WorkerConfig,
)

__all__ = [
    "CancelOutcome",
    "ClaimedJob",
    "EnqueueResult",
    "FailOutcome",
    "Heartbeat",
    "JobCancelled",
    "JobContext",
    "JobError",
    "JobQueue",
    "LeaseLostError",
    "ParkJob",
    "PeriodicSchedule",
    "PermanentJobError",
    "QueueStats",
    "ReapResult",
    "RetryableJobError",
    "TaskRegistry",
    "Worker",
    "WorkerConfig",
]
