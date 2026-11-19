"""Turning a Gmail message (``format=full`` JSON) into the few facts the system needs. Pure: no I/O.

Everything here treats the message as **untrusted data**. Nothing in it is ever followed as an instruction, links are
not fetched, HTML is reduced to plain text, and the amount of text kept is capped.

Deterministic pre-classification (ARCHITECTURE §6) happens here, *before* any model sees the text:

* **bounce** — a delivery-status notification (``multipart/report`` with ``report-type=delivery-status``, a
  ``mailer-daemon``/``postmaster`` sender, or ``X-Failed-Recipients``), parsed for the failed recipient, the status code
  and the ``Message-ID`` of the message that bounced;
* **auto reply** — ``Auto-Submitted`` other than ``no``, ``Precedence: auto_reply|bulk|junk``, ``X-Autoreply`` …;
* otherwise a **human** wrote it.
"""

from __future__ import annotations

import base64
import binascii
import html
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any

import nh3

from app.domain.enums import ReplyKind
from app.integrations.gmail.mime import ADDRESS_RE

MAX_TEXT_CHARS = 8_000
MAX_PART_BYTES = 400_000
MAX_PARTS = 60
MAX_DEPTH = 8
SNIPPET_CHARS = 300

#: The only headers fetched for a message before we know it is ours. Nothing else is read or stored.
METADATA_HEADERS = (
    "From",
    "To",
    "Subject",
    "Date",
    "Message-ID",
    "In-Reply-To",
    "References",
    "Auto-Submitted",
    "Precedence",
    "X-Autoreply",
    "X-Autorespond",
    "X-Failed-Recipients",
    "Content-Type",
)
#: Headers kept on the stored reply (a subset of the above — never ``To`` or ``Date`` content beyond what is needed).
STORED_HEADERS = frozenset(
    {
        "message-id",
        "in-reply-to",
        "references",
        "auto-submitted",
        "precedence",
        "x-failed-recipients",
        "content-type",
    }
)

_MESSAGE_ID = re.compile(r"<[^<>\s@]+@[^<>\s@]+>")
_AUTO_PRECEDENCE = {"auto_reply", "auto-reply", "bulk", "junk", "list"}
_AUTO_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto[- ]?reply|autoreply|out of office|out-of-office|abwesenheit|absence)",
    re.IGNORECASE,
)
_BOUNCE_SENDERS = {"mailer-daemon", "postmaster"}
_AOI_TOKEN = re.compile(
    r"^X-AOI-Message:[ \t]*([A-Za-z0-9]{8,64})[ \t]*$", re.IGNORECASE | re.MULTILINE
)
_STATUS_CODE = re.compile(r"\b([245])\.(\d{1,3})\.(\d{1,3})\b")
_FINAL_RECIPIENT = re.compile(r"^Final-Recipient:\s*[\w-]+;\s*(\S+)", re.IGNORECASE | re.MULTILINE)
_ORIGINAL_RECIPIENT = re.compile(
    r"^Original-Recipient:\s*[\w-]+;\s*(\S+)", re.IGNORECASE | re.MULTILINE
)
_STATUS_LINE = re.compile(r"^Status:\s*(\d\.\d{1,3}\.\d{1,3})", re.IGNORECASE | re.MULTILINE)
_ACTION_LINE = re.compile(r"^Action:\s*(\w+)", re.IGNORECASE | re.MULTILINE)
_WROTE = re.compile(r"^\s*On .{5,200}?wrote:\s*$", re.IGNORECASE | re.DOTALL)
_ORIGINAL_MESSAGE = re.compile(
    r"^\s*(-{2,}|_{5,})\s*(original message|forwarded message|reply message)", re.IGNORECASE
)
_OUTLOOK_HEADER = re.compile(r"^\s*(from|von|de):\s.+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Dsn:
    """What a delivery-status notification says about the message that bounced."""

    failed_recipient: str | None
    status: str | None  # e.g. "5.1.1"
    hard: bool
    original_message_ids: tuple[str, ...]
    #: ``X-AOI-Message`` values quoted from the bounced message's headers — Gmail rewrites the Message-ID on send, but
    #: keeps this one, so it is how a bounce is tied back to the message we sent.
    original_tokens: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Envelope:
    """The cheap facts available from a metadata-only fetch — enough to decide whether the message is ours."""

    gmail_id: str
    thread_id: str
    labels: frozenset[str]
    received_at: datetime | None
    headers: dict[str, str]  # lower-cased names; first occurrence wins
    from_email: str | None
    from_name: str | None
    subject: str | None
    rfc_message_id: str | None
    in_reply_to: tuple[str, ...]
    references: tuple[str, ...]

    @property
    def referenced_ids(self) -> tuple[str, ...]:
        """Every Message-ID this message points back to (``In-Reply-To`` first, then ``References``)."""
        return tuple(dict.fromkeys((*self.in_reply_to, *self.references)))

    @property
    def is_own(self) -> bool:
        """Sent by the mailbox itself (also covers drafts): not an inbound message."""
        return bool({"SENT", "DRAFT"} & self.labels)

    @property
    def looks_like_dsn(self) -> bool:
        return _is_dsn(self.headers, self.from_email)


@dataclass(frozen=True, slots=True)
class Parsed:
    """A matched message, read in full."""

    envelope: Envelope
    kind: ReplyKind
    text: str
    snippet: str
    dsn: Dsn | None


# ───────────────────────────── envelope ─────────────────────────────


def envelope(message: dict[str, Any]) -> Envelope:
    headers = header_map(_payload(message))
    name, address = parseaddr(headers.get("from", ""))
    from_email = address.strip().lower() if ADDRESS_RE.fullmatch(address.strip()) else None
    ids = _ids(headers.get("message-id", ""))
    return Envelope(
        gmail_id=str(message.get("id", "")),
        thread_id=str(message.get("threadId", "")),
        labels=frozenset(x for x in _as_list(message.get("labelIds")) if isinstance(x, str)),
        received_at=_internal_date(message.get("internalDate")),
        headers=headers,
        from_email=from_email,
        from_name=_clean(name)[:300] or None,
        subject=_clean(headers.get("subject", ""))[:500] or None,
        rfc_message_id=ids[0] if ids else None,
        in_reply_to=_ids(headers.get("in-reply-to", "")),
        references=_ids(headers.get("references", "")),
    )


def header_map(payload: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in payload.get("headers") or []:
        if (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("value"), str)
        ):
            found.setdefault(item["name"].lower(), item["value"])
    return found


def stored_headers(env: Envelope) -> dict[str, str]:
    return {k: v[:1000] for k, v in env.headers.items() if k in STORED_HEADERS}


def _ids(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_MESSAGE_ID.findall(value)))


def _clean(value: str) -> str:
    """One line of printable text: control characters and runs of whitespace collapse to a single space."""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]+", " ", value)).strip()


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _payload(message: dict[str, Any]) -> dict[str, Any]:
    payload = message.get("payload")
    return payload if isinstance(payload, dict) else {}


def _internal_date(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(str(value)) / 1000, tz=UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


# ───────────────────────────── classification ─────────────────────────────


def _is_dsn(headers: dict[str, str], from_email: str | None) -> bool:
    content_type = headers.get("content-type", "").lower()
    sender_local = (from_email or "").split("@", 1)[0]
    return (
        ("multipart/report" in content_type and "delivery-status" in content_type)
        or sender_local in _BOUNCE_SENDERS
        or "x-failed-recipients" in headers
    )


def classify(env: Envelope) -> ReplyKind:
    if _is_dsn(env.headers, env.from_email):
        return ReplyKind.BOUNCE
    auto = env.headers.get("auto-submitted", "no").strip().lower()
    if (
        (auto and auto != "no")
        or env.headers.get("precedence", "").strip().lower() in _AUTO_PRECEDENCE
        or "x-autoreply" in env.headers
        or "x-autorespond" in env.headers
        or _AUTO_SUBJECT.match(env.subject or "")
    ):
        return ReplyKind.AUTO_REPLY
    return ReplyKind.HUMAN


# ───────────────────────────── bodies ─────────────────────────────


def walk(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Depth-first over the MIME tree, bounded in depth and in number of parts (a hostile message can be a bomb)."""
    stack: list[tuple[dict[str, Any], int]] = [(payload, 0)]
    seen = 0
    while stack and seen < MAX_PARTS:
        part, depth = stack.pop()
        seen += 1
        yield part
        if depth < MAX_DEPTH:
            children = [p for p in part.get("parts") or [] if isinstance(p, dict)]
            stack.extend((child, depth + 1) for child in reversed(children))


def _decode(part: dict[str, Any]) -> str:
    body = part.get("body")
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, str) or len(data) > MAX_PART_BYTES * 2:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return ""
    return raw[:MAX_PART_BYTES].decode("utf-8", errors="replace")


def _is_attachment(part: dict[str, Any]) -> bool:
    return bool(part.get("filename"))


def html_to_text(markup: str) -> str:
    """All tags removed (and the content of script/style), entities decoded. The result is plain text, never HTML."""
    stripped = nh3.clean(markup, tags=set(), clean_content_tags={"script", "style"})
    return html.unescape(stripped)


def extract_text(payload: dict[str, Any]) -> str:
    plain: list[str] = []
    markup: list[str] = []
    for part in walk(payload):
        if _is_attachment(part):
            continue
        mime = str(part.get("mimeType", "")).lower()
        if mime == "text/plain":
            plain.append(_decode(part))
        elif mime == "text/html":
            markup.append(_decode(part))
    text = "\n".join(p for p in plain if p.strip()) or "\n".join(
        html_to_text(m) for m in markup if m.strip()
    )
    return normalise(strip_quoted(text))[:MAX_TEXT_CHARS]


def strip_quoted(text: str) -> str:
    """Keep what the person wrote, drop the thread they replied to (``> …`` lines, "On … wrote:", Outlook headers)."""
    kept: list[str] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for index, line in enumerate(lines):
        if _ORIGINAL_MESSAGE.match(line):
            break
        if _WROTE.match(line) or _wrote_wrapped(lines, index):
            break
        if _OUTLOOK_HEADER.match(line) and _outlook_block(lines, index):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _wrote_wrapped(lines: list[str], index: int) -> bool:
    """ "On Mon, 3 Nov 2026 at 10:02, Pat <pat@…>\\nwrote:" — the attribution line is often wrapped."""
    if not lines[index].lstrip().startswith("On ") or index + 1 >= len(lines):
        return False
    return bool(_WROTE.match(f"{lines[index]} {lines[index + 1].strip()}"))


def _outlook_block(lines: list[str], index: int) -> bool:
    window = " ".join(lines[index : index + 5]).lower()
    return ("sent:" in window or "gesendet:" in window or "envoyé" in window) and (
        "subject:" in window or "betreff:" in window or "objet" in window
    )


def normalise(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text).strip()


# ───────────────────────────── bounces ─────────────────────────────


def parse_dsn(payload: dict[str, Any], headers: dict[str, str]) -> Dsn | None:
    """Read a delivery-status notification. ``None`` if the message does not actually contain one."""
    status_text = ""
    original_ids: list[str] = []
    original_tokens: list[str] = []
    for part in walk(payload):
        mime = str(part.get("mimeType", "")).lower()
        if mime == "message/delivery-status":
            status_text += "\n" + _decode(part) + _nested_text(part)
        elif mime in {"message/rfc822", "text/rfc822-headers"}:
            quoted = _decode(part) + _nested_text(part)
            original_ids.extend(_ids(quoted))
            original_tokens.extend(_AOI_TOKEN.findall(quoted))
    failed = _first(_FINAL_RECIPIENT, status_text) or _first(_ORIGINAL_RECIPIENT, status_text)
    if not failed:
        failed = (headers.get("x-failed-recipients", "").split(",")[0].strip()) or None
    status = _first(_STATUS_LINE, status_text)
    if status is None:
        match = _STATUS_CODE.search(_all_text(payload))
        status = match.group(0) if match else None
    if not (failed or status or original_ids or original_tokens):
        return None
    action = (_first(_ACTION_LINE, status_text) or "").lower()
    return Dsn(
        failed_recipient=_valid_address(failed),
        status=status,
        hard=_is_hard(status, action),
        original_message_ids=tuple(dict.fromkeys(original_ids)),
        original_tokens=tuple(dict.fromkeys(original_tokens)),
    )


def _is_hard(status: str | None, action: str) -> bool:
    """A permanent failure: class 5, except 'mailbox full' (5.2.2), which resolves by itself."""
    if action in {"delayed", "delivered", "relayed", "expanded"}:
        return False
    return bool(status and status.startswith("5.") and status != "5.2.2")


def _nested_text(part: dict[str, Any]) -> str:
    return "\n".join(_decode(child) for child in part.get("parts") or [] if isinstance(child, dict))


def _all_text(payload: dict[str, Any]) -> str:
    return "\n".join(
        _decode(p) for p in walk(payload) if str(p.get("mimeType", "")).startswith("text/")
    )[:20_000]


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def _valid_address(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip().strip("<>").lower()
    return candidate if ADDRESS_RE.fullmatch(candidate) else None


# ───────────────────────────── entry point ─────────────────────────────


def parse(message: dict[str, Any]) -> Parsed:
    env = envelope(message)
    payload = _payload(message)
    kind = classify(env)
    dsn = parse_dsn(payload, env.headers) if kind == ReplyKind.BOUNCE else None
    text = extract_text(payload)
    snippet = _clean(html.unescape(str(message.get("snippet", ""))))[:SNIPPET_CHARS]
    return Parsed(envelope=env, kind=kind, text=text, snippet=snippet, dsn=dsn)
