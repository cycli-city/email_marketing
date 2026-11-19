"""A fake Gmail REST API (over ``httpx.MockTransport``) that remembers what was "sent".

The point of keeping the sent messages is the exactly-once tests: after any crash, timeout or retry the test can ask
the fake how many copies of a message really exist. Failures are injected per send, in order, via :attr:`script`.
"""

from __future__ import annotations

import base64
import json
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from tests.gmail_helpers import PERSONAL_EMAIL


class Fate(StrEnum):
    """What happens to the next send."""

    OK = "ok"
    ACCEPT_THEN_TIMEOUT = "accept_then_timeout"  # Gmail stores it, the client never hears back
    ACCEPT_THEN_GARBAGE = (
        "accept_then_garbage"  # Gmail stores it, answers 200 with an unusable body
    )
    ACCEPT_THEN_503 = "accept_then_503"  # stored, but the answer is a 5xx
    REJECT_503 = "reject_503"  # not stored, 5xx
    CONNECT_ERROR = "connect_error"  # nothing reached Gmail
    BAD_REQUEST = "bad_request"  # 400, not stored
    UNAUTHORIZED = "unauthorized"  # 401, not stored
    SENDING_LIMIT = "sending_limit"  # 429 "Mail sending", not stored
    RATE_LIMITED = "rate_limited"  # 429 with Retry-After, not stored
    RATE_LIMITED_NO_HINT = "rate_limited_no_hint"  # 429 without Retry-After, not stored
    DOMAIN_POLICY = "domain_policy"  # 403 domainPolicy, not stored


@dataclass
class Stored:
    id: str
    thread_id: str
    internal_ms: int
    message: EmailMessage
    raw: bytes

    @property
    def rfc_id(self) -> str:
        return str(self.message["Message-ID"])


@dataclass
class FakeGmail:
    clock: Any  # anything with .now() -> aware datetime
    email: str = PERSONAL_EMAIL
    aliases: list[dict[str, Any]] = field(default_factory=list)
    script: deque[Fate] = field(default_factory=deque)
    stored: list[Stored] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    tokens_seen: list[str] = field(default_factory=list)
    valid_tokens: set[str] | None = None  # None → any bearer token is accepted
    # Inbox side: messages that arrived (Gmail "full" JSON), the history log, and what was read how.
    messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[tuple[int, str, str]] = field(
        default_factory=list
    )  # (history id, message id, thread id)
    history_id: int = 1000
    history_floor: int = 0  # a startHistoryId below this is "too old" → 404
    fail_ids: set[str] = field(default_factory=set)  # reading these messages fails with a 503
    full_fetches: list[str] = field(default_factory=list)
    metadata_fetches: list[str] = field(default_factory=list)
    _counter: int = 0

    # ───────────────────────────── plumbing ─────────────────────────────

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def sends(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/messages/send")]

    def copies_of(self, rfc_id: str) -> int:
        return sum(1 for s in self.stored if s.rfc_id == rfc_id)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ")
        self.tokens_seen.append(token)
        if self.valid_tokens is not None and token not in self.valid_tokens:
            return _error(401, "Invalid Credentials", "authError")
        path = urlsplit(str(request.url)).path.removeprefix("/gmail/v1/users/me")
        query = parse_qs(urlsplit(str(request.url)).query)
        routes: list[tuple[str, str, Callable[[re.Match[str]], httpx.Response]]] = [
            ("POST", r"/messages/send", lambda m: self._send(request)),
            ("GET", r"/messages", lambda m: self._search(query.get("q", [""])[0])),
            ("GET", r"/messages/([\w-]+)", lambda m: self._get(m.group(1), query)),
            ("GET", r"/profile", lambda m: self._profile()),
            ("GET", r"/history", lambda m: self._history(query)),
            ("GET", r"/threads/([\w-]+)", lambda m: self._thread(m.group(1))),
            ("GET", r"/settings/sendAs", lambda m: self._send_as()),
        ]
        for method, pattern, respond in routes:
            match = re.fullmatch(pattern, path)
            if match and method == request.method:
                return respond(match)
        return httpx.Response(404, json={"error": {"code": 404, "message": f"no route {path}"}})

    # ───────────────────────────── operations ─────────────────────────────

    def _store(self, request: httpx.Request) -> Stored:
        payload = json.loads(request.content)
        raw = base64.urlsafe_b64decode(payload["raw"])
        self._counter += 1
        return_thread = payload.get("threadId") or f"thread{self._counter:04d}"
        stored = Stored(
            id=f"msg{self._counter:04d}",
            thread_id=return_thread,
            internal_ms=int(self.clock.now().timestamp() * 1000),
            message=message_from_bytes(raw, policy=default),
            raw=raw,
        )
        self.stored.append(stored)
        self._event(stored.id, stored.thread_id)
        return stored

    def _send(self, request: httpx.Request) -> httpx.Response:
        fate = self.script.popleft() if self.script else Fate.OK
        if fate is Fate.CONNECT_ERROR:
            raise httpx.ConnectError("connection refused", request=request)
        if fate in _REFUSALS:  # answered without storing anything
            status, message, reason, headers = _REFUSALS[fate]
            return _error(status, message, reason, headers)
        stored = self._store(request)
        if fate is Fate.ACCEPT_THEN_TIMEOUT:
            raise httpx.ReadTimeout("timed out waiting for the response", request=request)
        if fate is Fate.ACCEPT_THEN_GARBAGE:
            return httpx.Response(200, text="<html>not json</html>")
        if fate is Fate.ACCEPT_THEN_503:
            return _error(503, "Backend Error", "backendError")
        return httpx.Response(
            200, json={"id": stored.id, "threadId": stored.thread_id, "labelIds": ["SENT"]}
        )

    def _search(self, q: str) -> httpx.Response:
        match = re.fullmatch(r"rfc822msgid:(\S+)", q)
        if not match:
            return _error(400, "unsupported query", "invalidArgument")
        wanted = f"<{match.group(1)}>"
        found = [s for s in self.stored if s.rfc_id == wanted]
        body: dict[str, Any] = {"resultSizeEstimate": len(found)}
        if found:
            body["messages"] = [{"id": s.id, "threadId": s.thread_id} for s in found]
        return httpx.Response(200, json=body)

    def _get(self, message_id: str, query: dict[str, list[str]]) -> httpx.Response:
        if message_id in self.fail_ids:
            return _error(503, "Backend Error", "backendError")
        fmt = query.get("format", ["full"])[0].lower()
        inbound = self.messages.get(message_id)
        if inbound is not None:
            return self._inbound(inbound, fmt, query.get("metadataHeaders", []))
        for s in self.stored:
            if s.id == message_id:
                return httpx.Response(
                    200,
                    json={
                        "id": s.id,
                        "threadId": s.thread_id,
                        "internalDate": str(s.internal_ms),
                        "labelIds": ["SENT"],
                    },
                )
        return _error(404, "Requested entity was not found.", "notFound")

    def _inbound(self, message: dict[str, Any], fmt: str, wanted: list[str]) -> httpx.Response:
        if fmt == "minimal":
            keys = ("id", "threadId", "labelIds", "internalDate")
            return httpx.Response(200, json={k: message[k] for k in keys})
        if fmt == "metadata":
            self.metadata_fetches.append(message["id"])
            lowered = {w.lower() for w in wanted}
            payload = message["payload"]
            headers = [h for h in payload["headers"] if not lowered or h["name"].lower() in lowered]
            return httpx.Response(
                200,
                json={
                    **{k: message[k] for k in ("id", "threadId", "labelIds", "internalDate")},
                    "payload": {"mimeType": payload["mimeType"], "headers": headers},
                },
            )
        self.full_fetches.append(message["id"])
        return httpx.Response(200, json=message)

    def _profile(self) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "emailAddress": self.email,
                "historyId": str(self.history_id),
                "messagesTotal": 1,
            },
        )

    def _history(self, query: dict[str, list[str]]) -> httpx.Response:
        start = int(query["startHistoryId"][0])
        if start < self.history_floor:
            return _error(404, "Requested entity was not found.", "notFound")
        due = [e for e in self.events if e[0] > start]
        size = int(query.get("maxResults", ["100"])[0])
        offset = int(query.get("pageToken", ["0"])[0])
        page = due[offset : offset + size]
        body: dict[str, Any] = {"historyId": str(self.history_id)}
        if page:
            body["history"] = [
                {"id": str(hid), "messagesAdded": [{"message": {"id": mid, "threadId": tid}}]}
                for hid, mid, tid in page
            ]
        if offset + size < len(due):
            body["nextPageToken"] = str(offset + size)
        return httpx.Response(200, json=body)

    def _thread(self, thread_id: str) -> httpx.Response:
        ids = [e[1] for e in self.events if e[2] == thread_id]
        if not ids:
            return _error(404, "Requested entity was not found.", "notFound")
        return httpx.Response(200, json={"id": thread_id, "messages": [{"id": i} for i in ids]})

    # ───────────────────────────── test controls ─────────────────────────────

    def _event(self, message_id: str, thread_id: str) -> None:
        self.history_id += 1
        self.events.append((self.history_id, message_id, thread_id))

    def expire_history(self) -> None:
        """Google forgets everything before 'now': an incremental sync from an older point gets a 404 (a sync from
        the current position is still fine)."""
        self.history_floor = self.history_id

    def deliver(
        self,
        *,
        sender: str = "Pat Lead <pat@example.org>",
        subject: str = "Re: A quick idea",
        text: str | None = "Sounds interesting - can we talk Tuesday?",
        html: str | None = None,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        headers: dict[str, str] | None = None,
        labels: tuple[str, ...] = ("INBOX", "UNREAD"),
        payload: dict[str, Any] | None = None,
    ) -> str:
        """A message arrives in the mailbox. Returns its Gmail id."""
        self._counter += 1
        message_id = f"in{self._counter:04d}"
        thread = thread_id or f"thr{self._counter:04d}"
        header_map: dict[str, str] = {
            "From": sender,
            "To": self.email,
            "Subject": subject,
            "Message-ID": f"<{message_id}@mail.example.org>",
        }
        if in_reply_to:
            header_map["In-Reply-To"] = in_reply_to
        if references:
            header_map["References"] = references
        header_map.update(headers or {})
        root = payload or _body(text, html)
        root = {**root, "headers": [{"name": k, "value": v} for k, v in header_map.items()]}
        self.messages[message_id] = {
            "id": message_id,
            "threadId": thread,
            "labelIds": list(labels),
            "internalDate": str(int(self.clock.now().timestamp() * 1000)),
            "snippet": (text or "")[:80],
            "payload": root,
        }
        self._event(message_id, thread)
        return message_id

    def _send_as(self) -> httpx.Response:
        primary = {
            "sendAsEmail": self.email,
            "isPrimary": True,
            "isDefault": True,
            "displayName": "Owner",
        }
        return httpx.Response(200, json={"sendAs": [primary, *self.aliases]})


def _error(
    status: int, message: str, reason: str, headers: dict[str, str] | None = None
) -> httpx.Response:
    errors = [{"reason": reason, "message": message}] if reason else []
    return httpx.Response(
        status,
        json={"error": {"code": status, "message": message, "errors": errors}},
        headers=headers,
    )


# Fates that Gmail answers without storing the message: (status, message, reason, extra headers).
_REFUSALS: dict[Fate, tuple[int, str, str, dict[str, str] | None]] = {
    Fate.REJECT_503: (503, "Backend Error", "backendError", None),
    Fate.BAD_REQUEST: (400, "Invalid To header", "invalidArgument", None),
    Fate.UNAUTHORIZED: (401, "Invalid Credentials", "authError", None),
    Fate.SENDING_LIMIT: (
        429,
        "Too many requests: User-rate limit exceeded (Mail sending)",
        "",
        None,
    ),
    Fate.RATE_LIMITED: (429, "Too many concurrent requests for user", "", {"Retry-After": "45"}),
    Fate.RATE_LIMITED_NO_HINT: (429, "Too many concurrent requests for user", "", None),
    Fate.DOMAIN_POLICY: (
        403,
        "Gmail apps are disabled by your administrator",
        "domainPolicy",
        None,
    ),
}


def alias(email: str, *, verified: bool = True) -> dict[str, Any]:
    return {
        "sendAsEmail": email,
        "isPrimary": False,
        "verificationStatus": "accepted" if verified else "pending",
        "displayName": "Alias",
    }


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def text_part(mime: str, text: str) -> dict[str, Any]:
    return {"mimeType": mime, "filename": "", "body": {"data": _b64(text), "size": len(text)}}


def _body(text: str | None, html: str | None) -> dict[str, Any]:
    if text is not None and html is None:
        return text_part("text/plain", text)
    parts = [text_part("text/plain", text)] if text is not None else []
    if html is not None:
        parts.append(text_part("text/html", html))
    return {"mimeType": "multipart/alternative", "filename": "", "body": {}, "parts": parts}
