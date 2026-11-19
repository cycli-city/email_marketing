"""The in-process worker, end to end on a real PostgreSQL (real clock, tiny intervals)."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import SYSTEM_CLOCK
from app.core.retry import BackoffPolicy
from app.db.models import DeadLetter, Job
from app.db.session import session_scope
from app.domain.enums import JobStatus
from app.queue import (
    CancelOutcome,
    JobContext,
    JobQueue,
    ParkJob,
    PeriodicSchedule,
    PermanentJobError,
    RetryableJobError,
    TaskRegistry,
    Worker,
    WorkerConfig,
)

Factory = async_sessionmaker[AsyncSession]

FAST = WorkerConfig(
    concurrency=4,
    lease_seconds=3.0,
    heartbeat_seconds=0.15,
    poll_min_seconds=0.02,
    poll_max_seconds=0.1,
    reap_interval_seconds=0.15,
    schedule_tick_seconds=0.03,
    default_timeout_seconds=5.0,
    shutdown_grace_seconds=0.3,
)
FAST_RETRY = BackoffPolicy(base_seconds=0.05, factor=1.5, max_seconds=0.2, jitter="none")


@pytest.fixture
def queue(factory: Factory) -> JobQueue:
    return JobQueue(factory, SYSTEM_CLOCK, policy=FAST_RETRY)


async def fetch(factory: Factory, job_id: uuid.UUID) -> Job:
    async with factory() as s:
        return (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()


async def eventually(
    check: Callable[[], Awaitable[bool]] | Callable[[], bool],
    *,
    within: float = 10.0,
    what: str = "condition",
) -> None:
    deadline = time.monotonic() + within
    while True:
        outcome = check()
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        if outcome:
            return
        if time.monotonic() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


@asynccontextmanager
async def running(worker: Worker) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.create_task(worker.run())
    try:
        yield task
    finally:
        worker.stop()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)


def make_worker(queue: JobQueue, registry: TaskRegistry, **kwargs: Any) -> Worker:
    kwargs.setdefault("config", FAST)
    return Worker(queue, registry, clock=SYSTEM_CLOCK, **kwargs)


def status_is(
    factory: Factory, job_id: uuid.UUID, status: JobStatus
) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return (await fetch(factory, job_id)).status == status

    return check


# ───────────────────────────── happy path & retries ─────────────────────────────


async def test_processes_enqueued_jobs_and_stores_results(
    queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
) -> None:
    registry = TaskRegistry()
    seen: list[int] = []

    @registry.task("double")
    async def double(ctx: JobContext) -> dict[str, int]:
        n = int(ctx.job.payload["n"])
        seen.append(n)
        return {"result": n * 2}

    ids = [
        (await queue.enqueue("double", {"n": n}, workspace_id=workspace_id)).job_id
        for n in range(5)
    ]
    async with running(make_worker(queue, registry)):
        for job_id in ids:
            await eventually(status_is(factory, job_id, JobStatus.SUCCEEDED), what="job to succeed")
    assert sorted(seen) == [0, 1, 2, 3, 4], "each job ran exactly once"
    rows = [await fetch(factory, i) for i in ids]
    assert sorted((row.result or {})["result"] for row in rows) == [0, 2, 4, 6, 8]


async def test_a_transient_failure_is_retried_until_it_succeeds(
    queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
) -> None:
    registry = TaskRegistry()
    calls = 0

    @registry.task("flaky")
    async def flaky(ctx: JobContext) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("temporary glitch")

    job_id = (await queue.enqueue("flaky", workspace_id=workspace_id)).job_id
    async with running(make_worker(queue, registry)):
        await eventually(status_is(factory, job_id, JobStatus.SUCCEEDED), what="retries to finish")
    row = await fetch(factory, job_id)
    assert (calls, row.attempts) == (3, 3)
    assert row.last_error is None, "a success clears the previous error"


async def test_a_permanent_error_is_dead_lettered_without_retrying(
    queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
) -> None:
    registry = TaskRegistry()
    calls = 0

    @registry.task("bad")
    async def bad(ctx: JobContext) -> None:
        nonlocal calls
        calls += 1
        raise PermanentJobError("recipient is on the suppression list")

    job_id = (await queue.enqueue("bad", {"lead": 1}, workspace_id=workspace_id)).job_id
    async with running(make_worker(queue, registry)):
        await eventually(status_is(factory, job_id, JobStatus.DEAD), what="dead letter")
    assert calls == 1
    async with factory() as s:
        (letter,) = (await s.execute(select(DeadLetter))).scalars().all()
    assert (letter.error_class, letter.error_message) == (
        "PermanentJobError",
        "recipient is on the suppression list",
    )


async def test_retries_stop_at_max_attempts(
    queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
) -> None:
    registry = TaskRegistry()
    calls = 0

    @registry.task("always-fails")
    async def always_fails(ctx: JobContext) -> None:
        nonlocal calls
        calls += 1
        raise ConnectionError("provider down")

    job_id = (await queue.enqueue("always-fails", workspace_id=workspace_id, max_attempts=3)).job_id
    async with running(make_worker(queue, registry)):
        await eventually(status_is(factory, job_id, JobStatus.DEAD), what="exhausted retries")
    assert calls == 3


async def test_retry_after_from_the_handler_delays_the_next_attempt(
    queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
) -> None:
    registry = TaskRegistry()
    stamps: list[float] = []

    @registry.task("rate-limited")
    async def rate_limited(ctx: JobContext) -> None:
        stamps.append(time.monotonic())
        if len(stamps) == 1:
            raise RetryableJobError("429 too many requests", retry_after=0.4)

    job_id = (await queue.enqueue("rate-limited", workspace_id=workspace_id)).job_id
    async with running(make_worker(queue, registry)):
        await eventually(status_is(factory, job_id, JobStatus.SUCCEEDED), what="retry after delay")
    assert stamps[1] - stamps[0] >= 0.4


async def test_parking_defers_the_job_without_consuming_an_attempt(
    queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
) -> None:
    registry = TaskRegistry()
    stamps: list[float] = []

    @registry.task("needs-provider")
    async def needs_provider(ctx: JobContext) -> None:
        stamps.append(time.monotonic())
        if len(stamps) <= 2:
            raise ParkJob("all LLM providers are rate limited", delay_seconds=0.25)

    job_id = (
        await queue.enqueue("needs-provider", workspace_id=workspace_id, max_attempts=1)
    ).job_id
    async with running(make_worker(queue, registry)):
        await eventually(
            status_is(factory, job_id, JobStatus.SUCCEEDED), what="parked job to resume"
        )
    row = await fetch(factory, job_id)
    assert row.attempts == 1, "parked twice and still on its only attempt"
    assert len(stamps) == 3
    assert stamps[1] - stamps[0] >= 0.25
    assert stamps[2] - stamps[1] >= 0.25


async def test_a_handler_timeout_is_a_retryable_failure(
    queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
) -> None:
    registry = TaskRegistry()

    @registry.task("hangs", timeout_seconds=0.15)
    async def hangs(ctx: JobContext) -> None:
        await asyncio.sleep(30)

    job_id = (await queue.enqueue("hangs", workspace_id=workspace_id, max_attempts=1)).job_id
    async with running(make_worker(queue, registry)):
        await eventually(status_is(factory, job_id, JobStatus.DEAD), what="timeout to dead-letter")
    assert (await fetch(factory, job_id)).last_error_class == "TimeoutError"


# ───────────────────────────── concurrency & scheduling ─────────────────────────────


async def test_concurrency_is_capped(
    queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
) -> None:
    registry = TaskRegistry()
    current = peak = 0

    @registry.task("slow")
    async def slow(ctx: JobContext) -> None:
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        await asyncio.sleep(0.12)
        current -= 1

    ids = [(await queue.enqueue("slow", workspace_id=workspace_id)).job_id for _ in range(9)]
    config = replace(FAST, concurrency=3)
    async with running(make_worker(queue, registry, config=config)):
        for job_id in ids:
            await eventually(status_is(factory, job_id, JobStatus.SUCCEEDED), what="all slow jobs")
    assert peak == 3


async def test_tasks_without_a_registered_handler_are_left_in_the_queue(
    queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
) -> None:
    registry = TaskRegistry()

    @registry.task("known")
    async def known(ctx: JobContext) -> None:
        return None

    foreign = (await queue.enqueue("someone-elses", workspace_id=workspace_id)).job_id
    mine = (await queue.enqueue("known", workspace_id=workspace_id)).job_id
    async with running(make_worker(queue, registry)):
        await eventually(status_is(factory, mine, JobStatus.SUCCEEDED), what="registered task")
        await asyncio.sleep(0.2)
    row = await fetch(factory, foreign)
    assert (row.status, row.attempts) == (JobStatus.QUEUED, 0)


async def test_wake_makes_an_idle_worker_pick_up_new_work_immediately(
    queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
) -> None:
    registry = TaskRegistry()

    @registry.task("ping")
    async def ping(ctx: JobContext) -> None:
        return None

    sleepy = replace(FAST, poll_min_seconds=30.0, poll_max_seconds=30.0)
    worker = make_worker(queue, registry, config=sleepy)
    async with running(worker):
        await asyncio.sleep(0.2)  # worker is now napping for 30 s
        job_id = (await queue.enqueue("ping", workspace_id=workspace_id)).job_id
        worker.wake()
        await eventually(
            status_is(factory, job_id, JobStatus.SUCCEEDED), within=3, what="woken worker"
        )


async def test_periodic_schedules_run_repeatedly_but_never_stack(
    queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
) -> None:
    registry = TaskRegistry()
    runs = 0
    current = peak = 0

    @registry.task("tick")
    async def tick(ctx: JobContext) -> None:
        nonlocal runs, current, peak
        runs += 1
        current += 1
        peak = max(peak, current)
        await asyncio.sleep(0.25)  # much slower than the schedule interval
        current -= 1

    schedule = PeriodicSchedule("tick", every_seconds=0.05, workspace_id=workspace_id)
    async with running(make_worker(queue, registry, schedules=[schedule])):
        await eventually(lambda: runs >= 3, within=10, what="three periodic runs")
    assert peak == 1, "a slow run must not let copies pile up"


# ───────────────────────────── cancellation, shutdown, leases, resilience ─────────────────────────────


async def test_cooperative_cancel_stops_a_running_job(
    queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
) -> None:
    registry = TaskRegistry()
    started = asyncio.Event()

    @registry.task("long")
    async def long_job(ctx: JobContext) -> None:
        started.set()
        while True:
            ctx.raise_if_cancelled()
            await asyncio.sleep(0.02)

    job_id = (await queue.enqueue("long", workspace_id=workspace_id)).job_id
    async with running(make_worker(queue, registry)):
        await asyncio.wait_for(started.wait(), timeout=5)
        outcome = await queue.cancel(job_id, workspace_id=workspace_id)
        assert outcome == CancelOutcome.REQUESTED
        await eventually(status_is(factory, job_id, JobStatus.CANCELLED), what="cancellation")
    row = await fetch(factory, job_id)
    assert (row.attempts, row.last_error) == (1, None), (
        "stopped cooperatively: not retried, not an error"
    )


async def test_graceful_shutdown_hands_in_flight_jobs_back_unconsumed(
    queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
) -> None:
    registry = TaskRegistry()
    started = asyncio.Event()
    interrupted = asyncio.Event()

    @registry.task("stuck")
    async def stuck(ctx: JobContext) -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            interrupted.set()
            raise

    job_id = (await queue.enqueue("stuck", workspace_id=workspace_id)).job_id
    worker = make_worker(queue, registry)
    async with running(worker) as task:
        await asyncio.wait_for(started.wait(), timeout=5)
        t0 = time.monotonic()
        worker.stop()
        await asyncio.wait_for(task, timeout=5)
        assert time.monotonic() - t0 < 3, "shutdown must be bounded by the grace period"
    assert interrupted.is_set()
    row = await fetch(factory, job_id)
    assert (row.status, row.attempts, row.locked_by) == (JobStatus.QUEUED, 0, None)

    # …and the next process starts it straight away.
    (again,) = await queue.claim("next-process")
    assert again.id == job_id
    assert again.attempt == 1


async def test_a_worker_that_lost_its_lease_stops_the_handler_and_writes_nothing(
    queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
) -> None:
    registry = TaskRegistry()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    @registry.task("zombie")
    async def zombie(ctx: JobContext) -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    job_id = (await queue.enqueue("zombie", workspace_id=workspace_id)).job_id
    async with running(make_worker(queue, registry)):
        await asyncio.wait_for(started.wait(), timeout=5)
        # Someone else takes ownership (as if our lease had expired and the job was re-claimed).
        async with session_scope(factory) as s:
            await s.execute(update(Job).where(Job.id == job_id).values(locked_by="the-new-owner"))
        await asyncio.wait_for(cancelled.wait(), timeout=5)
        await asyncio.sleep(0.2)
    row = await fetch(factory, job_id)
    assert (row.status, row.locked_by) == (JobStatus.RUNNING, "the-new-owner"), (
        "the zombie must not complete, fail or release a job it no longer owns"
    )


async def test_orphaned_jobs_from_a_crashed_worker_are_recovered_and_finished(
    queue: JobQueue, factory: Factory, workspace_id: uuid.UUID
) -> None:
    registry = TaskRegistry()
    ran = asyncio.Event()

    @registry.task("important")
    async def important(ctx: JobContext) -> dict[str, int]:
        ran.set()
        return {"attempt": ctx.job.attempt}

    job_id = (await queue.enqueue("important", workspace_id=workspace_id)).job_id
    # A previous process claimed it with a short lease and died without a trace.
    (orphan,) = await queue.claim("dead-process", lease_seconds=0.3)
    assert orphan.id == job_id

    async with running(make_worker(queue, registry)):
        await eventually(status_is(factory, job_id, JobStatus.SUCCEEDED), what="recovery")
    row = await fetch(factory, job_id)
    assert row.result == {"attempt": 2}


async def test_the_claim_loop_survives_database_errors(
    queue: JobQueue, factory: Factory, workspace_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = TaskRegistry()

    @registry.task("ok")
    async def ok(ctx: JobContext) -> None:
        return None

    real_claim = queue.claim
    failures = 0

    async def flaky_claim(*args: Any, **kwargs: Any) -> Any:
        nonlocal failures
        if failures < 2:
            failures += 1
            raise ConnectionError("database is restarting")
        return await real_claim(*args, **kwargs)

    monkeypatch.setattr(queue, "claim", flaky_claim)
    job_id = (await queue.enqueue("ok", workspace_id=workspace_id)).job_id
    async with running(make_worker(queue, registry)):
        await eventually(
            status_is(factory, job_id, JobStatus.SUCCEEDED),
            within=15,
            what="recovery from DB errors",
        )
    assert failures == 2


def test_worker_config_rejects_unsafe_values() -> None:
    with pytest.raises(ValueError, match="concurrency"):
        WorkerConfig(concurrency=0)
    with pytest.raises(ValueError, match="heartbeat"):
        WorkerConfig(lease_seconds=10, heartbeat_seconds=10)


def test_registry_rejects_duplicate_task_names() -> None:
    registry = TaskRegistry()

    async def handler(ctx: JobContext) -> None:
        return None

    registry.register("a", handler)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("a", handler)
