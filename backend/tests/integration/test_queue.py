"""Job queue guarantees, proven against a real PostgreSQL with a fake clock (ADR-024)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FakeClock
from app.core.retry import BackoffPolicy
from app.db.models import DeadLetter, Job
from app.db.session import session_scope
from app.domain.enums import DeadLetterStatus, JobStatus
from app.queue import CancelOutcome, FailOutcome, JobQueue

Factory = async_sessionmaker[AsyncSession]

# Deterministic backoff: 10 s, 20 s, 40 s …
POLICY = BackoffPolicy(base_seconds=10, factor=2, max_seconds=600, jitter="none")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest_asyncio.fixture
async def queue(factory: Factory, clock: FakeClock) -> JobQueue:
    return JobQueue(factory, clock, policy=POLICY)


async def fetch(factory: Factory, job_id: uuid.UUID) -> Job:
    async with factory() as s:
        return (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()


async def dead_letters(factory: Factory) -> list[DeadLetter]:
    async with factory() as s:
        return list((await s.execute(select(DeadLetter).order_by(DeadLetter.created_at))).scalars())


async def count_jobs(factory: Factory) -> int:
    async with factory() as s:
        return (await s.execute(select(func.count()).select_from(Job))).scalar_one()


# ───────────────────────────────── enqueue ─────────────────────────────────


class TestEnqueue:
    async def test_creates_a_queued_job_with_defaults(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID, clock: FakeClock
    ) -> None:
        res = await queue.enqueue("discover", {"query": "dentists"}, workspace_id=workspace_id)
        job = await fetch(factory, res.job_id)
        assert res.created
        assert (job.status, job.attempts, job.max_attempts, job.priority) == (
            JobStatus.QUEUED,
            0,
            5,
            100,
        )
        assert job.payload == {"query": "dentists"}
        assert job.run_at == clock.now()
        assert job.locked_by is None

    async def test_active_dedupe_key_returns_the_existing_job(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
    ) -> None:
        first = await queue.enqueue("poll", workspace_id=workspace_id, dedupe_key="poll:inbox")
        second = await queue.enqueue("poll", workspace_id=workspace_id, dedupe_key="poll:inbox")
        assert first.created
        assert not second.created
        assert second.job_id == first.job_id
        assert await count_jobs(factory) == 1

    async def test_dedupe_also_covers_a_running_job(
        self, queue: JobQueue, workspace_id: uuid.UUID
    ) -> None:
        first = await queue.enqueue("poll", workspace_id=workspace_id, dedupe_key="k")
        await queue.claim("w1")
        again = await queue.enqueue("poll", workspace_id=workspace_id, dedupe_key="k")
        assert not again.created
        assert again.job_id == first.job_id

    async def test_dedupe_keys_are_scoped_per_workspace(
        self, queue: JobQueue, workspace_id: uuid.UUID, other_workspace_id: uuid.UUID
    ) -> None:
        a = await queue.enqueue("poll", workspace_id=workspace_id, dedupe_key="k")
        b = await queue.enqueue("poll", workspace_id=other_workspace_id, dedupe_key="k")
        assert a.created
        assert b.created
        assert a.job_id != b.job_id

    async def test_a_finished_job_no_longer_blocks_its_dedupe_key(
        self, queue: JobQueue, workspace_id: uuid.UUID
    ) -> None:
        first = await queue.enqueue("poll", workspace_id=workspace_id, dedupe_key="k")
        (job,) = await queue.claim("w1")
        assert await queue.complete(job)
        again = await queue.enqueue("poll", workspace_id=workspace_id, dedupe_key="k")
        assert again.created
        assert again.job_id != first.job_id

    async def test_jobs_without_a_dedupe_key_never_collide(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
    ) -> None:
        for _ in range(3):
            assert (await queue.enqueue("x", workspace_id=workspace_id)).created
        assert await count_jobs(factory) == 3

    async def test_joins_the_callers_transaction_and_rolls_back_with_it(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
    ) -> None:
        async with factory() as s:
            await queue.enqueue("x", workspace_id=workspace_id, dedupe_key="k", session=s)
            await s.rollback()
        assert await count_jobs(factory) == 0

    async def test_a_dedupe_conflict_does_not_poison_the_callers_transaction(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
    ) -> None:
        async with session_scope(factory) as s:
            a = await queue.enqueue("x", workspace_id=workspace_id, dedupe_key="k", session=s)
            b = await queue.enqueue("x", workspace_id=workspace_id, dedupe_key="k", session=s)
            c = await queue.enqueue("x", workspace_id=workspace_id, dedupe_key="other", session=s)
        assert (a.created, b.created, c.created) == (True, False, True)
        assert await count_jobs(factory) == 2

    async def test_rejects_naive_datetimes_and_bad_attempt_limits(
        self, queue: JobQueue, workspace_id: uuid.UUID
    ) -> None:
        from datetime import datetime

        with pytest.raises(ValueError, match="timezone-aware"):
            await queue.enqueue("x", workspace_id=workspace_id, run_at=datetime(2026, 1, 1))
        with pytest.raises(ValueError, match="max_attempts"):
            await queue.enqueue("x", workspace_id=workspace_id, max_attempts=0)


# ───────────────────────────────── claim ─────────────────────────────────


class TestClaim:
    async def test_orders_by_priority_then_run_at(
        self, queue: JobQueue, workspace_id: uuid.UUID, clock: FakeClock
    ) -> None:
        now = clock.now()
        low = await queue.enqueue("t", {"n": "low"}, workspace_id=workspace_id, priority=200)
        old = await queue.enqueue(
            "t", {"n": "old"}, workspace_id=workspace_id, run_at=now - timedelta(hours=2)
        )
        new = await queue.enqueue(
            "t", {"n": "new"}, workspace_id=workspace_id, run_at=now - timedelta(hours=1)
        )
        urgent = await queue.enqueue("t", {"n": "urgent"}, workspace_id=workspace_id, priority=1)
        claimed = await queue.claim("w1", limit=10)
        assert [j.id for j in claimed] == [urgent.job_id, old.job_id, new.job_id, low.job_id]

    async def test_does_not_claim_jobs_that_are_not_due_yet(
        self, queue: JobQueue, workspace_id: uuid.UUID, clock: FakeClock
    ) -> None:
        await queue.enqueue(
            "t", workspace_id=workspace_id, run_at=clock.now() + timedelta(minutes=5)
        )
        assert await queue.claim("w1") == []
        clock.advance(minutes=5)
        assert len(await queue.claim("w1")) == 1

    async def test_claim_leases_the_job_and_consumes_an_attempt(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID, clock: FakeClock
    ) -> None:
        await queue.enqueue("t", workspace_id=workspace_id, max_attempts=3)
        (job,) = await queue.claim("worker-a", lease_seconds=45)
        row = await fetch(factory, job.id)
        assert (job.attempt, job.max_attempts, job.locked_by) == (1, 3, "worker-a")
        assert row.status == JobStatus.RUNNING
        assert row.locked_until == clock.now() + timedelta(seconds=45)
        assert row.started_at == clock.now()

    async def test_task_name_filter_leaves_other_tasks_untouched(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
    ) -> None:
        mine = await queue.enqueue("mine", workspace_id=workspace_id)
        theirs = await queue.enqueue("theirs", workspace_id=workspace_id)
        claimed = await queue.claim("w1", limit=10, task_names={"mine"})
        assert [j.id for j in claimed] == [mine.job_id]
        assert (await fetch(factory, theirs.job_id)).status == JobStatus.QUEUED
        assert await queue.claim("w1", task_names=set()) == []

    async def test_limit_is_respected(self, queue: JobQueue, workspace_id: uuid.UUID) -> None:
        for _ in range(5):
            await queue.enqueue("t", workspace_id=workspace_id)
        assert len(await queue.claim("w1", limit=2)) == 2
        assert len(await queue.claim("w1", limit=10)) == 3

    async def test_concurrent_claimers_never_receive_the_same_job(
        self, queue: JobQueue, workspace_id: uuid.UUID
    ) -> None:
        total = 60
        ids = {
            (await queue.enqueue("t", {"i": i}, workspace_id=workspace_id)).job_id
            for i in range(total)
        }
        got: list[uuid.UUID] = []

        async def claimer(name: str) -> None:
            while batch := await queue.claim(name, limit=4):
                got.extend(j.id for j in batch)
                await asyncio.sleep(0)

        await asyncio.gather(*(claimer(f"w{n}") for n in range(10)))
        assert len(got) == total, "every job claimed"
        assert len(set(got)) == total, "no job claimed twice"
        assert set(got) == ids


# ───────────────────────────────── fencing ─────────────────────────────────


class TestFencing:
    async def test_complete_marks_succeeded_and_stores_the_result(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID, clock: FakeClock
    ) -> None:
        await queue.enqueue("t", workspace_id=workspace_id)
        (job,) = await queue.claim("w1")
        assert await queue.complete(job, {"found": 3})
        row = await fetch(factory, job.id)
        assert row.status == JobStatus.SUCCEEDED
        assert row.result == {"found": 3}
        assert row.finished_at == clock.now()
        assert row.locked_by is None
        assert row.locked_until is None

    async def test_another_worker_cannot_complete_my_job(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
    ) -> None:
        from dataclasses import replace

        await queue.enqueue("t", workspace_id=workspace_id)
        (job,) = await queue.claim("w1")
        impostor = replace(job, locked_by="w2")
        assert not await queue.complete(impostor)
        assert (await fetch(factory, job.id)).status == JobStatus.RUNNING

    async def test_a_zombie_cannot_overwrite_the_new_owners_outcome(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID, clock: FakeClock
    ) -> None:
        """Worker A stalls past its lease; the job is recovered and re-claimed (by the same worker id, the worst
        case); A then wakes up and tries to report. Only the current attempt may write."""
        await queue.enqueue("t", workspace_id=workspace_id)
        (stale,) = await queue.claim("w1", lease_seconds=30)
        clock.advance(seconds=31)
        await queue.requeue_expired()
        clock.advance(seconds=11)  # past the 10 s backoff
        (fresh,) = await queue.claim("w1", lease_seconds=30)
        assert (stale.attempt, fresh.attempt) == (1, 2)

        assert not await queue.complete(stale, {"from": "zombie"})
        assert await queue.fail(stale, "zombie error") == FailOutcome.LOST
        assert await queue.heartbeat(stale) is None
        assert (await fetch(factory, fresh.id)).status == JobStatus.RUNNING

        assert await queue.complete(fresh, {"from": "owner"})
        assert (await fetch(factory, fresh.id)).result == {"from": "owner"}

    async def test_heartbeat_extends_the_lease_and_reports_cancel_requests(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID, clock: FakeClock
    ) -> None:
        await queue.enqueue("t", workspace_id=workspace_id)
        (job,) = await queue.claim("w1", lease_seconds=30)
        clock.advance(seconds=20)
        beat = await queue.heartbeat(job, lease_seconds=30)
        assert beat is not None
        assert not beat.cancel_requested
        assert (await fetch(factory, job.id)).locked_until == clock.now() + timedelta(seconds=30)

        await queue.cancel(job.id, workspace_id=workspace_id)
        beat = await queue.heartbeat(job, lease_seconds=30)
        assert beat is not None
        assert beat.cancel_requested


# ───────────────────────────────── failures ─────────────────────────────────


class TestFailure:
    async def test_retries_with_growing_backoff(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID, clock: FakeClock
    ) -> None:
        await queue.enqueue("t", workspace_id=workspace_id)
        (job,) = await queue.claim("w1")
        assert await queue.fail(job, RuntimeError("boom")) == FailOutcome.RETRY_SCHEDULED
        row = await fetch(factory, job.id)
        assert row.status == JobStatus.QUEUED
        assert row.run_at == clock.now() + timedelta(seconds=10)
        assert (row.last_error_class, row.last_error) == ("RuntimeError", "boom")
        assert await queue.claim("w1") == [], "not before the backoff elapsed"

        clock.advance(seconds=10)
        (job,) = await queue.claim("w1")
        assert job.attempt == 2
        await queue.fail(job, RuntimeError("boom again"))
        assert (await fetch(factory, job.id)).run_at == clock.now() + timedelta(seconds=20)

    async def test_retry_after_is_honoured(
        self, factory: Factory, workspace_id: uuid.UUID, clock: FakeClock
    ) -> None:
        q = JobQueue(factory, clock, policy=POLICY)
        await q.enqueue("t", workspace_id=workspace_id)
        (job,) = await q.claim("w1")
        await q.fail(job, RuntimeError("429"), retry_after=300)
        waited = (await fetch(factory, job.id)).run_at - clock.now()
        assert timedelta(seconds=300) <= waited <= timedelta(seconds=331)

    async def test_permanent_errors_go_straight_to_the_dead_letters(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
    ) -> None:
        await queue.enqueue("send", {"lead": 7}, workspace_id=workspace_id)
        (job,) = await queue.claim("w1")
        assert await queue.fail(job, ValueError("bad input"), permanent=True) == FailOutcome.DEAD
        row = await fetch(factory, job.id)
        assert row.status == JobStatus.DEAD
        assert row.finished_at is not None
        (letter,) = await dead_letters(factory)
        assert (letter.task_name, letter.payload, letter.error_class) == (
            "send",
            {"lead": 7},
            "ValueError",
        )
        assert (letter.status, letter.attempts, letter.job_id) == (
            DeadLetterStatus.OPEN,
            1,
            str(job.id),
        )

    async def test_running_out_of_attempts_dead_letters_the_job(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID, clock: FakeClock
    ) -> None:
        await queue.enqueue("t", workspace_id=workspace_id, max_attempts=2)
        (job,) = await queue.claim("w1")
        assert await queue.fail(job, "first") == FailOutcome.RETRY_SCHEDULED
        clock.advance(seconds=10)
        (job,) = await queue.claim("w1")
        assert job.is_last_attempt
        assert await queue.fail(job, "second") == FailOutcome.DEAD
        assert (await fetch(factory, job.id)).status == JobStatus.DEAD
        assert len(await dead_letters(factory)) == 1

    async def test_the_same_task_and_payload_failing_again_updates_one_dead_letter(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
    ) -> None:
        for _ in range(2):
            await queue.enqueue("send", {"b": 2, "a": 1}, workspace_id=workspace_id)
            (job,) = await queue.claim("w1")
            await queue.fail(job, ValueError("nope"), permanent=True)
        (letter,) = await dead_letters(factory)
        assert letter.attempts == 2

    async def test_a_resolved_dead_letter_reopens_when_the_task_fails_again(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
    ) -> None:
        await queue.enqueue("send", {"a": 1}, workspace_id=workspace_id)
        (job,) = await queue.claim("w1")
        await queue.fail(job, "x", permanent=True)
        (letter,) = await dead_letters(factory)
        assert await queue.discard_dead_letter(letter.id, workspace_id=workspace_id)

        await queue.enqueue("send", {"a": 1}, workspace_id=workspace_id)
        (job,) = await queue.claim("w1")
        await queue.fail(job, "x again", permanent=True)
        (reopened,) = await dead_letters(factory)
        assert reopened.status == DeadLetterStatus.OPEN
        assert reopened.resolved_at is None

    async def test_persisted_errors_never_contain_credentials(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
    ) -> None:
        await queue.enqueue("t", workspace_id=workspace_id)
        (job,) = await queue.claim("w1")
        secret = "gsk_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
        await queue.fail(
            job, RuntimeError(f"401 from api: Authorization: Bearer {secret} (api_key={secret})")
        )
        error = (await fetch(factory, job.id)).last_error or ""
        assert secret not in error
        assert "401 from api" in error


# ───────────────────────────────── park / release ─────────────────────────────────


class TestParkAndRelease:
    async def test_parking_does_not_consume_an_attempt(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID, clock: FakeClock
    ) -> None:
        await queue.enqueue("t", workspace_id=workspace_id, max_attempts=1)
        (job,) = await queue.claim("w1")
        resume = clock.now() + timedelta(hours=6)
        assert await queue.park(job, "Groq daily quota exhausted", until=resume)
        row = await fetch(factory, job.id)
        assert (row.status, row.attempts, row.run_at) == (JobStatus.QUEUED, 0, resume)
        assert row.parked_reason == "Groq daily quota exhausted"

        assert await queue.claim("w1") == []
        clock.advance(hours=6)
        (again,) = await queue.claim("w1")
        # Still on attempt 1 of 1: parking a thousand times can never dead-letter a healthy task.
        assert again.attempt == 1
        assert (await fetch(factory, job.id)).parked_reason is None

    async def test_release_makes_the_job_immediately_claimable_without_using_an_attempt(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
    ) -> None:
        await queue.enqueue("t", workspace_id=workspace_id)
        (job,) = await queue.claim("w1")
        assert await queue.release(job)
        assert (await fetch(factory, job.id)).attempts == 0
        (again,) = await queue.claim("w2")
        assert again.attempt == 1
        assert again.locked_by == "w2"


# ───────────────────────────────── cancel ─────────────────────────────────


class TestCancel:
    async def test_a_queued_job_is_cancelled_immediately_and_never_runs(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
    ) -> None:
        res = await queue.enqueue("t", workspace_id=workspace_id)
        assert await queue.cancel(res.job_id, workspace_id=workspace_id) == CancelOutcome.CANCELLED
        assert (await fetch(factory, res.job_id)).status == JobStatus.CANCELLED
        assert await queue.claim("w1") == []

    async def test_a_running_job_is_asked_to_stop_and_is_not_retried(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
    ) -> None:
        await queue.enqueue("t", workspace_id=workspace_id)
        (job,) = await queue.claim("w1")
        assert await queue.cancel(job.id, workspace_id=workspace_id) == CancelOutcome.REQUESTED
        assert (await fetch(factory, job.id)).status == JobStatus.RUNNING
        assert await queue.fail(job, "interrupted") == FailOutcome.CANCELLED
        assert (await fetch(factory, job.id)).status == JobStatus.CANCELLED

    async def test_cancel_is_scoped_to_the_workspace_and_to_active_jobs(
        self, queue: JobQueue, workspace_id: uuid.UUID, other_workspace_id: uuid.UUID
    ) -> None:
        res = await queue.enqueue("t", workspace_id=workspace_id)
        assert (
            await queue.cancel(res.job_id, workspace_id=other_workspace_id)
            == CancelOutcome.NOT_ACTIVE
        )
        assert (
            await queue.cancel(uuid.uuid4(), workspace_id=workspace_id) == CancelOutcome.NOT_ACTIVE
        )
        (job,) = await queue.claim("w1")
        await queue.complete(job)
        assert await queue.cancel(res.job_id, workspace_id=workspace_id) == CancelOutcome.NOT_ACTIVE


# ───────────────────────────────── crash recovery ─────────────────────────────────


class TestCrashRecovery:
    async def test_an_expired_lease_is_requeued_with_backoff(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID, clock: FakeClock
    ) -> None:
        await queue.enqueue("t", workspace_id=workspace_id)
        (job,) = await queue.claim("crashed-worker", lease_seconds=30)
        clock.advance(seconds=31)  # the worker died; nobody heartbeat
        result = await queue.requeue_expired()
        assert (result.requeued, result.dead, result.cancelled) == (1, 0, 0)
        row = await fetch(factory, job.id)
        assert row.status == JobStatus.QUEUED
        assert row.attempts == 1, "a crash consumes the attempt (poison-pill protection)"
        assert row.last_error_class == "LeaseExpired"
        assert row.locked_by is None
        assert await queue.claim("w2") == [], "backoff applies"
        clock.advance(seconds=10)
        (again,) = await queue.claim("w2")
        assert again.attempt == 2

    async def test_live_leases_are_left_alone(
        self, queue: JobQueue, workspace_id: uuid.UUID, clock: FakeClock
    ) -> None:
        await queue.enqueue("t", workspace_id=workspace_id)
        await queue.claim("w1", lease_seconds=30)
        clock.advance(seconds=29)
        assert (await queue.requeue_expired()).total == 0

    async def test_a_task_that_keeps_killing_its_worker_is_dead_lettered(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID, clock: FakeClock
    ) -> None:
        await queue.enqueue("t", workspace_id=workspace_id, max_attempts=2)
        for _ in range(2):
            await queue.claim("w1", lease_seconds=30)
            clock.advance(seconds=31)
            result = await queue.requeue_expired()
            clock.advance(seconds=30)
        assert result.dead == 1
        (letter,) = await dead_letters(factory)
        assert letter.error_class == "LeaseExpired"
        assert await queue.claim("w1") == []

    async def test_a_cancelled_job_is_not_resurrected_by_the_reaper(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID, clock: FakeClock
    ) -> None:
        await queue.enqueue("t", workspace_id=workspace_id)
        (job,) = await queue.claim("w1", lease_seconds=30)
        await queue.cancel(job.id, workspace_id=workspace_id)
        clock.advance(seconds=31)
        assert (await queue.requeue_expired()).cancelled == 1
        assert (await fetch(factory, job.id)).status == JobStatus.CANCELLED

    async def test_the_reaper_never_touches_queued_or_finished_jobs(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID, clock: FakeClock
    ) -> None:
        a = await queue.enqueue("t", workspace_id=workspace_id)
        b = await queue.enqueue("t", workspace_id=workspace_id)
        (done,) = await queue.claim("w1", limit=1, lease_seconds=5)
        await queue.complete(done)
        clock.advance(hours=1)
        assert (await queue.requeue_expired()).total == 0
        statuses = {(await fetch(factory, j)).status for j in (a.job_id, b.job_id)}
        assert statuses == {JobStatus.SUCCEEDED, JobStatus.QUEUED}


# ───────────────────────────────── operations ─────────────────────────────────


class TestOperations:
    async def test_stats_describe_the_queue(
        self,
        queue: JobQueue,
        workspace_id: uuid.UUID,
        other_workspace_id: uuid.UUID,
        clock: FakeClock,
    ) -> None:
        empty = await queue.stats()
        assert (empty.queued, empty.running, empty.oldest_due_age_seconds) == (0, 0, None)

        await queue.enqueue("due", workspace_id=workspace_id)
        await queue.enqueue(
            "later", workspace_id=workspace_id, run_at=clock.now() + timedelta(hours=1)
        )
        await queue.enqueue("other", workspace_id=other_workspace_id)
        clock.advance(seconds=90)
        (running,) = await queue.claim("w1", limit=1, task_names={"due"}, lease_seconds=10)
        clock.advance(seconds=60)

        stats = await queue.stats()
        assert (stats.queued, stats.due_now, stats.scheduled) == (2, 1, 1)
        assert (stats.running, stats.expired_leases) == (1, 1)
        assert stats.oldest_due_age_seconds == 150
        scoped = await queue.stats(workspace_id=workspace_id)
        assert (scoped.queued, scoped.running) == (1, 1)
        await queue.complete(running)  # fenced on the (id, worker, attempt) it was claimed with
        assert (await queue.stats()).succeeded == 1

    async def test_purge_applies_retention_and_keeps_active_jobs(
        self, queue: JobQueue, factory: Factory, workspace_id: uuid.UUID, clock: FakeClock
    ) -> None:
        old_ok = await queue.enqueue("t", workspace_id=workspace_id)
        (job,) = await queue.claim("w1")
        await queue.complete(job)
        dead = await queue.enqueue("t", {"d": 1}, workspace_id=workspace_id)
        (djob,) = await queue.claim("w1")
        await queue.fail(djob, "x", permanent=True)
        waiting = await queue.enqueue("t", workspace_id=workspace_id)

        clock.advance(days=8)
        assert await queue.purge_finished(succeeded_older_than_days=7, dead_older_than_days=30) == 1
        assert await count_jobs(factory) == 2  # dead (30 d retention) + queued survive
        clock.advance(days=30)
        assert await queue.purge_finished() == 1
        remaining = (await fetch(factory, waiting.job_id)).status
        assert remaining == JobStatus.QUEUED
        assert old_ok.job_id != dead.job_id
        assert len(await dead_letters(factory)) == 1, "dead-letter records outlive their job rows"

    async def test_retrying_a_dead_letter_enqueues_a_fresh_job_once(
        self,
        queue: JobQueue,
        factory: Factory,
        workspace_id: uuid.UUID,
        other_workspace_id: uuid.UUID,
    ) -> None:
        await queue.enqueue("send", {"lead": 9}, workspace_id=workspace_id)
        (job,) = await queue.claim("w1")
        await queue.fail(job, "bad", permanent=True)
        (letter,) = await dead_letters(factory)

        assert await queue.retry_dead_letter(letter.id, workspace_id=other_workspace_id) is None
        new_id = await queue.retry_dead_letter(letter.id, workspace_id=workspace_id)
        assert new_id is not None
        fresh = await fetch(factory, new_id)
        assert (fresh.task_name, fresh.payload, fresh.status, fresh.attempts) == (
            "send",
            {"lead": 9},
            JobStatus.QUEUED,
            0,
        )
        (resolved,) = await dead_letters(factory)
        assert resolved.status == DeadLetterStatus.RETRIED
        assert resolved.resolved_at is not None
        assert await queue.retry_dead_letter(letter.id, workspace_id=workspace_id) is None

    async def test_discarding_a_dead_letter_is_scoped_and_one_shot(
        self,
        queue: JobQueue,
        factory: Factory,
        workspace_id: uuid.UUID,
        other_workspace_id: uuid.UUID,
    ) -> None:
        await queue.enqueue("send", workspace_id=workspace_id)
        (job,) = await queue.claim("w1")
        await queue.fail(job, "bad", permanent=True)
        (letter,) = await dead_letters(factory)
        assert not await queue.discard_dead_letter(letter.id, workspace_id=other_workspace_id)
        assert await queue.discard_dead_letter(letter.id, workspace_id=workspace_id)
        assert not await queue.discard_dead_letter(letter.id, workspace_id=workspace_id)


async def test_the_database_rejects_corrupt_attempt_counts(
    factory: Factory, queue: JobQueue, workspace_id: uuid.UUID
) -> None:
    from sqlalchemy.exc import IntegrityError

    res = await queue.enqueue("t", workspace_id=workspace_id)
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as s:
            await s.execute(update(Job).where(Job.id == res.job_id).values(attempts=-1))
