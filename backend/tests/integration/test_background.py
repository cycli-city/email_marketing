"""The background machinery: handlers, the scheduler, and a real worker reading a reply with no script involved."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import IncomingReply, Job
from app.domain.enums import AccountStatus, MessageStatus
from app.queue.errors import JobCancelled
from app.queue.service import JobQueue
from app.queue.types import ClaimedJob
from app.queue.worker import JobContext, TaskRegistry
from app.services.outreach.sender import SendOutcome
from app.workers.mailbox_tasks import TASK_INBOX_POLL, TASK_SEND_RECONCILE, register_mailbox_tasks
from app.workers.runtime import MailboxScheduler, build_background
from tests.gmail_fake import Fate
from tests.mailbox_world import Factory, World


@pytest.fixture
def world(factory: Factory, workspace_id: uuid.UUID) -> World:
    return World(factory, workspace_id)


def context(world: World, task: str) -> JobContext:
    return JobContext(
        ClaimedJob(
            id=uuid.uuid4(),
            workspace_id=world.workspace_id,
            task_name=task,
            payload={},
            dedupe_key=None,
            priority=100,
            attempt=1,
            max_attempts=3,
            locked_by="test-worker",
        )
    )


def registry(world: World) -> TaskRegistry:
    tasks = TaskRegistry()
    register_mailbox_tasks(tasks, sessions=world.factory, sync=world.sync, sender=world.sender)
    return tasks


async def run(world: World, task: str, ctx: JobContext | None = None) -> Any:
    spec = registry(world).get(task)
    assert spec is not None
    return await spec.handler(ctx or context(world, task))


async def conversation(world: World) -> tuple[uuid.UUID, str]:
    """A mailbox that has sent one message and has its inbox cursor set; returns (account id, gmail thread id)."""
    account_id = await world.account()
    await world.poll(account_id)
    message_id = await world.message(account_id)
    assert (await world.send(message_id)).outcome == SendOutcome.SENT
    return account_id, str((await world.row(message_id)).gmail_thread_id)


# ───────────────────────────── handlers ─────────────────────────────


class TestInboxPollTask:
    async def test_polls_every_active_mailbox_and_only_those(self, world: World) -> None:
        active_a, thread = await conversation(world)
        active_b = await world.account()
        await world.poll(active_b)
        await world.account(status=AccountStatus.NEEDS_REAUTH)  # must not be touched
        world.gmail.deliver(thread_id=thread, text="Interested - call me.")
        before = len(world.gmail.requests)

        result = await run(world, TASK_INBOX_POLL)

        assert result["mailboxes"] == 2
        assert result["stored"] == 1  # a's reply; b has no such conversation
        assert result["errors"] == 0
        assert len(world.gmail.requests) > before
        assert active_a != active_b

    async def test_one_broken_mailbox_does_not_stop_the_others(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good, thread = await conversation(world)
        broken = await world.account()
        world.gmail.deliver(thread_id=thread, text="Yes.")
        original = world.sync.poll

        async def flaky(workspace_id: uuid.UUID, account_id: uuid.UUID) -> Any:
            if account_id == broken:
                raise RuntimeError("something unexpected in this mailbox")
            return await original(workspace_id, account_id)

        monkeypatch.setattr(world.sync, "poll", flaky)

        result = await run(world, TASK_INBOX_POLL)

        assert (result["mailboxes"], result["errors"], result["stored"]) == (2, 1, 1)
        assert good != broken

    async def test_when_every_mailbox_fails_the_job_fails_so_the_queue_can_retry_and_report_it(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await world.account()

        async def always(*_: object) -> Any:
            raise RuntimeError("down")

        monkeypatch.setattr(world.sync, "poll", always)
        with pytest.raises(RuntimeError, match="every connected mailbox"):
            await run(world, TASK_INBOX_POLL)

    async def test_no_mailboxes_is_a_quiet_success(self, world: World) -> None:
        result = await run(world, TASK_INBOX_POLL)
        assert (result["mailboxes"], result["errors"]) == (0, 0)
        assert world.gmail.requests == []

    async def test_an_operator_cancel_stops_it_between_mailboxes(self, world: World) -> None:
        await world.account()
        ctx = context(world, TASK_INBOX_POLL)
        ctx.note_cancel_requested(True)
        with pytest.raises(JobCancelled):
            await run(world, TASK_INBOX_POLL, ctx)
        assert world.gmail.requests == []


class TestReconcileTask:
    async def test_settles_an_unknown_send_from_the_mailbox(self, world: World) -> None:
        account_id = await world.account()
        message_id = await world.message(account_id)
        world.gmail.script.append(Fate.ACCEPT_THEN_TIMEOUT)
        await world.send(message_id)
        world.clock.advance(minutes=1)

        result = await run(world, TASK_SEND_RECONCILE)

        assert result["confirmed_sent"] == 1
        assert (await world.row(message_id)).status == MessageStatus.SENT

    async def test_with_nothing_to_settle_gmail_is_not_called(self, world: World) -> None:
        await world.account()
        result = await run(world, TASK_SEND_RECONCILE)
        assert result == {"confirmed_sent": 0, "requeued": 0, "failed": 0, "waiting": 0}
        assert world.gmail.requests == []


# ───────────────────────────── the scheduler ─────────────────────────────


async def queued(world: World) -> list[Job]:
    return list(await world.all(Job))


class TestScheduler:
    def scheduler(self, world: World, wake: Any = None) -> tuple[MailboxScheduler, JobQueue]:
        queue = JobQueue(world.factory, world.clock)
        scheduler = MailboxScheduler(
            sessions=world.factory, queue=queue, interval_seconds=30, wake=wake or (lambda: None)
        )
        return scheduler, queue

    async def test_each_workspace_gets_a_poll_and_a_reconcile_job(
        self, world: World, other_workspace_id: uuid.UUID
    ) -> None:
        scheduler, _ = self.scheduler(world)
        created = await scheduler.tick()
        jobs = [
            j
            for j in await queued(world)
            if j.workspace_id in {world.workspace_id, other_workspace_id}
        ]
        assert created >= 4
        assert {(j.workspace_id, j.task_name) for j in jobs} >= {
            (ws, task)
            for ws in (world.workspace_id, other_workspace_id)
            for task in (TASK_INBOX_POLL, TASK_SEND_RECONCILE)
        }

    async def test_a_slow_run_never_stacks_up_copies(self, world: World) -> None:
        scheduler, _ = self.scheduler(world)
        await scheduler.tick()
        first = len([j for j in await queued(world) if j.workspace_id == world.workspace_id])
        again = await scheduler.tick()
        second = len([j for j in await queued(world) if j.workspace_id == world.workspace_id])
        assert (first, second) == (2, 2)
        assert again == 0

    async def test_a_finished_job_is_replaced_on_the_next_tick(self, world: World) -> None:
        scheduler, queue = self.scheduler(world)
        await scheduler.tick()
        claimed = await queue.claim(
            worker_id="w", lease_seconds=30, task_names=[TASK_INBOX_POLL], limit=5
        )
        mine = [j for j in claimed if j.workspace_id == world.workspace_id]
        assert mine
        for job in mine:
            assert await queue.complete(job)
        assert await scheduler.tick() >= 1  # a new poll is queued once the previous one is done

    async def test_it_wakes_the_worker_only_when_something_was_queued(self, world: World) -> None:
        woken: list[int] = []
        scheduler, _ = self.scheduler(world, wake=lambda: woken.append(1))
        await scheduler.tick()
        await scheduler.tick()
        assert woken == [1]

    def test_a_non_positive_interval_is_refused(self, world: World) -> None:
        queue = JobQueue(world.factory, world.clock)
        with pytest.raises(ValueError, match="positive"):
            MailboxScheduler(sessions=world.factory, queue=queue, interval_seconds=0)


# ───────────────────────────── the whole thing, running by itself ─────────────────────────────


class TestRunningByItself:
    async def test_a_reply_is_picked_up_with_no_script_involved_and_shutdown_is_clean(
        self, world: World
    ) -> None:
        account_id, thread = await conversation(world)
        world.gmail.deliver(thread_id=thread, text="Sounds good - Tuesday works.")

        background = build_background(
            session_factory=world.factory,
            clock=world.clock,
            gmail=world.sender._gmail,
            credentials=world.credentials,
            poll_seconds=0.3,
        )
        background.start()
        try:
            stored: list[IncomingReply] = []
            for _ in range(100):  # up to ~15 s
                async with world.factory() as s:
                    stored = list((await s.execute(select(IncomingReply))).scalars())
                if stored:
                    break
                await asyncio.sleep(0.15)
            assert [r.body_text for r in stored] == ["Sounds good - Tuesday works."]
            assert background.running
        finally:
            await background.stop()

        assert not background.running
        assert account_id is not None

    async def test_the_background_worker_can_never_start_a_send(self, world: World) -> None:
        """Until the compliance gate exists, the sender the runtime builds refuses every message."""
        account_id = await world.account()
        message_id = await world.message(account_id)
        background = build_background(
            session_factory=world.factory,
            clock=world.clock,
            gmail=world.sender._gmail,
            credentials=world.credentials,
            poll_seconds=0.3,
        )

        result = await background.sender.send(world.workspace_id, message_id)

        assert result.outcome == SendOutcome.REFUSED
        assert "not enabled" in (result.detail or "")
        assert (await world.row(message_id)).status == MessageStatus.SKIPPED
        assert world.gmail.sends() == []
