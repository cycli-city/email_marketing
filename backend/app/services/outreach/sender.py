"""Exactly-once email sending (ARCHITECTURE §5).

The promise: **a message is sent at most once, and is never silently lost.** Sending is not idempotent, so the state
machine below is built around the one fact we can always check later — our own ``Message-ID`` in the mailbox's Sent
folder.

::

    QUEUED --claim--> SENDING --Gmail accepted--> SENT
       ^                 |  \\--definitely refused--> FAILED / SKIPPED (gate)
       |                 |   \\--limit / outage before it left--> back to QUEUED (later)
       |                 \\--timeout, reset, 5xx, crash--> SENDING / SEND_UNCERTAIN
       \\---not in Sent after the settle window (at most one more attempt)--- reconciler --found in Sent--> SENT

* **Claim** is one row-locked transaction that also counts the mailbox's recent sends, so two workers can never both
  see "one slot left" (the mailbox row is the lock). Claim and limit check are atomic.
* **Re-check after claiming**: the pre-send gate (compliance, suppression, stop rules) runs after the claim, because
  the world may have changed since the message was queued.
* **Every write after the claim is fenced** by ``claim_token`` and the expected status, so a worker that lost its
  claim (crash recovery, slow call) cannot overwrite what the new owner wrote.
* **Ambiguous outcomes are never retried blindly**: a timeout after the request left, a 5xx, or a crash mean
  ``SEND_UNCERTAIN``/stuck ``SENDING``; only :meth:`MailboxSender.reconcile` resolves them, by looking in Sent.
* **A sending limit is waited out, never routed around**: Gmail's "Mail sending" 429 parks the message; it never moves
  to another mailbox (ADR-025). Each mailbox is one identity.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

import structlog
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.core.redact import scrub_error
from app.db.models import (
    ActivityEvent,
    AuditLog,
    ConnectedEmailAccount,
    EmailMessage,
    EmailThread,
)
from app.db.session import session_scope
from app.domain.enums import (
    AccountStatus,
    ActivityLevel,
    ActorType,
    MessageKind,
    MessageStatus,
)
from app.integrations.gmail.api import GmailApi
from app.integrations.gmail.credentials import MailboxCredentials
from app.integrations.gmail.errors import AccountNeedsReauthError, GoogleUnavailableError
from app.integrations.gmail.mime import Mailbox, MessageBuildError, OutboundEmail, build_message
from app.providers.errors import (
    AuthError,
    ForbiddenReason,
    InvalidResponseError,
    PermanentProviderError,
    PermissionDeniedError,
    ProviderError,
    QuotaExhaustedError,
    RateLimitedError,
    TransientProviderError,
)

log = structlog.get_logger(__name__)

SEND_LEASE = timedelta(minutes=10)  # a SENDING row older than this belonged to a crashed worker
SETTLE = timedelta(
    minutes=15
)  # how long an unconfirmed send is given to show up in Sent before we conclude it did not
MAX_SEND_ATTEMPTS = 2  # first try + exactly one more after the first was confirmed absent
RATE_LIMIT_PARK = timedelta(minutes=5)
QUOTA_PARK = timedelta(
    hours=1
)  # re-check interval after Gmail's sending limit; NOT a claimed reset time
TRANSIENT_PARK = timedelta(minutes=5)
ALIAS_CACHE_TTL = timedelta(minutes=10)
HOUR = timedelta(hours=1)
DAY = timedelta(hours=24)
_RECENT_REFERENCES = 10


class _Progress:
    """Mutable flag shared with the delivery step: has the (non-idempotent) Gmail call been started?"""

    def __init__(self) -> None:
        self.send_started = False


class SendOutcome(StrEnum):
    SENT = "sent"
    NOT_CLAIMABLE = (
        "not_claimable"  # not queued (already sent / being sent by someone else / cancelled)
    )
    DEFERRED = "deferred"  # stays queued; ``retry_at`` says when it may be tried again (None → needs a human)
    REFUSED = "refused"  # the gate said no → SKIPPED
    FAILED = "failed"
    UNCERTAIN = "uncertain"  # may have been sent → the reconciler decides


@dataclass(frozen=True, slots=True)
class SendResult:
    outcome: SendOutcome
    message_id: uuid.UUID
    retry_at: datetime | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    confirmed_sent: int = 0
    requeued: int = 0
    failed: int = 0
    waiting: int = 0


# ───────────────────────────── collaborators ─────────────────────────────


@dataclass(frozen=True, slots=True)
class Clearance:
    """The gate allows the send. It also supplies what only the compliance layer knows."""

    unsubscribe_https: str | None = None
    unsubscribe_mailto: str | None = None
    footer_text: str | None = None
    footer_html: str | None = None


@dataclass(frozen=True, slots=True)
class Refusal:
    reason: str


class SendGate(Protocol):
    """Evaluated *after* the claim: suppression, bounces, replies, jurisdiction, stop rules (ARCHITECTURE §7)."""

    async def check(
        self, session: AsyncSession, message: EmailMessage, account: ConnectedEmailAccount
    ) -> Clearance | Refusal: ...


@dataclass(frozen=True, slots=True)
class _Handle:
    """Identifies one claimed message: enough to make fenced writes and to describe it in the activity feed."""

    workspace_id: uuid.UUID
    message_id: uuid.UUID
    account_id: uuid.UUID
    account_email: str
    claim_token: str
    kind: MessageKind
    rfc_message_id: str


@dataclass(frozen=True, slots=True)
class _Pending:
    """A send whose outcome is unknown, waiting for the reconciler."""

    handle: _Handle
    claimed_at: datetime
    status: MessageStatus
    attempts: int


@dataclass(frozen=True, slots=True)
class _Claim(_Handle):
    """Everything the delivery step needs, copied out of the database so no session is held during the HTTP call."""

    to_email: str
    to_name: str | None
    from_email: str | None
    reply_to: str | None
    subject: str
    body_text: str
    body_html: str | None
    gmail_thread_id: str | None
    in_reply_to: str | None
    references: tuple[str, ...]


# ───────────────────────────── the sender ─────────────────────────────


def _pending_from(m: EmailMessage) -> _Pending | None:
    """Rows without a mailbox, claim or claim time cannot be settled by looking in a Sent folder."""
    if m.account_id is None or m.claim_token is None or m.claimed_at is None:
        return None
    return _Pending(
        handle=_Handle(
            workspace_id=m.workspace_id,
            message_id=m.id,
            account_id=m.account_id,
            account_email="",
            claim_token=m.claim_token,
            kind=m.kind,
            rfc_message_id=m.rfc_message_id,
        ),
        claimed_at=m.claimed_at,
        status=m.status,
        attempts=m.attempt_count,
    )


class MailboxSender:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        gmail: GmailApi,
        credentials: MailboxCredentials,
        gate: SendGate,
        claim_token_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock
        self._gmail = gmail
        self._credentials = credentials
        self._gate = gate
        self._new_claim_token = claim_token_factory
        self._aliases: dict[uuid.UUID, tuple[datetime, frozenset[str]]] = {}

    # ───────────────────────────── send ─────────────────────────────

    async def send(self, workspace_id: uuid.UUID, message_id: uuid.UUID) -> SendResult:
        claimed = await self._claim(workspace_id, message_id)
        if isinstance(claimed, SendResult):
            return claimed
        progress = _Progress()
        try:
            return await self._deliver(claimed, progress)
        except BaseException:
            # An unexpected error (a bug, a cancelled task, a lost database connection) between claim and outcome.
            # If the Gmail call has not been started, nothing was sent: hand the message back rather than strand it.
            # If it HAS been started, it may have gone out - releasing would risk a duplicate, so the row stays
            # SENDING and the reconciler settles it by looking in Sent once the lease runs out.
            if not progress.send_started:
                await self._release(
                    claimed, retry_at=self._clock.now() + TRANSIENT_PARK, consume_attempt=False
                )
            raise

    async def _claim(self, workspace_id: uuid.UUID, message_id: uuid.UUID) -> _Claim | SendResult:
        """Lock the message, then its mailbox, check eligibility and the mailbox's limits, and take the claim."""
        now = self._clock.now()
        async with session_scope(self._sessions) as s:
            message = (
                await s.execute(
                    select(EmailMessage)
                    .where(EmailMessage.id == message_id, EmailMessage.workspace_id == workspace_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            early = self._screen_message(message, message_id, now)
            if early is not None or message is None or message.account_id is None:
                return early or SendResult(SendOutcome.NOT_CLAIMABLE, message_id)

            # The mailbox row is the lock that serialises every claim for that mailbox.
            account = (
                await s.execute(
                    select(ConnectedEmailAccount)
                    .where(
                        ConnectedEmailAccount.id == message.account_id,
                        ConnectedEmailAccount.workspace_id == workspace_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            blocked = await self._screen_account(s, account, message_id, now)
            if blocked is not None or account is None:
                return blocked or SendResult(SendOutcome.NOT_CLAIMABLE, message_id)
            return await self._take_claim(s, message, account, now)

    def _screen_message(
        self, message: EmailMessage | None, message_id: uuid.UUID, now: datetime
    ) -> SendResult | None:
        """``None`` means the message may be sent; anything else is the answer to give instead."""
        if message is None:
            return SendResult(SendOutcome.NOT_CLAIMABLE, message_id, detail="no such message")
        if message.status != MessageStatus.QUEUED:
            return SendResult(
                SendOutcome.NOT_CLAIMABLE, message_id, detail=f"message is {message.status.value}"
            )
        if message.scheduled_for and message.scheduled_for > now:
            return SendResult(SendOutcome.DEFERRED, message_id, retry_at=message.scheduled_for)
        if message.account_id is None:
            message.status = MessageStatus.SKIPPED
            message.skip_reason = "no sending mailbox is assigned to this message"
            return SendResult(SendOutcome.REFUSED, message_id, detail=message.skip_reason)
        return None

    async def _screen_account(
        self,
        s: AsyncSession,
        account: ConnectedEmailAccount | None,
        message_id: uuid.UUID,
        now: datetime,
    ) -> SendResult | None:
        if account is None or account.status != AccountStatus.ACTIVE:
            state = account.status.value if account else "missing"
            return SendResult(
                SendOutcome.DEFERRED, message_id, detail=f"the sending mailbox is {state}"
            )
        blocked = await self._limit_block(s, account, now)
        if blocked is not None:
            return SendResult(
                SendOutcome.DEFERRED, message_id, retry_at=blocked[0], detail=blocked[1]
            )
        return None

    async def _take_claim(
        self, s: AsyncSession, message: EmailMessage, account: ConnectedEmailAccount, now: datetime
    ) -> _Claim:
        thread_id, in_reply_to, references = await self._threading(s, message)
        claim_token = self._new_claim_token()
        message.status = MessageStatus.SENDING
        message.claim_token = claim_token
        message.claimed_at = now
        message.attempt_count += 1
        return _Claim(
            workspace_id=message.workspace_id,
            message_id=message.id,
            account_id=account.id,
            account_email=account.email_address,
            claim_token=claim_token,
            kind=message.kind,
            to_email=message.to_email,
            to_name=message.to_name,
            from_email=message.from_email,
            reply_to=message.reply_to or account.reply_to,
            subject=message.subject,
            body_text=message.body_text,
            body_html=message.body_html,
            rfc_message_id=message.rfc_message_id,
            gmail_thread_id=thread_id,
            in_reply_to=in_reply_to,
            references=references,
        )

    async def _limit_block(
        self, s: AsyncSession, account: ConnectedEmailAccount, now: datetime
    ) -> tuple[datetime | None, str] | None:
        """``(retry_at, why)`` when the mailbox is at its rolling hourly or daily limit, else ``None``.

        Counts what is sent *or may have been sent* (SENDING, SEND_UNCERTAIN) in the window: an unconfirmed send still
        spends a slot, so a crash can never let a mailbox exceed its limit.
        """
        stamp = func.coalesce(EmailMessage.sent_at, EmailMessage.claimed_at)
        for window, limit, label in (
            (HOUR, account.hourly_limit, "hourly"),
            (DAY, account.daily_limit, "daily"),
        ):
            if limit <= 0:
                return None, f"sending is switched off for this mailbox ({label} limit is 0)"
            rows = await s.execute(
                select(stamp)
                .where(
                    EmailMessage.account_id == account.id,
                    EmailMessage.status.in_(
                        (MessageStatus.SENT, MessageStatus.SENDING, MessageStatus.SEND_UNCERTAIN)
                    ),
                    stamp > now - window,
                )
                .order_by(stamp)
            )
            stamps = [row[0] for row in rows]
            if len(stamps) >= limit:
                frees_at = stamps[len(stamps) - limit] + window
                return frees_at, f"the {label} sending limit ({limit}) is reached"
        return None

    async def _threading(
        self, s: AsyncSession, message: EmailMessage
    ) -> tuple[str | None, str | None, tuple[str, ...]]:
        """Gmail thread id plus In-Reply-To/References for a follow-up or reply; nothing for a first message."""
        if message.thread_id is None:
            return None, None, ()
        thread = await s.get(EmailThread, message.thread_id)
        previous = (
            (
                await s.execute(
                    select(EmailMessage.rfc_message_id)
                    .where(
                        EmailMessage.thread_id == message.thread_id,
                        EmailMessage.status == MessageStatus.SENT,
                        EmailMessage.id != message.id,
                    )
                    .order_by(EmailMessage.sent_at.desc())
                    .limit(_RECENT_REFERENCES)
                )
            )
            .scalars()
            .all()
        )
        chain = tuple(reversed(previous))
        return (
            thread.gmail_thread_id if thread else None,
            chain[-1] if chain else None,
            chain,
        )

    # ───────────────────────────── delivery ─────────────────────────────

    async def _deliver(self, c: _Claim, progress: _Progress) -> SendResult:
        decision = await self._gate_decision(c)
        if isinstance(decision, Refusal):
            await self._finish(c, MessageStatus.SKIPPED, skip_reason=decision.reason)
            return SendResult(SendOutcome.REFUSED, c.message_id, detail=decision.reason)

        token = await self._token_or_defer(c)
        if isinstance(token, SendResult):
            return token
        raw = await self._compose_or_stop(c, decision, token)
        if isinstance(raw, SendResult):
            return raw

        # From here on the message may have left; it must never be released blindly.
        progress.send_started = True
        try:
            sent = await self._gmail.send(token, raw, thread_id=c.gmail_thread_id)
        except ProviderError as exc:
            return await self._after_send_error(c, exc, token)
        await self._record_sent(c, sent.id, sent.thread_id, sent_at=self._clock.now())
        return SendResult(SendOutcome.SENT, c.message_id)

    async def _token_or_defer(self, c: _Claim) -> str | SendResult:
        try:
            return await self._credentials.access_token(c.workspace_id, c.account_id)
        except AccountNeedsReauthError:
            await self._release(c, retry_at=None, consume_attempt=False)
            return SendResult(
                SendOutcome.DEFERRED, c.message_id, detail="the mailbox needs reconnecting"
            )
        except GoogleUnavailableError:
            retry_at = self._clock.now() + TRANSIENT_PARK
            await self._release(c, retry_at=retry_at, consume_attempt=False)
            return SendResult(
                SendOutcome.DEFERRED,
                c.message_id,
                retry_at=retry_at,
                detail="Google is unreachable",
            )

    async def _compose_or_stop(
        self, c: _Claim, clearance: Clearance, token: str
    ) -> str | SendResult:
        try:
            return await self._compose(c, clearance, token)
        except MessageBuildError as exc:
            return await self._fail(c, "invalid_message", str(exc), token)
        except ProviderError as exc:  # the identity lookup failed: nothing has been sent
            return await self._after_unsent_error(c, exc, token)

    async def _gate_decision(self, c: _Claim) -> Clearance | Refusal:
        async with self._sessions() as s:
            message = await s.get(EmailMessage, c.message_id)
            account = await s.get(ConnectedEmailAccount, c.account_id)
            if message is None or account is None:
                return Refusal("the message or its mailbox no longer exists")
            return await self._gate.check(s, message, account)

    async def _compose(self, c: _Claim, clearance: Clearance, token: str) -> str:
        from_address = (c.from_email or c.account_email).strip()
        if from_address.lower() != c.account_email.lower():
            await self._require_alias(c, from_address, token)
        body_text = c.body_text + (f"\n\n{clearance.footer_text}" if clearance.footer_text else "")
        body_html = c.body_html
        if body_html and clearance.footer_html:
            body_html = f"{body_html}\n{clearance.footer_html}"
        return build_message(
            OutboundEmail(
                sender=Mailbox(from_address),
                to=Mailbox(c.to_email, c.to_name),
                subject=c.subject,
                body_text=body_text,
                body_html=body_html,
                sent_at=self._clock.now(),
                message_id=c.rfc_message_id,
                reply_to=c.reply_to,
                in_reply_to=c.in_reply_to,
                references=c.references,
                unsubscribe_https=clearance.unsubscribe_https,
                unsubscribe_mailto=clearance.unsubscribe_mailto,
            )
        ).raw

    async def _require_alias(self, c: _Claim, from_address: str, token: str) -> None:
        """Gmail silently rewrites a ``From`` it does not recognise, so only a verified send-as alias is allowed."""
        now = self._clock.now()
        cached = self._aliases.get(c.account_id)
        if cached is None or cached[0] <= now:
            verified = frozenset(
                a.email for a in await self._gmail.list_send_as(token) if a.verified
            )
            self._aliases[c.account_id] = (now + ALIAS_CACHE_TTL, verified)
            cached = self._aliases[c.account_id]
        if from_address.lower() not in cached[1]:
            raise MessageBuildError(
                f"{from_address} is not a verified send-as address of {c.account_email}; "
                "Gmail would send as the mailbox's own address instead"
            )

    # ───────────────────────────── error handling ─────────────────────────────

    async def _after_send_error(self, c: _Claim, exc: ProviderError, token: str) -> SendResult:
        """Decide what a failed Gmail call means. The send call may have left, which changes everything."""
        if isinstance(exc, TransientProviderError) and exc.request_may_have_been_processed:
            await self._finish(
                c,
                MessageStatus.SEND_UNCERTAIN,
                error_class=type(exc).__name__,
                error_detail=scrub_error(str(exc), known_secrets=(token,)),
                keep_claim=True,
            )
            log.warning("send.uncertain", message_id=str(c.message_id), error=type(exc).__name__)
            return SendResult(
                SendOutcome.UNCERTAIN, c.message_id, detail="outcome unknown; will be reconciled"
            )
        return await self._after_unsent_error(c, exc, token)

    async def _after_unsent_error(self, c: _Claim, exc: ProviderError, token: str) -> SendResult:
        """The request definitely did not result in a sent message."""
        now = self._clock.now()
        detail = scrub_error(str(exc), known_secrets=(token,))
        if isinstance(exc, TransientProviderError | InvalidResponseError):
            retry_at = now + TRANSIENT_PARK
        elif isinstance(exc, RateLimitedError):
            retry_at = (
                now + timedelta(seconds=exc.retry_after)
                if exc.retry_after
                else now + RATE_LIMIT_PARK
            )
        elif isinstance(exc, QuotaExhaustedError):
            retry_at = now + QUOTA_PARK
            await self._note(
                c,
                ActivityLevel.WARNING,
                "email_send_limit",
                f"Gmail sending limit reached for {c.account_email}; waiting, not switching mailbox",
            )
        elif isinstance(exc, AuthError):
            await self._credentials.mark_needs_reauth(c.workspace_id, c.account_id)
            await self._release(c, retry_at=None, consume_attempt=False)
            return SendResult(
                SendOutcome.DEFERRED,
                c.message_id,
                detail="Google rejected the mailbox's credentials",
            )
        elif isinstance(exc, PermissionDeniedError) and exc.reason in (
            ForbiddenReason.ACCOUNT_RESTRICTED,
            ForbiddenReason.PERMISSIONS,
        ):
            await self._mark_account_error(c, detail)
            await self._release(c, retry_at=None, consume_attempt=False)
            return SendResult(SendOutcome.DEFERRED, c.message_id, detail=detail)
        elif isinstance(exc, PermanentProviderError | PermissionDeniedError):
            return await self._fail(c, "gmail_rejected", detail, token)
        else:  # an error class we do not know: do not guess, do not loop
            return await self._fail(c, type(exc).__name__, detail, token)
        await self._release(c, retry_at=retry_at, consume_attempt=False)
        return SendResult(SendOutcome.DEFERRED, c.message_id, retry_at=retry_at, detail=detail)

    async def _fail(self, c: _Claim, error_class: str, detail: str, token: str) -> SendResult:
        safe = scrub_error(detail, known_secrets=(token,))
        await self._finish(c, MessageStatus.FAILED, error_class=error_class, error_detail=safe)
        await self._note(
            c, ActivityLevel.ERROR, "email_failed", f"A message could not be sent ({error_class})"
        )
        return SendResult(SendOutcome.FAILED, c.message_id, detail=safe)

    # ───────────────────────────── fenced writes ─────────────────────────────

    async def _finish(
        self,
        c: _Handle,
        status: MessageStatus,
        *,
        skip_reason: str | None = None,
        error_class: str | None = None,
        error_detail: str | None = None,
        keep_claim: bool = False,
    ) -> bool:
        values: dict[str, object] = {
            "status": status,
            "skip_reason": skip_reason,
            "error_class": error_class,
            "error_detail": error_detail,
        }
        if not keep_claim:
            values["claim_token"] = None
        async with session_scope(self._sessions) as s:
            won = (
                await s.execute(
                    update(EmailMessage)
                    .where(
                        EmailMessage.id == c.message_id,
                        EmailMessage.claim_token == c.claim_token,
                        EmailMessage.status == MessageStatus.SENDING,
                    )
                    .values(**values)
                    .returning(EmailMessage.id)
                )
            ).scalar_one_or_none()
        if won is None:
            log.error("send.claim_lost", message_id=str(c.message_id), wanted=status.value)
        return won is not None

    async def _release(
        self, c: _Handle, *, retry_at: datetime | None, consume_attempt: bool
    ) -> None:
        """Back to QUEUED. ``retry_at=None`` leaves the schedule alone (a human must fix something first)."""
        values: dict[str, object] = {
            "status": MessageStatus.QUEUED,
            "claim_token": None,
            "claimed_at": None,
            "attempt_count": EmailMessage.attempt_count - (0 if consume_attempt else 1),
        }
        if retry_at is not None:
            values["scheduled_for"] = retry_at
        async with session_scope(self._sessions) as s:
            await s.execute(
                update(EmailMessage)
                .where(
                    EmailMessage.id == c.message_id,
                    EmailMessage.claim_token == c.claim_token,
                    EmailMessage.status == MessageStatus.SENDING,
                )
                .values(**values)
            )

    async def _record_sent(
        self, c: _Handle, gmail_id: str, gmail_thread_id: str, *, sent_at: datetime
    ) -> bool:
        async with session_scope(self._sessions) as s:
            won = (
                await s.execute(
                    update(EmailMessage)
                    .where(
                        EmailMessage.id == c.message_id,
                        EmailMessage.claim_token == c.claim_token,
                        EmailMessage.status == MessageStatus.SENDING,
                    )
                    .values(
                        status=MessageStatus.SENT,
                        sent_at=sent_at,
                        gmail_message_id=gmail_id,
                        gmail_thread_id=gmail_thread_id,
                        claim_token=None,
                        error_class=None,
                        error_detail=None,
                    )
                    .returning(EmailMessage.id)
                )
            ).scalar_one_or_none()
            if won is None:
                log.error("send.record_lost", message_id=str(c.message_id), gmail_id=gmail_id)
                return False
            message = await s.get(EmailMessage, c.message_id)
            assert message is not None
            await self._link_thread(s, message, c.account_id, gmail_thread_id, sent_at)
            self._remember_sent(s, c, sent_at)
        return True

    async def _link_thread(
        self,
        s: AsyncSession,
        message: EmailMessage,
        account_id: uuid.UUID,
        gmail_thread_id: str,
        sent_at: datetime,
    ) -> None:
        await s.execute(
            pg_insert(EmailThread)
            .values(
                id=uuid.uuid4(),
                workspace_id=message.workspace_id,
                account_id=account_id,
                gmail_thread_id=gmail_thread_id,
                campaign_id=message.campaign_id,
                lead_id=message.lead_id,
                contact_id=message.contact_id,
                subject=message.subject,
                first_message_at=sent_at,
                last_message_at=sent_at,
                message_count=0,
                created_at=sent_at,
                updated_at=sent_at,
            )
            .on_conflict_do_nothing(
                index_elements=["account_id", "gmail_thread_id"],
                index_where=text("gmail_thread_id IS NOT NULL"),
            )
        )
        thread = (
            await s.execute(
                select(EmailThread)
                .where(
                    EmailThread.account_id == account_id,
                    EmailThread.gmail_thread_id == gmail_thread_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        thread.message_count += 1
        thread.last_message_at = max(sent_at, thread.last_message_at or sent_at)
        message.thread_id = thread.id

    def _remember_sent(self, s: AsyncSession, c: _Handle, sent_at: datetime) -> None:
        s.add(
            ActivityEvent(
                workspace_id=c.workspace_id,
                created_at=sent_at,
                level=ActivityLevel.INFO,
                category="email",
                event_type="email_sent",
                message=f"Message sent from {c.account_email}",
                data={"message_id": str(c.message_id), "account_id": str(c.account_id)},
            )
        )
        s.add(
            AuditLog(
                workspace_id=c.workspace_id,
                created_at=sent_at,
                actor_type=ActorType.WORKER,
                action="email.sent",
                entity_type="email_message",
                entity_id=str(c.message_id),
                details={"account_id": str(c.account_id), "kind": c.kind.value},
            )
        )

    async def _note(self, c: _Handle, level: ActivityLevel, event_type: str, message: str) -> None:
        async with session_scope(self._sessions) as s:
            s.add(
                ActivityEvent(
                    workspace_id=c.workspace_id,
                    created_at=self._clock.now(),
                    level=level,
                    category="email",
                    event_type=event_type,
                    message=message,
                    data={"message_id": str(c.message_id), "account_id": str(c.account_id)},
                )
            )

    async def _mark_account_error(self, c: _Handle, detail: str) -> None:
        now = self._clock.now()
        async with session_scope(self._sessions) as s:
            account = (
                await s.execute(
                    select(ConnectedEmailAccount)
                    .where(ConnectedEmailAccount.id == c.account_id)
                    .with_for_update()
                )
            ).scalar_one()
            account.status = AccountStatus.ERROR
            account.last_error = detail[:500]
            account.last_error_at = now
            account.consecutive_failures += 1
            s.add(
                ActivityEvent(
                    workspace_id=c.workspace_id,
                    created_at=now,
                    level=ActivityLevel.ERROR,
                    category="email",
                    event_type="mailbox_error",
                    message=f"{c.account_email} cannot send: {detail[:200]}",
                    data={"account_id": str(c.account_id)},
                )
            )

    # ───────────────────────────── reconciler ─────────────────────────────

    async def reconcile(self, workspace_id: uuid.UUID, *, limit: int = 50) -> ReconcileReport:
        """Resolve every send whose outcome is unknown: ``SEND_UNCERTAIN``, and ``SENDING`` rows left by a crash.

        The answer always comes from the mailbox itself — a Sent-folder search for our Message-ID. Only a message
        confirmed absent after the settle window is retried, and at most :data:`MAX_SEND_ATTEMPTS` times in total.
        """
        now = self._clock.now()
        async with self._sessions() as s:
            stale = (
                (
                    await s.execute(
                        select(EmailMessage)
                        .where(
                            EmailMessage.workspace_id == workspace_id,
                            (EmailMessage.status == MessageStatus.SEND_UNCERTAIN)
                            | (
                                (EmailMessage.status == MessageStatus.SENDING)
                                & (EmailMessage.claimed_at < now - SEND_LEASE)
                            ),
                        )
                        .order_by(EmailMessage.claimed_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            candidates = [_pending_from(m) for m in stale]
            pending = [p for p in candidates if p is not None]
            unreconcilable = len(candidates) - len(pending)
        counts = {"confirmed_sent": 0, "requeued": 0, "failed": 0, "waiting": unreconcilable}
        for item in pending:
            counts[await self._reconcile_one(item)] += 1
        return ReconcileReport(**counts)

    async def _reconcile_one(self, item: _Pending) -> str:
        c = item.handle
        try:
            token = await self._credentials.access_token(c.workspace_id, c.account_id)
            found = await self._gmail.find_by_rfc_message_id(token, c.rfc_message_id)
            ref = await self._gmail.get_message_ref(token, found[0].id) if found else None
        except (AccountNeedsReauthError, GoogleUnavailableError, ProviderError) as exc:
            log.warning(
                "send.reconcile_deferred", message_id=str(c.message_id), error=type(exc).__name__
            )
            return "waiting"

        if found and ref is not None:
            if len(found) > 1:
                await self._note(
                    c,
                    ActivityLevel.ERROR,
                    "duplicate_send",
                    "The same message appears more than once in Sent",
                )
                log.error(
                    "send.duplicate_detected", message_id=str(c.message_id), copies=len(found)
                )
            return await self._confirm_sent(
                c,
                item.status,
                found[0].id,
                found[0].thread_id,
                ref.internal_date or item.claimed_at,
            )
        if self._clock.now() - item.claimed_at < SETTLE:
            return "waiting"
        return await self._conclude_absent(c, item.status, item.attempts)

    async def _confirm_sent(
        self,
        c: _Handle,
        status: MessageStatus,
        gmail_id: str,
        gmail_thread_id: str,
        sent_at: datetime,
    ) -> str:
        async with session_scope(self._sessions) as s:
            message = (
                await s.execute(
                    select(EmailMessage)
                    .where(
                        EmailMessage.id == c.message_id,
                        EmailMessage.claim_token == c.claim_token,
                        EmailMessage.status == status,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if message is None:
                return "waiting"  # resolved by someone else meanwhile
            account = await s.get(ConnectedEmailAccount, c.account_id)
            message.status = MessageStatus.SENT
            message.sent_at = sent_at
            message.gmail_message_id = gmail_id
            message.gmail_thread_id = gmail_thread_id
            message.claim_token = None
            message.error_class = None
            message.error_detail = None
            await self._link_thread(s, message, c.account_id, gmail_thread_id, sent_at)
            self._remember_sent(
                s,
                replace(
                    c, account_email=account.email_address if account else "", kind=message.kind
                ),
                sent_at,
            )
        return "confirmed_sent"

    async def _conclude_absent(self, c: _Handle, status: MessageStatus, attempts: int) -> str:
        """Not in Sent after the settle window: it did not go out. Retry once, then hand it to a human."""
        now = self._clock.now()
        async with session_scope(self._sessions) as s:
            message = (
                await s.execute(
                    select(EmailMessage)
                    .where(
                        EmailMessage.id == c.message_id,
                        EmailMessage.claim_token == c.claim_token,
                        EmailMessage.status == status,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if message is None:
                return "waiting"
            if attempts >= MAX_SEND_ATTEMPTS:
                message.status = MessageStatus.FAILED
                message.error_class = "send_unconfirmed"
                message.error_detail = (
                    "The outcome of two send attempts could not be confirmed and the message is not in Sent. "
                    "Check the mailbox, then re-queue it manually if it really was not delivered."
                )
                message.claim_token = None
                s.add(
                    ActivityEvent(
                        workspace_id=c.workspace_id,
                        created_at=now,
                        level=ActivityLevel.ERROR,
                        category="email",
                        event_type="send_unconfirmed",
                        message="A message could not be confirmed as sent after two attempts; it needs a look",
                        data={"message_id": str(c.message_id)},
                    )
                )
                return "failed"
            message.status = MessageStatus.QUEUED
            message.claim_token = None
            message.claimed_at = None
            message.scheduled_for = now
            message.error_detail = (
                "The previous attempt was confirmed absent from Sent; trying once more."
            )
        return "requeued"
