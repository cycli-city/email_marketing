"""Queue task handlers for the connected mailboxes. Thin wrappers over the services: no logic of their own.

* ``inbox.poll`` — read new replies from every active mailbox of the workspace (ADR-036).
* ``outreach.reconcile`` — settle sends whose outcome is unknown (ADR-035). Cheap when there is nothing to settle: no
  call to Gmail is made unless a message is actually waiting.

Both are idempotent, so the queue's at-least-once delivery is harmless, and a failure in one mailbox never stops the others.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ConnectedEmailAccount
from app.domain.enums import AccountProvider, AccountStatus
from app.queue.worker import JobContext, TaskRegistry
from app.services.inbox.sync import InboxSync
from app.services.outreach.sender import MailboxSender

log = structlog.get_logger(__name__)

TASK_INBOX_POLL = "inbox.poll"
TASK_SEND_RECONCILE = "outreach.reconcile"


def register_mailbox_tasks(
    registry: TaskRegistry,
    *,
    sessions: async_sessionmaker[AsyncSession],
    sync: InboxSync,
    sender: MailboxSender,
) -> None:
    @registry.task(TASK_INBOX_POLL, timeout_seconds=180)
    async def inbox_poll(ctx: JobContext) -> Mapping[str, Any]:
        workspace_id = ctx.job.workspace_id
        async with sessions() as s:
            account_ids = (
                (
                    await s.execute(
                        select(ConnectedEmailAccount.id).where(
                            ConnectedEmailAccount.workspace_id == workspace_id,
                            ConnectedEmailAccount.provider == AccountProvider.GMAIL,
                            ConnectedEmailAccount.status == AccountStatus.ACTIVE,
                        )
                    )
                )
                .scalars()
                .all()
            )
        totals = {"mailboxes": 0, "stored": 0, "ignored": 0, "deferred": 0, "errors": 0}
        for account_id in account_ids:
            ctx.raise_if_cancelled()
            totals["mailboxes"] += 1
            try:
                report = await sync.poll(workspace_id, account_id)
            except (
                Exception
            ):  # one broken mailbox must not stop the others; it is logged and counted
                log.exception("inbox.poll_failed", account_id=str(account_id))
                totals["errors"] += 1
                continue
            totals["stored"] += report.stored
            totals["ignored"] += report.ignored
            totals["deferred"] += 1 if report.deferred else 0
        if account_ids and totals["errors"] == len(account_ids):
            # Every mailbox failed: let the queue's retry / dead-letter machinery surface it.
            raise RuntimeError("inbox.poll failed for every connected mailbox")
        return totals

    @registry.task(TASK_SEND_RECONCILE, timeout_seconds=180)
    async def send_reconcile(ctx: JobContext) -> Mapping[str, Any]:
        report = await sender.reconcile(ctx.job.workspace_id)
        return {
            "confirmed_sent": report.confirmed_sent,
            "requeued": report.requeued,
            "failed": report.failed,
            "waiting": report.waiting,
        }
