"""In-process job worker (ADR-024: workers are asyncio tasks inside the API process, ``APP_ROLE=all``).

What it does, and why it is safe to leave running unattended:

* **Claims** due jobs for the registered task names only, up to ``concurrency`` at a time, polling with an idle
  backoff (and an in-process wake-up so a freshly enqueued job doesn't wait for the next poll).
* **Heartbeats** every running job. If a heartbeat finds the lease gone (we stalled; someone else owns the job),
  the handler is cancelled on the spot — a zombie must not keep performing side effects.
* **Classifies outcomes**: success → ``complete``; :class:`ParkJob` → back to the queue *without* using an attempt;
  :class:`PermanentJobError` → dead letter; :class:`RetryableJobError` / any other exception / timeout → retry with
  jittered exponential backoff (``Retry-After`` honoured) until ``max_attempts``.
* **Recovers** jobs orphaned by a crash (reaper) and enqueues **periodic** jobs (one active instance per task).
* **Shuts down gracefully**: stop claiming, give in-flight jobs a grace period, then cancel the rest and hand
  their jobs back (attempt not consumed) so the next process starts them immediately.

Handlers must be idempotent: delivery is at-least-once (a crash after a side effect and before ``complete`` runs the
job again). Cancellation (:attr:`JobContext.cancel_requested`) is cooperative.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from typing import Any

import structlog

from app.core.clock import Clock
from app.core.retry import BackoffPolicy, backoff_seconds
from app.queue.errors import JobCancelled, ParkJob, PermanentJobError, RetryableJobError
from app.queue.service import JobQueue
from app.queue.types import ClaimedJob

log = structlog.get_logger("queue.worker")

_LOOP_ERROR_BACKOFF = BackoffPolicy(base_seconds=1.0, max_seconds=30.0, jitter="equal")


class JobContext:
    """What a handler receives: the claimed job plus cooperative-cancel and lease state."""

    def __init__(self, job: ClaimedJob) -> None:
        self.job = job
        self._cancel_requested = False
        self.lease_lost = False

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    def note_cancel_requested(self, requested: bool) -> None:
        """Worker-internal: updated from each heartbeat response."""
        self._cancel_requested = requested

    def raise_if_cancelled(self) -> None:
        """Call between units of work in long handlers; raises :class:`JobCancelled` if the operator cancelled."""
        if self._cancel_requested:
            raise JobCancelled


Handler = Callable[[JobContext], Awaitable[Mapping[str, Any] | None]]


@dataclass(frozen=True, slots=True)
class TaskSpec:
    name: str
    handler: Handler
    timeout_seconds: float | None = None


class TaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskSpec] = {}

    def register(
        self, name: str, handler: Handler, *, timeout_seconds: float | None = None
    ) -> None:
        if name in self._tasks:
            raise ValueError(f"task {name!r} is already registered")
        self._tasks[name] = TaskSpec(name, handler, timeout_seconds)

    def task(
        self, name: str, *, timeout_seconds: float | None = None
    ) -> Callable[[Handler], Handler]:
        def decorator(handler: Handler) -> Handler:
            self.register(name, handler, timeout_seconds=timeout_seconds)
            return handler

        return decorator

    def get(self, name: str) -> TaskSpec | None:
        return self._tasks.get(name)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._tasks)


@dataclass(frozen=True, slots=True)
class PeriodicSchedule:
    """Run ``task_name`` every ``every_seconds``. At most one instance is ever queued/running (dedupe key), so a slow
    run never stacks up copies. Periodic tasks must be idempotent (a restart enqueues one immediately)."""

    task_name: str
    every_seconds: float
    workspace_id: uuid.UUID
    payload: Mapping[str, Any] = field(default_factory=dict)
    run_immediately: bool = True
    priority: int = 100


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    concurrency: int = 4
    lease_seconds: float = 60.0
    heartbeat_seconds: float | None = None  # default: lease / 3
    poll_min_seconds: float = 0.5
    poll_max_seconds: float = 5.0
    reap_interval_seconds: float = 30.0
    schedule_tick_seconds: float = 1.0
    default_timeout_seconds: float = 600.0
    shutdown_grace_seconds: float = 25.0

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if self.heartbeat_interval >= self.lease_seconds:
            raise ValueError("heartbeat interval must be shorter than the lease")

    @property
    def heartbeat_interval(self) -> float:
        return self.heartbeat_seconds or self.lease_seconds / 3.0


def _default_worker_id() -> str:
    return f"w-{socket.gethostname()[:30]}-{os.getpid()}-{secrets.token_hex(3)}"


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        registry: TaskRegistry,
        *,
        clock: Clock,
        config: WorkerConfig | None = None,
        worker_id: str | None = None,
        schedules: Sequence[PeriodicSchedule] = (),
    ) -> None:
        self._queue = queue
        self._registry = registry
        self._clock = clock
        self._config = config or WorkerConfig()
        self.worker_id = worker_id or _default_worker_id()
        self._schedules = tuple(schedules)
        self._inflight: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        self._started = False

    # ───────────────────────────── control ─────────────────────────────

    @property
    def in_flight(self) -> int:
        return len(self._inflight)

    def wake(self) -> None:
        """Tell the claim loop to look for work now (call after enqueueing in the same process)."""
        self._wake.set()

    def stop(self) -> None:
        """Begin graceful shutdown. Idempotent; ``run()`` returns once in-flight jobs are drained/released."""
        self._stopping.set()
        self._wake.set()

    async def run(self) -> None:
        if self._started:
            raise RuntimeError("a Worker can only be run once")
        self._started = True
        log.info(
            "queue.worker_started",
            worker_id=self.worker_id,
            concurrency=self._config.concurrency,
            tasks=sorted(self._registry.names),
        )
        helpers = [
            asyncio.create_task(self._reap_loop(), name="queue-reaper"),
            asyncio.create_task(self._schedule_loop(), name="queue-scheduler"),
        ]
        try:
            await self._claim_loop()
        finally:
            await self._drain()
            for helper in helpers:
                helper.cancel()
            await asyncio.gather(*helpers, return_exceptions=True)
            log.info("queue.worker_stopped", worker_id=self.worker_id)

    # ───────────────────────────── loops ─────────────────────────────

    async def _claim_loop(self) -> None:
        cfg = self._config
        idle_polls = 0
        errors = 0
        while not self._stopping.is_set():
            free = cfg.concurrency - len(self._inflight)
            if free <= 0:
                await self._nap(cfg.poll_max_seconds)
                continue
            try:
                jobs = await self._queue.claim(
                    self.worker_id,
                    limit=free,
                    lease_seconds=cfg.lease_seconds,
                    task_names=self._registry.names,
                )
            except Exception:
                errors += 1
                log.exception("queue.claim_failed", consecutive_errors=errors)
                await self._nap(backoff_seconds(_LOOP_ERROR_BACKOFF, min(errors, 10)))
                continue
            errors = 0
            if jobs:
                idle_polls = 0
                for job in jobs:
                    self._spawn(job)
                continue
            idle_polls += 1
            await self._nap(
                min(cfg.poll_max_seconds, cfg.poll_min_seconds * 2 ** min(idle_polls, 10))
            )

    async def _reap_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                reaped = await self._queue.requeue_expired()
                if reaped.requeued or reaped.dead:
                    self.wake()
            except Exception:
                log.exception("queue.reap_failed")
            await self._sleep(self._config.reap_interval_seconds)

    async def _schedule_loop(self) -> None:
        if not self._schedules:
            return
        start = self._clock.now()
        next_due = [
            start if s.run_immediately else start + timedelta(seconds=s.every_seconds)
            for s in self._schedules
        ]
        while not self._stopping.is_set():
            now = self._clock.now()
            for index, schedule in enumerate(self._schedules):
                if now < next_due[index]:
                    continue
                next_due[index] = now + timedelta(seconds=schedule.every_seconds)
                try:
                    result = await self._queue.enqueue(
                        schedule.task_name,
                        schedule.payload,
                        workspace_id=schedule.workspace_id,
                        dedupe_key=f"periodic:{schedule.task_name}",
                        priority=schedule.priority,
                        max_attempts=3,
                    )
                except Exception:
                    log.exception("queue.schedule_enqueue_failed", task=schedule.task_name)
                    continue
                if result.created:
                    self.wake()
            await self._sleep(self._config.schedule_tick_seconds)

    async def _nap(self, seconds: float) -> None:
        """Sleep until woken (new work / a slot freed / stop) or ``seconds`` pass."""
        with suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        self._wake.clear()

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake immediately on stop."""
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    # ───────────────────────────── running a job ─────────────────────────────

    def _spawn(self, job: ClaimedJob) -> None:
        task = asyncio.create_task(self._run_job(job), name=f"job-{job.id}")
        self._inflight.add(task)
        task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task[None]) -> None:
        self._inflight.discard(task)
        self._wake.set()  # a slot is free
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("queue.job_task_crashed", task_name=task.get_name(), error=repr(error))

    async def _drain(self) -> None:
        if not self._inflight:
            return
        pending = set(self._inflight)
        log.info(
            "queue.draining", in_flight=len(pending), grace_s=self._config.shutdown_grace_seconds
        )
        _, still_running = await asyncio.wait(pending, timeout=self._config.shutdown_grace_seconds)
        for task in still_running:
            task.cancel()  # → _run_job releases the job (attempt not consumed)
        if still_running:
            await asyncio.gather(*still_running, return_exceptions=True)

    async def _run_job(self, job: ClaimedJob) -> None:
        with structlog.contextvars.bound_contextvars(
            job_id=str(job.id), task=job.task_name, attempt=job.attempt
        ):
            await self._execute(job)

    async def _execute(self, job: ClaimedJob) -> None:
        spec = self._registry.get(job.task_name)
        if (
            spec is None
        ):  # claim() is filtered by registry names, so this means the registry changed under us
            await self._queue.fail(
                job,
                PermanentJobError(f"no handler registered for {job.task_name!r}"),
                permanent=True,
            )
            return

        runner = asyncio.current_task()
        assert runner is not None
        ctx = JobContext(job)
        heartbeat = asyncio.create_task(self._heartbeat_loop(ctx, runner), name=f"hb-{job.id}")
        timeout = spec.timeout_seconds or self._config.default_timeout_seconds
        finalize: Callable[[], Awaitable[object]]
        log.info("queue.job_started")
        try:
            try:
                async with asyncio.timeout(timeout):
                    result = await spec.handler(ctx)
                finalize = partial(self._queue.complete, job, result)
            except ParkJob as parked:
                until = parked.until or self._clock.now() + timedelta(
                    seconds=parked.delay_seconds or 0
                )
                log.info("queue.job_parked", reason=parked.reason, until=until.isoformat())
                finalize = partial(self._queue.park, job, parked.reason, until=until)
            except JobCancelled:
                finalize = partial(self._queue.mark_cancelled, job)
            except PermanentJobError as exc:
                finalize = partial(self._queue.fail, job, exc, permanent=True)
            except RetryableJobError as exc:
                finalize = partial(self._queue.fail, job, exc, retry_after=exc.retry_after)
            except TimeoutError:
                finalize = partial(
                    self._queue.fail,
                    job,
                    TimeoutError(f"handler exceeded its {timeout:.0f}s timeout"),
                )
            except asyncio.CancelledError:
                heartbeat.cancel()
                if ctx.lease_lost:
                    log.warning(
                        "queue.job_abandoned", reason="lease lost; another worker owns it now"
                    )
                    return
                # Shutdown: hand the job back (shielded so the write survives this cancellation).
                await asyncio.shield(self._queue.release(job))
                log.info("queue.job_released", reason="worker shutting down")
                raise
            except Exception as exc:
                log.warning("queue.job_failed", error_class=type(exc).__name__)
                finalize = partial(self._queue.fail, job, exc)

            heartbeat.cancel()  # stop beating before the terminal write so it can't race it
            await asyncio.shield(finalize())
        finally:
            heartbeat.cancel()

    async def _heartbeat_loop(self, ctx: JobContext, runner: asyncio.Task[Any]) -> None:
        cfg = self._config
        while True:
            await asyncio.sleep(cfg.heartbeat_interval)
            try:
                beat = await self._queue.heartbeat(ctx.job, lease_seconds=cfg.lease_seconds)
            except Exception:
                # A database blip must not kill a healthy job: the lease is still valid until it expires.
                log.warning("queue.heartbeat_failed", job_id=str(ctx.job.id))
                continue
            if beat is None:
                ctx.lease_lost = True
                log.error("queue.lease_lost", job_id=str(ctx.job.id), task=ctx.job.task_name)
                runner.cancel()
                return
            ctx.note_cancel_requested(beat.cancel_requested)
