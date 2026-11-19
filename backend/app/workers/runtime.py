"""Starts the queue worker and the mailbox scheduler inside the application process (ADR-024: Postgres is the queue, there
is no broker). The backend keeps working with the dashboard closed: replies are read and unknown sends settled on a
timer, and a restart loses nothing because every job is a durable row.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.db.models import Workspace
from app.integrations.gmail.api import GmailApi
from app.integrations.gmail.credentials import MailboxCredentials
from app.queue.service import JobQueue
from app.queue.worker import TaskRegistry, Worker, WorkerConfig
from app.services.inbox.sync import InboxSync
from app.services.outreach.gate import DenyAllGate
from app.services.outreach.sender import MailboxSender
from app.workers.mailbox_tasks import TASK_INBOX_POLL, TASK_SEND_RECONCILE, register_mailbox_tasks

log = structlog.get_logger(__name__)

STOP_TIMEOUT_SECONDS = 30.0


class MailboxScheduler:
    """Every ``interval`` seconds, makes sure each workspace has a poll and a reconcile job waiting. A dedupe key means a
    slow run never stacks up copies: while one is queued or running, enqueueing another is a no-op."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        queue: JobQueue,
        interval_seconds: float,
        wake: Callable[[], None] = lambda: None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval must be positive")
        self._sessions = sessions
        self._queue = queue
        self._interval = interval_seconds
        self._wake = wake

    async def tick(self) -> int:
        """Enqueue what is due. Returns how many jobs were newly created."""
        async with self._sessions() as s:
            workspace_ids = (await s.execute(select(Workspace.id))).scalars().all()
        created = 0
        for workspace_id in workspace_ids:
            for task in (TASK_INBOX_POLL, TASK_SEND_RECONCILE):
                result = await self._queue.enqueue(
                    task,
                    workspace_id=workspace_id,
                    dedupe_key=f"{task}:{workspace_id}",
                    max_attempts=3,
                )
                created += int(result.created)
        if created:
            self._wake()
        return created

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:  # a database blip must not kill the scheduler
                log.exception("scheduler.tick_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval)


@dataclass
class Background:
    worker: Worker
    scheduler: MailboxScheduler
    sender: MailboxSender
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self.worker.run(), name="queue-worker"),
            asyncio.create_task(self.scheduler.run(self._stop), name="mailbox-scheduler"),
        ]
        for task in self._tasks:
            task.add_done_callback(self._report_exit)

    @staticmethod
    def _report_exit(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.error("background.task_crashed", task=task.get_name(), error=repr(task.exception()))

    @property
    def running(self) -> bool:
        return bool(self._tasks) and not any(t.done() for t in self._tasks)

    async def stop(self) -> None:
        self._stop.set()
        self.worker.stop()
        _, pending = await asyncio.wait(self._tasks, timeout=STOP_TIMEOUT_SECONDS)
        for task in pending:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


def build_background(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    gmail: GmailApi,
    credentials: MailboxCredentials,
    poll_seconds: float,
) -> Background:
    queue = JobQueue(session_factory, clock)
    registry = TaskRegistry()
    # The background worker can settle unknown sends but never start one: the compliance gate is not built yet.
    sender = MailboxSender(
        session_factory=session_factory,
        clock=clock,
        gmail=gmail,
        credentials=credentials,
        gate=DenyAllGate(),
    )
    register_mailbox_tasks(
        registry,
        sessions=session_factory,
        sync=InboxSync(
            session_factory=session_factory, clock=clock, gmail=gmail, credentials=credentials
        ),
        sender=sender,
    )
    worker = Worker(queue, registry, clock=clock, config=WorkerConfig(concurrency=2))
    scheduler = MailboxScheduler(
        sessions=session_factory, queue=queue, interval_seconds=poll_seconds, wake=worker.wake
    )
    return Background(worker=worker, scheduler=scheduler, sender=sender)
