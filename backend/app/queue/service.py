"""The Postgres-backed job queue (ADR-024: no Redis).

Guarantees
----------
* **Exactly one claimer per job** — ``FOR UPDATE SKIP LOCKED``: concurrent workers never block each other and never
  receive the same row.
* **Fenced writes** — every state change after a claim must match ``(id, locked_by, attempt)`` while the job is
  ``running``. A "zombie" worker whose lease expired (and whose job was re-claimed) therefore cannot overwrite the
  new owner's outcome; its write is rejected (``False`` / ``FailOutcome.LOST``) and nothing changes.
* **Crash recovery** — a worker that dies leaves ``running`` + an expiring lease; :meth:`requeue_expired` recovers
  it. A crash *consumes* the attempt (a task that kills the process must not loop forever).
* **No lost work, no silent drops** — every job ends ``succeeded`` / ``cancelled`` or ``dead`` (+ a dead-letter row
  for the Ops page). ``ParkJob`` / graceful shutdown hand the attempt back.
* **Deterministic dedupe** — at most one *active* job per ``(workspace, dedupe_key)``; enqueueing it again while
  queued/running is a no-op that returns the existing job.

Time comes from the injected :class:`~app.core.clock.Clock` so every rule is testable with a fake clock.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Collection, Mapping
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import and_, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.core.redact import scrub_error
from app.core.retry import DEFAULT_POLICY, BackoffPolicy, backoff_seconds
from app.db.models import DeadLetter, Job
from app.db.session import session_scope
from app.domain.enums import DeadLetterStatus, JobStatus
from app.queue.types import (
    CancelOutcome,
    ClaimedJob,
    EnqueueResult,
    FailOutcome,
    Heartbeat,
    QueueStats,
    ReapResult,
    payload_hash,
)

log = structlog.get_logger("queue")

_ACTIVE = (JobStatus.QUEUED, JobStatus.RUNNING)
_ACTIVE_DEDUPE_PREDICATE = text("dedupe_key IS NOT NULL AND status IN ('queued', 'running')")
_MAX_ENQUEUE_RACE_RETRIES = 3
LEASE_EXPIRED_MESSAGE = (
    "lease expired: the worker holding this job stopped responding (crash or restart)"
)


class JobQueue:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        *,
        policy: BackoffPolicy = DEFAULT_POLICY,
        rng: random.Random | None = None,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock
        self._policy = policy
        self._rng = rng

    # ───────────────────────────── producing ─────────────────────────────

    async def enqueue(
        self,
        task_name: str,
        payload: Mapping[str, Any] | None = None,
        *,
        workspace_id: uuid.UUID,
        dedupe_key: str | None = None,
        run_at: datetime | None = None,
        priority: int = 100,
        max_attempts: int = 5,
        session: AsyncSession | None = None,
    ) -> EnqueueResult:
        """Add a job. Pass ``session`` to enqueue **in the caller's transaction** (the outbox pattern: the state
        change and its follow-up job commit — or roll back — together, so neither is ever lost or orphaned)."""
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if run_at is not None and run_at.tzinfo is None:
            raise ValueError("run_at must be timezone-aware")
        values: dict[str, Any] = {
            "workspace_id": workspace_id,
            "task_name": task_name,
            "payload": dict(payload or {}),
            "dedupe_key": dedupe_key,
            "priority": priority,
            "status": JobStatus.QUEUED,
            "run_at": run_at or self._clock.now(),
            "attempts": 0,
            "max_attempts": max_attempts,
            "cancel_requested": False,
        }
        if session is not None:
            return await self._enqueue(session, values)
        async with session_scope(self._sessions) as own:
            return await self._enqueue(own, values)

    async def _enqueue(self, session: AsyncSession, values: dict[str, Any]) -> EnqueueResult:
        dedupe_key: str | None = values["dedupe_key"]
        if dedupe_key is None:
            inserted = await session.execute(pg_insert(Job).values(**values).returning(Job.id))
            return EnqueueResult(job_id=inserted.scalar_one(), created=True)

        # The unique index only covers *active* jobs, so a finished job never blocks a re-enqueue. If the
        # existing job finishes between our failed insert and the lookup, simply try again.
        for _ in range(_MAX_ENQUEUE_RACE_RETRIES):
            # A SAVEPOINT keeps a conflict from poisoning the caller's transaction.
            async with session.begin_nested():
                inserted = await session.execute(
                    pg_insert(Job)
                    .values(**values)
                    .on_conflict_do_nothing(
                        index_elements=["workspace_id", "dedupe_key"],
                        index_where=_ACTIVE_DEDUPE_PREDICATE,
                    )
                    .returning(Job.id)
                )
                new_id = inserted.scalar_one_or_none()
            if new_id is not None:
                return EnqueueResult(job_id=new_id, created=True)
            existing = await session.execute(
                select(Job.id).where(
                    Job.workspace_id == values["workspace_id"],
                    Job.dedupe_key == dedupe_key,
                    Job.status.in_(_ACTIVE),
                )
            )
            existing_id = existing.scalar_one_or_none()
            if existing_id is not None:
                return EnqueueResult(job_id=existing_id, created=False)
        raise RuntimeError(f"could not enqueue or find an active job for dedupe key {dedupe_key!r}")

    # ───────────────────────────── claiming ─────────────────────────────

    async def claim(
        self,
        worker_id: str,
        *,
        limit: int = 1,
        lease_seconds: float = 60.0,
        task_names: Collection[str] | None = None,
    ) -> list[ClaimedJob]:
        """Atomically lease up to ``limit`` due jobs (highest priority, then oldest ``run_at``).

        ``task_names`` restricts the claim to tasks this worker can actually run; others stay queued untouched.
        """
        if limit < 1:
            return []
        if task_names is not None and not task_names:
            return []
        now = self._clock.now()
        due = select(Job.id).where(Job.status == JobStatus.QUEUED, Job.run_at <= now)
        if task_names is not None:
            due = due.where(Job.task_name.in_(list(task_names)))
        due = (
            due.order_by(Job.priority, Job.run_at, Job.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        stmt = (
            update(Job)
            .where(Job.id.in_(due))
            .values(
                status=JobStatus.RUNNING,
                locked_by=worker_id,
                locked_until=now + timedelta(seconds=lease_seconds),
                started_at=now,
                attempts=Job.attempts + 1,
                parked_reason=None,
                updated_at=now,
            )
            .returning(Job)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        async with session_scope(self._sessions) as session:
            rows = (await session.execute(stmt)).scalars().all()
        rows = sorted(rows, key=lambda j: (j.priority, j.run_at, str(j.id)))
        return [
            ClaimedJob(
                id=row.id,
                workspace_id=row.workspace_id,
                task_name=row.task_name,
                payload=dict(row.payload),
                dedupe_key=row.dedupe_key,
                priority=row.priority,
                attempt=row.attempts,
                max_attempts=row.max_attempts,
                locked_by=worker_id,
            )
            for row in rows
        ]

    # ───────────────────────── fenced state changes ─────────────────────────

    @staticmethod
    def _fence(job: ClaimedJob) -> Any:
        return and_(
            Job.id == job.id,
            Job.status == JobStatus.RUNNING,
            Job.locked_by == job.locked_by,
            Job.attempts == job.attempt,
        )

    async def _fenced_update(self, job: ClaimedJob, **values: Any) -> bool:
        stmt = (
            update(Job)
            .where(self._fence(job))
            .values(locked_by=None, locked_until=None, updated_at=self._clock.now(), **values)
            .returning(Job.id)
            .execution_options(synchronize_session=False)
        )
        async with session_scope(self._sessions) as session:
            return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def heartbeat(self, job: ClaimedJob, *, lease_seconds: float = 60.0) -> Heartbeat | None:
        """Extend the lease. ``None`` means the lease is gone — stop working on this job immediately."""
        now = self._clock.now()
        stmt = (
            update(Job)
            .where(self._fence(job))
            .values(locked_until=now + timedelta(seconds=lease_seconds), updated_at=now)
            .returning(Job.cancel_requested)
            .execution_options(synchronize_session=False)
        )
        async with session_scope(self._sessions) as session:
            row = (await session.execute(stmt)).first()
        return None if row is None else Heartbeat(cancel_requested=bool(row[0]))

    async def complete(self, job: ClaimedJob, result: Mapping[str, Any] | None = None) -> bool:
        ok = await self._fenced_update(
            job,
            status=JobStatus.SUCCEEDED,
            finished_at=self._clock.now(),
            result=dict(result) if result is not None else None,
            last_error=None,
            last_error_class=None,
        )
        if not ok:
            log.warning(
                "queue.complete_rejected",
                job_id=str(job.id),
                task=job.task_name,
                reason="lease lost",
            )
        return ok

    async def park(
        self, job: ClaimedJob, reason: str, *, until: datetime, hold_attempt: bool = True
    ) -> bool:
        """Put a running job back to wait until ``until``. By default the attempt is **not** consumed."""
        return await self._fenced_update(
            job,
            status=JobStatus.QUEUED,
            run_at=until,
            attempts=Job.attempts - 1 if hold_attempt else Job.attempts,
            parked_reason=scrub_error(reason),
        )

    async def release(self, job: ClaimedJob) -> bool:
        """Graceful shutdown: hand the job straight back, attempt not consumed, runnable immediately."""
        return await self._fenced_update(
            job,
            status=JobStatus.QUEUED,
            run_at=self._clock.now(),
            attempts=Job.attempts - 1,
            parked_reason=None,
        )

    async def mark_cancelled(self, job: ClaimedJob) -> bool:
        return await self._fenced_update(
            job, status=JobStatus.CANCELLED, finished_at=self._clock.now()
        )

    async def fail(
        self,
        job: ClaimedJob,
        error: BaseException | str,
        *,
        retry_after: float | None = None,
        permanent: bool = False,
    ) -> FailOutcome:
        """Record a failed attempt: retry with backoff, or dead-letter when out of attempts / permanent."""
        error_class = type(error).__name__ if isinstance(error, BaseException) else "Error"
        message = scrub_error(str(error) or error_class)
        now = self._clock.now()
        async with session_scope(self._sessions) as session:
            row = (
                await session.execute(select(Job).where(self._fence(job)).with_for_update())
            ).scalar_one_or_none()
            if row is None:
                log.warning(
                    "queue.fail_rejected",
                    job_id=str(job.id),
                    task=job.task_name,
                    reason="lease lost",
                )
                return FailOutcome.LOST
            row.last_error, row.last_error_class = message, error_class
            row.locked_by = row.locked_until = None
            row.updated_at = now
            if row.cancel_requested:
                row.status, row.finished_at = JobStatus.CANCELLED, now
                return FailOutcome.CANCELLED
            if permanent or row.attempts >= row.max_attempts:
                await self._bury(session, row, now)
                return FailOutcome.DEAD
            delay = backoff_seconds(
                self._policy, row.attempts, retry_after=retry_after, rng=self._rng
            )
            row.status = JobStatus.QUEUED
            row.run_at = now + timedelta(seconds=delay)
        log.info(
            "queue.retry_scheduled",
            job_id=str(job.id),
            task=job.task_name,
            attempt=job.attempt,
            retry_in_s=round(delay, 1),
            error_class=error_class,
        )
        return FailOutcome.RETRY_SCHEDULED

    async def _bury(self, session: AsyncSession, row: Job, now: datetime) -> None:
        """Mark ``row`` dead and upsert its dead-letter record (same task+payload ⇒ one record, counted up)."""
        row.status, row.finished_at = JobStatus.DEAD, now
        digest = payload_hash(row.payload)
        values = {
            "workspace_id": row.workspace_id,
            "task_name": row.task_name,
            "job_id": str(row.id),
            "payload": row.payload,
            "payload_hash": digest,
            "error_class": row.last_error_class or "Error",
            "error_message": row.last_error or "",
            "attempts": row.attempts,
            "first_failed_at": now,
            "last_failed_at": now,
            "status": DeadLetterStatus.OPEN,
        }
        stmt = pg_insert(DeadLetter).values(**values)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["workspace_id", "task_name", "payload_hash"],
                set_={
                    "job_id": stmt.excluded.job_id,
                    "error_class": stmt.excluded.error_class,
                    "error_message": stmt.excluded.error_message,
                    "attempts": DeadLetter.attempts + stmt.excluded.attempts,
                    "last_failed_at": now,
                    "updated_at": now,
                    "status": DeadLetterStatus.OPEN,  # failing again reopens a resolved record
                    "resolved_at": None,
                    "resolved_by": None,
                },
            )
        )
        log.error(
            "queue.dead_lettered",
            job_id=str(row.id),
            task=row.task_name,
            attempts=row.attempts,
            error_class=row.last_error_class,
        )

    # ───────────────────────────── operator actions ─────────────────────────────

    async def cancel(self, job_id: uuid.UUID, *, workspace_id: uuid.UUID) -> CancelOutcome:
        now = self._clock.now()
        async with session_scope(self._sessions) as session:
            queued = await session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.workspace_id == workspace_id,
                    Job.status == JobStatus.QUEUED,
                )
                .values(status=JobStatus.CANCELLED, finished_at=now, updated_at=now)
                .returning(Job.id)
            )
            if queued.scalar_one_or_none() is not None:
                return CancelOutcome.CANCELLED
            running = await session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.workspace_id == workspace_id,
                    Job.status == JobStatus.RUNNING,
                )
                .values(cancel_requested=True, updated_at=now)
                .returning(Job.id)
            )
            if running.scalar_one_or_none() is not None:
                return CancelOutcome.REQUESTED
        return CancelOutcome.NOT_ACTIVE

    async def retry_dead_letter(
        self,
        dead_letter_id: uuid.UUID,
        *,
        workspace_id: uuid.UUID,
        resolved_by: uuid.UUID | None = None,
    ) -> uuid.UUID | None:
        """Ops-page "retry": enqueue a fresh job from an open dead letter. ``None`` if it isn't open."""
        now = self._clock.now()
        async with session_scope(self._sessions) as session:
            letter = (
                await session.execute(
                    select(DeadLetter)
                    .where(
                        DeadLetter.id == dead_letter_id,
                        DeadLetter.workspace_id == workspace_id,
                        DeadLetter.status == DeadLetterStatus.OPEN,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if letter is None:
                return None
            created = await self._enqueue(
                session,
                {
                    "workspace_id": workspace_id,
                    "task_name": letter.task_name,
                    "payload": dict(letter.payload),
                    "dedupe_key": None,
                    "priority": 100,
                    "status": JobStatus.QUEUED,
                    "run_at": now,
                    "attempts": 0,
                    "max_attempts": 5,
                    "cancel_requested": False,
                },
            )
            letter.status = DeadLetterStatus.RETRIED
            letter.resolved_at = now
            letter.resolved_by = resolved_by
            return created.job_id

    async def discard_dead_letter(
        self,
        dead_letter_id: uuid.UUID,
        *,
        workspace_id: uuid.UUID,
        resolved_by: uuid.UUID | None = None,
    ) -> bool:
        now = self._clock.now()
        async with session_scope(self._sessions) as session:
            result = await session.execute(
                update(DeadLetter)
                .where(
                    DeadLetter.id == dead_letter_id,
                    DeadLetter.workspace_id == workspace_id,
                    DeadLetter.status == DeadLetterStatus.OPEN,
                )
                .values(
                    status=DeadLetterStatus.DISCARDED,
                    resolved_at=now,
                    resolved_by=resolved_by,
                    updated_at=now,
                )
                .returning(DeadLetter.id)
            )
            return result.scalar_one_or_none() is not None

    # ───────────────────────────── maintenance ─────────────────────────────

    async def requeue_expired(self, *, limit: int = 100) -> ReapResult:
        """Recover jobs whose worker vanished (lease expired). Safe to run from every process, any time."""
        now = self._clock.now()
        requeued = dead = cancelled = 0
        async with session_scope(self._sessions) as session:
            rows = (
                (
                    await session.execute(
                        select(Job)
                        .where(Job.status == JobStatus.RUNNING, Job.locked_until < now)
                        .order_by(Job.locked_until)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.last_error_class, row.last_error = "LeaseExpired", LEASE_EXPIRED_MESSAGE
                row.locked_by = row.locked_until = None
                row.updated_at = now
                if row.cancel_requested:
                    row.status, row.finished_at = JobStatus.CANCELLED, now
                    cancelled += 1
                elif row.attempts >= row.max_attempts:
                    await self._bury(session, row, now)
                    dead += 1
                else:
                    delay = backoff_seconds(self._policy, row.attempts, rng=self._rng)
                    row.status = JobStatus.QUEUED
                    row.run_at = now + timedelta(seconds=delay)
                    requeued += 1
        result = ReapResult(requeued=requeued, dead=dead, cancelled=cancelled)
        if result.total:
            log.warning(
                "queue.recovered_expired_leases", requeued=requeued, dead=dead, cancelled=cancelled
            )
        return result

    async def purge_finished(
        self, *, succeeded_older_than_days: float = 7, dead_older_than_days: float = 30
    ) -> int:
        """Retention (Supabase free tier is ~500 MB): drop old finished jobs. Dead-letter rows are kept."""
        now = self._clock.now()
        ok_cutoff = now - timedelta(days=succeeded_older_than_days)
        dead_cutoff = now - timedelta(days=dead_older_than_days)
        stmt = (
            delete(Job)
            .where(
                (
                    Job.status.in_((JobStatus.SUCCEEDED, JobStatus.CANCELLED))
                    & (Job.finished_at < ok_cutoff)
                )
                | ((Job.status == JobStatus.DEAD) & (Job.finished_at < dead_cutoff))
            )
            .returning(Job.id)
        )
        async with session_scope(self._sessions) as session:
            return len((await session.execute(stmt)).all())

    async def stats(self, *, workspace_id: uuid.UUID | None = None) -> QueueStats:
        now = self._clock.now()
        queued = Job.status == JobStatus.QUEUED
        running = Job.status == JobStatus.RUNNING
        stmt = select(
            func.count().filter(queued),
            func.count().filter(queued & (Job.run_at <= now)),
            func.count().filter(running),
            func.count().filter(running & (Job.locked_until < now)),
            func.count().filter(Job.status == JobStatus.DEAD),
            func.count().filter(Job.status == JobStatus.SUCCEEDED),
            func.count().filter(Job.status == JobStatus.CANCELLED),
            func.min(Job.run_at).filter(queued & (Job.run_at <= now)),
        )
        if workspace_id is not None:
            stmt = stmt.where(Job.workspace_id == workspace_id)
        async with session_scope(self._sessions) as session:
            q, due, run, expired, dead, ok, cancelled, oldest = (await session.execute(stmt)).one()
        return QueueStats(
            queued=q,
            due_now=due,
            scheduled=q - due,
            running=run,
            expired_leases=expired,
            dead=dead,
            succeeded=ok,
            cancelled=cancelled,
            oldest_due_age_seconds=None
            if oldest is None
            else max(0.0, (now - oldest).total_seconds()),
            as_of=now,
        )
