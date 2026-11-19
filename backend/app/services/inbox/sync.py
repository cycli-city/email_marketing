"""Reading replies out of a connected mailbox (ARCHITECTURE §6) — and only replies to *our* mail.

The connected mailboxes also hold the owner's private email, and ``gmail.readonly`` can technically read all of it. The
**scope guard** is therefore the heart of this module: a message is only stored if it is demonstrably part of a
conversation this system started —

1. it sits in a Gmail thread of ours (``email_threads``), or
2. it answers (``In-Reply-To``/``References``) one of our own ``Message-ID``s, or
3. it is a delivery-status notification that quotes one of our ``Message-ID``s.

Everything else is **ignored without reading its body**: unrelated mail costs one metadata request limited to the
allow-listed headers in :data:`~app.integrations.gmail.inbound.METADATA_HEADERS`, and nothing about it is persisted.

Delivery is at-least-once: the history cursor only advances after every message of the batch was handled, and storing a
reply is idempotent (unique per mailbox + Gmail message id), so a crash or a repeated poll never duplicates anything.
A message that cannot be parsed is stored with an error status (a poison message must not block the mailbox); a
failure of Gmail itself stops the batch and leaves the cursor where it was.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.db.models import (
    ActivityEvent,
    ConnectedEmailAccount,
    EmailMessage,
    EmailThread,
    IncomingReply,
    SuppressionEntry,
    WatchState,
)
from app.db.session import session_scope
from app.domain.enums import (
    AccountStatus,
    ActivityLevel,
    MessageStatus,
    ReplyKind,
    ReplyProcessingStatus,
    SuppressionReason,
)
from app.integrations.gmail.api import GmailApi, HistoryExpiredError
from app.integrations.gmail.credentials import MailboxCredentials
from app.integrations.gmail.errors import AccountNeedsReauthError, GoogleUnavailableError
from app.integrations.gmail.inbound import Parsed, envelope, parse, stored_headers
from app.providers.errors import AuthError, PermanentProviderError, ProviderError

log = structlog.get_logger(__name__)

_MAX_HISTORY_PAGES = 50
_UNKNOWN_SENDER = "unknown@unknown.invalid"


@dataclass(frozen=True, slots=True)
class SyncReport:
    stored: int = 0  # new replies recorded
    ignored: int = 0  # not part of any conversation of ours — nothing kept
    own: int = 0  # the mailbox's own messages
    duplicates: int = 0  # already recorded earlier
    bounces: int = 0
    initialised: bool = False  # first poll: the cursor was set, nothing read
    resynced: bool = False  # Google had forgotten the cursor: our own threads were re-read
    deferred: str | None = None  # why the poll stopped early (cursor left unchanged)


@dataclass(frozen=True, slots=True)
class _Match:
    method: str  # thread_id | in_reply_to | dsn_original_id
    confidence: float
    thread: EmailThread | None
    original: EmailMessage | None


@dataclass(frozen=True, slots=True)
class _Mailbox:
    """The mailbox being polled, with a token valid for this poll."""

    workspace_id: uuid.UUID
    account_id: uuid.UUID
    address: str  # lower-cased
    token: str


@dataclass(frozen=True, slots=True)
class _Verdict:
    """What looking at one message concluded."""

    status: str  # "match" | "ignored" | "own"
    message: dict[str, Any] | None = None  # the full message, only for a match
    match: _Match | None = None


@dataclass
class _Tally:
    stored: int = 0
    ignored: int = 0
    own: int = 0
    duplicates: int = 0
    bounces: int = 0
    stop: str | None = None
    seen: set[str] = field(default_factory=set)


class InboxSync:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        gmail: GmailApi,
        credentials: MailboxCredentials,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock
        self._gmail = gmail
        self._credentials = credentials

    # ───────────────────────────── the poll ─────────────────────────────

    async def poll(self, workspace_id: uuid.UUID, account_id: uuid.UUID) -> SyncReport:
        async with self._sessions() as s:
            account = (
                await s.execute(
                    select(ConnectedEmailAccount).where(
                        ConnectedEmailAccount.id == account_id,
                        ConnectedEmailAccount.workspace_id == workspace_id,
                    )
                )
            ).scalar_one_or_none()
            if account is None or account.status != AccountStatus.ACTIVE:
                state = account.status.value if account else "missing"
                return SyncReport(deferred=f"the mailbox is {state}")
            state_row = (
                await s.execute(select(WatchState).where(WatchState.account_id == account_id))
            ).scalar_one_or_none()
            cursor = state_row.history_id if state_row else None
            own_address = account.email_address.lower()

        try:
            token = await self._credentials.access_token(workspace_id, account_id)
        except AccountNeedsReauthError:
            return SyncReport(deferred="the mailbox needs reconnecting")
        except GoogleUnavailableError:
            return SyncReport(deferred="Google is unreachable")

        try:
            box = _Mailbox(workspace_id, account_id, own_address, token)
            return await self._poll(box, cursor)
        except AuthError:
            await self._credentials.mark_needs_reauth(workspace_id, account_id)
            return SyncReport(deferred="Google rejected the mailbox's credentials")
        except ProviderError as exc:
            log.warning("inbox.poll_deferred", account_id=str(account_id), error=type(exc).__name__)
            return SyncReport(deferred=f"Gmail is not answering ({type(exc).__name__})")

    async def _poll(self, box: _Mailbox, cursor: str | None) -> SyncReport:
        if cursor is None:
            # Nothing before this moment can be a reply to something we sent, so start from here.
            latest = await self._gmail.current_history_id(box.token)
            await self._save_cursor(box.workspace_id, box.account_id, latest, full_sync=True)
            return SyncReport(initialised=True)

        resynced = False
        try:
            found, latest = await self._history_since(box.token, cursor)
        except HistoryExpiredError:
            log.warning("inbox.history_expired", account_id=str(box.account_id))
            # Take the new position first, so nothing that arrives while we re-read is missed.
            latest = await self._gmail.current_history_id(box.token)
            found = await self._our_thread_messages(box.token, box.workspace_id, box.account_id)
            resynced = True

        tally = _Tally()
        for message_id, thread_id in found:
            if message_id in tally.seen:
                continue
            tally.seen.add(message_id)
            await self._handle(box, message_id, thread_id, tally)
            if tally.stop:
                break

        if tally.stop is None:
            await self._save_cursor(box.workspace_id, box.account_id, latest, full_sync=resynced)
        return SyncReport(
            stored=tally.stored,
            ignored=tally.ignored,
            own=tally.own,
            duplicates=tally.duplicates,
            bounces=tally.bounces,
            resynced=resynced,
            deferred=tally.stop,
        )

    async def _history_since(self, token: str, cursor: str) -> tuple[list[tuple[str, str]], str]:
        found: list[tuple[str, str]] = []
        latest = cursor
        page_token: str | None = None
        for _ in range(_MAX_HISTORY_PAGES):
            page = await self._gmail.history_page(token, cursor, page_token=page_token)
            found.extend(page.messages)
            latest = page.history_id or latest
            page_token = page.next_page_token
            if page_token is None:
                return found, latest
        raise HistoryExpiredError("too much unread history; re-reading our own threads instead")

    async def _our_thread_messages(
        self, token: str, workspace_id: uuid.UUID, account_id: uuid.UUID
    ) -> list[tuple[str, str]]:
        """After lost history: the messages of the conversations we started — never a search of the whole mailbox."""
        async with self._sessions() as s:
            thread_ids = (
                (
                    await s.execute(
                        select(EmailThread.gmail_thread_id).where(
                            EmailThread.account_id == account_id,
                            EmailThread.workspace_id == workspace_id,
                            EmailThread.gmail_thread_id.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
        found: list[tuple[str, str]] = []
        for thread_id in thread_ids:
            if thread_id is None:
                continue
            try:
                found.extend(
                    (m, thread_id) for m in await self._gmail.thread_message_ids(token, thread_id)
                )
            except PermanentProviderError as exc:
                if exc.http_status != 404:  # a thread the owner deleted is simply gone
                    raise
        return found

    # ───────────────────────────── one message ─────────────────────────────

    async def _handle(self, box: _Mailbox, message_id: str, thread_id: str, tally: _Tally) -> None:
        try:
            known = await self._already_known(box.account_id, message_id)
            if known is not None:
                tally.own += known == "own"
                tally.duplicates += known == "duplicate"
                return
            verdict = await self._inspect(box, message_id, thread_id)
            if verdict.status == "own":
                tally.own += 1
            elif verdict.status == "ignored" or verdict.message is None or verdict.match is None:
                tally.ignored += 1
            else:
                await self._store(box, parse(verdict.message), verdict.match, tally)
        except AuthError:
            raise
        except PermanentProviderError as exc:
            if exc.http_status != 404:
                raise
            tally.ignored += 1  # deleted between the history entry and now
        except ProviderError as exc:
            tally.stop = f"Gmail is not answering ({type(exc).__name__})"

    async def _inspect(self, box: _Mailbox, message_id: str, thread_id: str) -> _Verdict:
        """Decide whether the message belongs to a conversation of ours. Reads the body only if it does."""
        thread = await self._thread(box.account_id, thread_id)
        if thread is not None:
            full = await self._gmail.get_message(box.token, message_id, full=True)
            return _Verdict(
                "match", full, _Match("thread_id", 1.0, thread, await self._last_sent(thread.id))
            )

        env = envelope(await self._gmail.get_message(box.token, message_id))
        if env.is_own or env.from_email == box.address:
            return _Verdict("own")
        original = await self._sent_by_rfc_id(box.workspace_id, env.referenced_ids)
        if original is not None:
            full = await self._gmail.get_message(box.token, message_id, full=True)
            return _Verdict(
                "match",
                full,
                _Match("in_reply_to", 0.95, await self._thread_of(original), original),
            )
        if env.looks_like_dsn:
            full = await self._gmail.get_message(box.token, message_id, full=True)
            dsn = parse(full).dsn
            bounced = await self._sent_by_rfc_id(
                box.workspace_id, dsn.original_message_ids if dsn else ()
            )
            if bounced is not None:
                return _Verdict(
                    "match",
                    full,
                    _Match("dsn_original_id", 0.95, await self._thread_of(bounced), bounced),
                )
        return _Verdict("ignored")

    # ───────────────────────────── lookups ─────────────────────────────

    async def _already_known(self, account_id: uuid.UUID, gmail_message_id: str) -> str | None:
        """``"own"`` for a message we sent ourselves, ``"duplicate"`` for one already recorded, else ``None``.

        Gmail message ids are only unique *within* a mailbox, so both checks are scoped to this mailbox."""
        async with self._sessions() as s:
            sent_here = await s.execute(
                select(EmailMessage.id)
                .where(
                    EmailMessage.account_id == account_id,
                    EmailMessage.gmail_message_id == gmail_message_id,
                )
                .limit(1)
            )
            if sent_here.first() is not None:
                return "own"
            stored = await s.execute(
                select(IncomingReply.id).where(
                    IncomingReply.account_id == account_id,
                    IncomingReply.gmail_message_id == gmail_message_id,
                )
            )
            return "duplicate" if stored.first() is not None else None

    async def _thread(self, account_id: uuid.UUID, gmail_thread_id: str) -> EmailThread | None:
        if not gmail_thread_id:
            return None
        async with self._sessions() as s:
            return (
                await s.execute(
                    select(EmailThread).where(
                        EmailThread.account_id == account_id,
                        EmailThread.gmail_thread_id == gmail_thread_id,
                    )
                )
            ).scalar_one_or_none()

    async def _thread_of(self, message: EmailMessage) -> EmailThread | None:
        if message.thread_id is None:
            return None
        async with self._sessions() as s:
            return await s.get(EmailThread, message.thread_id)

    async def _last_sent(self, thread_id: uuid.UUID) -> EmailMessage | None:
        async with self._sessions() as s:
            return (
                await s.execute(
                    select(EmailMessage)
                    .where(
                        EmailMessage.thread_id == thread_id,
                        EmailMessage.status == MessageStatus.SENT,
                    )
                    .order_by(EmailMessage.sent_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def _sent_by_rfc_id(
        self, workspace_id: uuid.UUID, rfc_ids: tuple[str, ...]
    ) -> EmailMessage | None:
        if not rfc_ids:
            return None
        async with self._sessions() as s:
            return (
                await s.execute(
                    select(EmailMessage)
                    .where(
                        EmailMessage.workspace_id == workspace_id,
                        EmailMessage.rfc_message_id.in_(rfc_ids),
                    )
                    .order_by(EmailMessage.sent_at.desc().nulls_last())
                    .limit(1)
                )
            ).scalar_one_or_none()

    # ───────────────────────────── storing ─────────────────────────────

    async def _store(self, box: _Mailbox, parsed: Parsed, match: _Match, tally: _Tally) -> None:
        env = parsed.envelope
        if env.is_own or env.from_email == box.address:
            tally.own += 1  # the owner answered from Gmail himself: not an inbound reply
            return
        now = self._clock.now()
        received = env.received_at or now
        original = match.original
        async with session_scope(self._sessions) as s:
            inserted = (
                await s.execute(
                    pg_insert(IncomingReply)
                    .values(
                        id=uuid.uuid4(),
                        workspace_id=box.workspace_id,
                        account_id=box.account_id,
                        thread_id=match.thread.id if match.thread else None,
                        in_response_to_message_id=original.id if original else None,
                        campaign_id=(original.campaign_id if original else None)
                        or (match.thread.campaign_id if match.thread else None),
                        lead_id=(original.lead_id if original else None)
                        or (match.thread.lead_id if match.thread else None),
                        contact_id=(original.contact_id if original else None)
                        or (match.thread.contact_id if match.thread else None),
                        gmail_message_id=env.gmail_id,
                        gmail_thread_id=env.thread_id or None,
                        rfc_message_id=env.rfc_message_id,
                        in_reply_to=env.in_reply_to[0] if env.in_reply_to else None,
                        from_email=env.from_email or _UNKNOWN_SENDER,
                        from_name=env.from_name,
                        subject=env.subject,
                        received_at=received,
                        snippet=parsed.snippet or None,
                        body_text=parsed.text or None,
                        kind=parsed.kind,
                        match_method=match.method,
                        match_confidence=match.confidence,
                        headers=stored_headers(env),
                        processing_status=ReplyProcessingStatus.PENDING,
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=["account_id", "gmail_message_id"])
                    .returning(IncomingReply.id)
                )
            ).scalar_one_or_none()
            if inserted is None:
                tally.duplicates += 1
                return
            thread = match.thread
            if thread is not None:
                live = await s.get(EmailThread, thread.id, with_for_update=True)
                if live is not None:
                    live.message_count += 1
                    live.last_message_at = max(received, live.last_message_at or received)
                    live.last_inbound_at = max(received, live.last_inbound_at or received)
            if parsed.kind == ReplyKind.BOUNCE:
                tally.bounces += 1
                await self._record_bounce(
                    s, box.workspace_id, parsed, original=original, received=received, now=now
                )
            elif parsed.kind == ReplyKind.HUMAN:
                s.add(
                    self._event(
                        box.workspace_id,
                        now,
                        ActivityLevel.INFO,
                        "reply_received",
                        "A reply arrived in a campaign thread",
                        about=original,
                    )
                )
        tally.stored += 1

    async def _record_bounce(
        self,
        s: AsyncSession,
        workspace_id: uuid.UUID,
        parsed: Parsed,
        *,
        original: EmailMessage | None,
        received: datetime,
        now: datetime,
    ) -> None:
        dsn = parsed.dsn
        if original is None or dsn is None:
            return
        live = await s.get(EmailMessage, original.id, with_for_update=True)
        if live is None:
            return
        live.bounced_at = received
        live.bounce_kind = "hard" if dsn.hard else "soft"
        # Suppress only the address we actually mailed. A notification naming some *other* recipient is not allowed to
        # put arbitrary addresses on the do-not-contact list.
        target = live.to_email.lower()
        if dsn.hard and (dsn.failed_recipient is None or dsn.failed_recipient == target):
            await s.execute(
                pg_insert(SuppressionEntry)
                .values(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    email=target,
                    reason=SuppressionReason.HARD_BOUNCE,
                    source="bounce",
                    note=f"delivery status {dsn.status or 'unknown'}",
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=["workspace_id", "email"], index_where=text("email IS NOT NULL")
                )
            )
        s.add(
            self._event(
                workspace_id,
                now,
                ActivityLevel.WARNING,
                "bounce_detected",
                f"A message bounced ({'permanently' if dsn.hard else 'temporarily'}, status {dsn.status or 'unknown'})",
                about=live,
            )
        )

    @staticmethod
    def _event(
        workspace_id: uuid.UUID,
        at: datetime,
        level: ActivityLevel,
        event_type: str,
        message: str,
        *,
        about: EmailMessage | None,
    ) -> ActivityEvent:
        return ActivityEvent(
            workspace_id=workspace_id,
            created_at=at,
            level=level,
            category="reply",
            event_type=event_type,
            message=message,
            campaign_id=about.campaign_id if about else None,
            lead_id=about.lead_id if about else None,
            data={"message_id": str(about.id)} if about else {},
        )

    async def _save_cursor(
        self, workspace_id: uuid.UUID, account_id: uuid.UUID, history_id: str, *, full_sync: bool
    ) -> None:
        now = self._clock.now()
        async with session_scope(self._sessions) as s:
            await s.execute(
                pg_insert(WatchState)
                .values(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    account_id=account_id,
                    history_id=history_id,
                    last_polled_at=now,
                    last_full_sync_at=now if full_sync else None,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["account_id"],
                    set_={
                        "history_id": history_id,
                        "last_polled_at": now,
                        "updated_at": now,
                        **({"last_full_sync_at": now} if full_sync else {}),
                        "last_error": None,
                    },
                )
            )
            account = await s.get(ConnectedEmailAccount, account_id)
            if account is not None:
                account.last_sync_at = now
