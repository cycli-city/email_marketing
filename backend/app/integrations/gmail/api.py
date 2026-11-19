"""The Gmail REST calls the sender and the inbox sync need, with failures translated into the provider taxonomy.

The distinction that matters most is for **sending**, which is not idempotent:

=====================================================  ======================================================
what happened                                          what we raise
=====================================================  ======================================================
could not connect / pool timeout (nothing was sent)    ``TransientProviderError(may_have_been_processed=False)``
read timeout, connection reset, 5xx, or a 2xx we       ``TransientProviderError(may_have_been_processed=True)``
cannot parse (Gmail may have accepted it)              → the sender records ``SEND_UNCERTAIN`` and reconciles
4xx refusing the request (bad recipient, scope…)       ``PermanentProviderError`` / ``PermissionDeniedError``
sending limit reached                                  ``QuotaExhaustedError`` — we WAIT, never use another mailbox
=====================================================  ======================================================

Nothing here retries: a retry of a send is a decision for the sender's state machine (ARCHITECTURE §5), not an HTTP
client. HTTPS only and no redirects come from the shared client (``app.core.http``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from app.core.clock import Clock
from app.core.redact import scrub_error
from app.integrations.gmail.inbound import METADATA_HEADERS
from app.integrations.gmail.mime import ADDRESS_RE
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
    parse_retry_after,
)

log = structlog.get_logger(__name__)

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

_TOKEN = re.compile(r"^[A-Za-z0-9]{8,64}$")
_MAX_CANDIDATES = 20
_RETRY_AFTER_IN_TEXT = re.compile(r"retry after (\d{4}-\d{2}-\d{2}T[\d:.]+Z)", re.IGNORECASE)
_SENDING_LIMIT_WORDS = ("mail sending", "sending quota", "sending limit", "daily user sending")
_RATE_REASONS = {"ratelimitexceeded", "userratelimitexceeded"}
_MAX_HINT_SECONDS = 24 * 3600


@dataclass(frozen=True, slots=True)
class SentMessage:
    id: str
    thread_id: str


@dataclass(frozen=True, slots=True)
class MessageRef:
    id: str
    thread_id: str
    internal_date: datetime | None = None  # when Gmail stored it (only from get_message_ref)
    message_id_header: str | None = None  # the Message-ID Gmail assigned (it replaces ours on send)
    aoi_token: str | None = None  # our X-AOI-Message header, which Gmail keeps


class HistoryExpiredError(Exception):
    """Google no longer holds the mailbox history from the stored point (typically after about a week, sometimes
    sooner). The caller falls back to re-reading its own threads."""


@dataclass(frozen=True, slots=True)
class HistoryPage:
    messages: tuple[tuple[str, str], ...]  # (message id, thread id), oldest first
    next_page_token: str | None
    history_id: str | None  # the mailbox's current position, as of this page


@dataclass(frozen=True, slots=True)
class SendAsAlias:
    email: str
    display_name: str | None
    is_primary: bool
    verified: bool  # primary address, or a custom alias Google reports as ``accepted``


# ───────────────────────────── error classification ─────────────────────────────


def _reasons(body: dict[str, Any]) -> set[str]:
    error = body.get("error")
    if not isinstance(error, dict):
        return set()
    found: set[str] = set()
    for item in error.get("errors") or []:
        if isinstance(item, dict) and isinstance(item.get("reason"), str):
            found.add(item["reason"])
    for item in error.get("details") or []:
        if isinstance(item, dict) and isinstance(item.get("reason"), str):
            found.add(item["reason"])
    if isinstance(error.get("status"), str):
        found.add(error["status"])
    return {r.lower().replace("_", "") for r in found}


def _message(body: dict[str, Any]) -> str:
    error = body.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return str(error["message"])
    return ""


def _wait_hint(text: str, header: str | None, now: datetime) -> float | None:
    """Seconds to wait: the ``Retry-After`` header, else the ``Retry after <time>`` Google puts in the message."""
    seconds = parse_retry_after(header, now=now)
    if seconds is None:
        match = _RETRY_AFTER_IN_TEXT.search(text)
        if match:
            try:
                when = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
                seconds = max(0.0, (when.astimezone(UTC) - now).total_seconds())
            except ValueError:
                seconds = None
    return None if seconds is None else min(seconds, _MAX_HINT_SECONDS)


def classify_error(
    status: int,
    headers: httpx.Headers,
    body: dict[str, Any],
    *,
    now: datetime,
    token: str,
    sending: bool = False,
) -> ProviderError:
    """Translate an HTTP error response. ``sending`` makes a 5xx 'maybe processed' (the send may have gone through)."""
    text = scrub_error(_message(body), known_secrets=(token,))[:300]
    lowered = text.lower()
    reasons = _reasons(body)

    if status == 401:
        return AuthError(f"Google rejected the access token: {text}".strip(), http_status=401)
    if any(word in lowered for word in _SENDING_LIMIT_WORDS):
        return QuotaExhaustedError(f"Gmail sending limit reached: {text}", http_status=status)
    if status == 429:
        return RateLimitedError(
            text or "Gmail rate limit",
            retry_after=_wait_hint(text, headers.get("retry-after"), now),
        )
    if status == 403:
        return _classify_forbidden(text, reasons, _wait_hint(text, headers.get("retry-after"), now))
    if status >= 500:
        return TransientProviderError(
            f"Gmail error {status}: {text}".strip(),
            http_status=status,
            request_may_have_been_processed=sending,
        )
    return PermanentProviderError(
        f"Gmail refused the request ({status}): {text}".strip(), http_status=status
    )


def _classify_forbidden(text: str, reasons: set[str], wait: float | None) -> ProviderError:
    """A 403 means very different things; Google's ``reason`` says which."""
    if reasons & _RATE_REASONS:
        return RateLimitedError(text or "Gmail rate limit", retry_after=wait, http_status=403)
    if "dailylimitexceeded" in reasons:
        return QuotaExhaustedError(f"Gmail API daily quota reached: {text}", http_status=403)
    if "domainpolicy" in reasons:
        return PermissionDeniedError(
            f"The Workspace administrator has disabled this app: {text}",
            reason=ForbiddenReason.ACCOUNT_RESTRICTED,
        )
    if {"insufficientpermissions", "forbidden", "permissiondenied"} & reasons:
        return PermissionDeniedError(
            f"Missing permission: {text}", reason=ForbiddenReason.PERMISSIONS
        )
    return PermissionDeniedError(text or "forbidden", reason=ForbiddenReason.UNKNOWN)


def _transport_error(exc: httpx.HTTPError, *, sending: bool) -> TransientProviderError:
    """Nothing reached Gmail for connect-phase failures; anything later may have been processed."""
    never_reached = isinstance(
        exc,
        httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout | httpx.UnsupportedProtocol,
    )
    return TransientProviderError(
        f"could not talk to Gmail ({type(exc).__name__})",
        request_may_have_been_processed=sending and not never_reached,
    )


# ───────────────────────────── client ─────────────────────────────


class GmailApi:
    def __init__(self, http: httpx.AsyncClient, clock: Clock) -> None:
        self._http = http
        self._clock = clock

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        params: dict[str, str | list[str]] | None = None,
        json: dict[str, Any] | None = None,
        sending: bool = False,
    ) -> dict[str, Any]:
        try:
            response = await self._http.request(
                method,
                f"{GMAIL_API}{path}",
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise _transport_error(exc, sending=sending) from exc
        body = _json(response)
        if response.status_code >= 400:
            error = classify_error(
                response.status_code,
                response.headers,
                body,
                now=self._clock.now(),
                token=token,
                sending=sending,
            )
            log.warning(
                "gmail.api_error",
                path="/".join(path.split("/")[:2]),  # "/messages", "/settings" — never an id
                status=response.status_code,
                error=type(error).__name__,
            )
            raise error
        if not 200 <= response.status_code < 300 or (not body and response.status_code != 204):
            # Accepted, but not in a form we understand. For a send that means "probably sent".
            if sending:
                raise TransientProviderError(
                    "Gmail answered the send with an unusable body",
                    http_status=response.status_code,
                    request_may_have_been_processed=True,
                )
            raise InvalidResponseError("Gmail answered with an unusable body")
        return body

    # ───────────────────────────── sending ─────────────────────────────

    async def send(self, token: str, raw: str, *, thread_id: str | None = None) -> SentMessage:
        payload: dict[str, Any] = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        body = await self._request(
            "POST", "/messages/send", token=token, json=payload, sending=True
        )
        message_id, returned_thread = body.get("id"), body.get("threadId")
        if not isinstance(message_id, str) or not isinstance(returned_thread, str):
            raise TransientProviderError(
                "Gmail's send response had no message id",
                request_may_have_been_processed=True,
            )
        return SentMessage(id=message_id, thread_id=returned_thread)

    async def find_sent_by_token(
        self, token: str, *, recipient: str, after: datetime, aoi_token: str
    ) -> list[MessageRef]:
        """Our message in the mailbox, found without knowing the Message-ID Gmail gave it.

        Gmail replaces the ``Message-ID`` header of every message sent through its API, so a search for the id we
        generated can never succeed. What survives is our own ``X-AOI-Message`` header. Search the Sent folder for
        mail to the same recipient since shortly before the send (a handful of candidates at most), then keep the ones
        carrying our token. Spam and trash are included.
        """
        if not ADDRESS_RE.fullmatch(recipient) or not _TOKEN.fullmatch(aoi_token):
            raise PermanentProviderError("refusing to search with a malformed recipient or token")
        body = await self._request(
            "GET",
            "/messages",
            token=token,
            params={
                "q": f"in:sent to:{recipient} after:{int(after.timestamp())}",
                "includeSpamTrash": "true",
                "maxResults": str(_MAX_CANDIDATES),
            },
        )
        matches: list[MessageRef] = []
        for item in body.get("messages") or []:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            ref = await self.get_message_ref(token, item["id"])
            if ref.aoi_token == aoi_token:
                matches.append(ref)
        return matches

    async def get_message_ref(self, token: str, message_id: str) -> MessageRef:
        """Ids, the time Gmail stored the message, the Message-ID it assigned and our token (headers only)."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", message_id):
            raise PermanentProviderError("refusing to fetch a malformed Gmail message id")
        body = await self._request(
            "GET",
            f"/messages/{message_id}",
            token=token,
            params={"format": "metadata", "metadataHeaders": ["Message-ID", "X-AOI-Message"]},
        )
        headers = {
            h["name"].lower(): h["value"]
            for h in (body.get("payload") or {}).get("headers") or []
            if isinstance(h, dict)
            and isinstance(h.get("name"), str)
            and isinstance(h.get("value"), str)
        }
        try:
            stored: datetime | None = datetime.fromtimestamp(
                int(body["internalDate"]) / 1000, tz=UTC
            )
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            stored = None
        return MessageRef(
            id=str(body.get("id", message_id)),
            thread_id=str(body.get("threadId", "")),
            internal_date=stored,
            message_id_header=headers.get("message-id", "").strip()[:300] or None,
            aoi_token=headers.get("x-aoi-message", "").strip() or None,
        )

    # ───────────────────────────── identity ─────────────────────────────

    # ───────────────────────────── reading the inbox ─────────────────────────────

    async def current_history_id(self, token: str) -> str:
        """The mailbox's current history position: the starting point for incremental sync."""
        body = await self._request("GET", "/profile", token=token)
        history_id = body.get("historyId")
        if not isinstance(history_id, str | int):
            raise InvalidResponseError("Gmail's profile had no historyId")
        return str(history_id)

    async def history_page(
        self, token: str, start_history_id: str, *, page_token: str | None = None
    ) -> HistoryPage:
        """Messages added since ``start_history_id``. Raises :class:`HistoryExpiredError` when Google no longer has
        that point in history (a full re-sync is then required)."""
        if not re.fullmatch(r"\d{1,20}", start_history_id):
            raise PermanentProviderError("refusing to use a malformed history id")
        params: dict[str, str | list[str]] = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
            "maxResults": "100",
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            body = await self._request("GET", "/history", token=token, params=params)
        except PermanentProviderError as exc:
            if exc.http_status == 404:
                raise HistoryExpiredError(
                    "Gmail no longer has that point in the mailbox history"
                ) from exc
            raise
        added: list[tuple[str, str]] = []
        for record in body.get("history") or []:
            if not isinstance(record, dict):
                continue
            for item in record.get("messagesAdded") or []:
                message = item.get("message") if isinstance(item, dict) else None
                if isinstance(message, dict) and isinstance(message.get("id"), str):
                    added.append((message["id"], str(message.get("threadId", ""))))
        token_next = body.get("nextPageToken")
        latest = body.get("historyId")
        return HistoryPage(
            messages=tuple(added),
            next_page_token=token_next if isinstance(token_next, str) else None,
            history_id=str(latest) if isinstance(latest, str | int) else None,
        )

    async def get_message(
        self, token: str, message_id: str, *, full: bool = False
    ) -> dict[str, Any]:
        """One message. ``full=False`` fetches only the allow-listed headers (nothing else of the message is read)."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", message_id):
            raise PermanentProviderError("refusing to fetch a malformed Gmail message id")
        params: dict[str, str | list[str]] = (
            {"format": "full"}
            if full
            else {"format": "metadata", "metadataHeaders": list(METADATA_HEADERS)}
        )
        return await self._request("GET", f"/messages/{message_id}", token=token, params=params)

    async def thread_message_ids(self, token: str, thread_id: str) -> tuple[str, ...]:
        """Ids of the messages in one thread (``format=minimal``: no content)."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", thread_id):
            raise PermanentProviderError("refusing to fetch a malformed Gmail thread id")
        body = await self._request(
            "GET", f"/threads/{thread_id}", token=token, params={"format": "minimal"}
        )
        return tuple(
            m["id"]
            for m in body.get("messages") or []
            if isinstance(m, dict) and isinstance(m.get("id"), str)
        )

    async def list_send_as(self, token: str) -> list[SendAsAlias]:
        """The addresses this mailbox may send as. Needed because Gmail would otherwise silently rewrite a ``From``
        it does not recognise to the mailbox's own address."""
        body = await self._request("GET", "/settings/sendAs", token=token)
        aliases: list[SendAsAlias] = []
        for item in body.get("sendAs") or []:
            if not isinstance(item, dict) or not isinstance(item.get("sendAsEmail"), str):
                continue
            primary = bool(item.get("isPrimary"))
            aliases.append(
                SendAsAlias(
                    email=item["sendAsEmail"].lower(),
                    display_name=item.get("displayName") or None,
                    is_primary=primary,
                    verified=primary or item.get("verificationStatus") == "accepted",
                )
            )
        return aliases


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
