"""Builds the RFC 5322 message handed to Gmail. Pure: no I/O, no clock, no randomness (the caller supplies both).

Security properties (each has a test):

* **No header injection.** Every value that ends up in a header is checked for CR, LF and NUL before it is used, and
  addresses must match a strict ASCII pattern. A subject like ``Hi\\r\\nBcc: victim@example.com`` is refused, never sent.
* **Our own ``Message-ID``** (``<token@sender-domain>``, created with :func:`message_id_for` when the message is queued)
  and ``X-AOI-Message`` are always set, so a message can be found again in the Sent folder even if the send response
  was lost (the reconciler's whole job).
* ``List-Unsubscribe`` is https/mailto only, and the one-click header is added only with an https URL (RFC 8058).
* The message is capped in size; it is plain text plus an optional HTML alternative and never carries attachments
  (CVs are Drive links), so a huge body is a bug, not a feature.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime

MAX_MESSAGE_BYTES = 200_000
MAX_SUBJECT_CHARS = 300
MAX_NAME_CHARS = 120
MAX_URL_CHARS = 1000

_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
ADDRESS_RE = re.compile(rf"^{_ATOM}(?:\.{_ATOM})*@{_LABEL}(?:\.{_LABEL})+$")
_MESSAGE_ID_RE = re.compile(r"^<[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+>$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
_UNSAFE_CHARS = re.compile(r"[\r\n\x00]")


class MessageBuildError(ValueError):
    """The message can never be sent as given. Permanent: retrying the same input cannot help."""


class UnsafeHeaderError(MessageBuildError):
    """A header value contained a line break or NUL (header-injection attempt or a bug)."""


class InvalidAddressError(MessageBuildError):
    """Not an address we are willing to put in a header."""


class MessageTooLargeError(MessageBuildError):
    """The finished message exceeds :data:`MAX_MESSAGE_BYTES`."""


@dataclass(frozen=True, slots=True)
class Mailbox:
    address: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class OutboundEmail:
    sender: Mailbox
    to: Mailbox
    subject: str
    body_text: str
    sent_at: datetime  # the Date header; supplied by the caller's clock
    message_id: str  # our own RFC Message-ID, "<token@domain>" (from message_id_for, stored on the row when queued)
    body_html: str | None = None
    reply_to: str | None = None
    in_reply_to: str | None = None  # an RFC Message-ID, "<...@...>"
    references: tuple[str, ...] = ()
    unsubscribe_https: str | None = None
    unsubscribe_mailto: str | None = None


@dataclass(frozen=True, slots=True)
class BuiltMessage:
    raw: str  # base64url of the RFC 5322 message — exactly what Gmail's ``raw`` field wants
    rfc_message_id: str  # "<token@domain>"
    size_bytes: int


def message_id_for(token: str, sender_address: str) -> str:
    """``<token@sender-domain>``. The token must be random (a uuid4 hex); the domain makes it globally unique."""
    _require_token(token)
    domain = _validated_address(sender_address, "sender").rsplit("@", 1)[1].lower()
    return f"<{token}@{domain}>"


def build_message(email: OutboundEmail) -> BuiltMessage:
    sender = _validated_address(email.sender.address, "sender")
    recipient = _validated_address(email.to.address, "recipient")
    rfc_id = _message_id(email.message_id)

    msg = EmailMessage(policy=SMTP)
    msg["From"] = _address(email.sender.name, sender)
    msg["To"] = _address(email.to.name, recipient)
    if email.reply_to:
        msg["Reply-To"] = _address(None, _validated_address(email.reply_to, "reply-to"))
    msg["Subject"] = _subject(email.subject)
    msg["Date"] = format_datetime(_utc(email.sent_at), usegmt=True)
    msg["Message-ID"] = rfc_id
    msg["X-AOI-Message"] = rfc_id[1:-1].split("@", 1)[0]

    if email.in_reply_to:
        msg["In-Reply-To"] = _message_id(email.in_reply_to)
    refs = tuple(_message_id(ref) for ref in email.references)
    if refs:
        msg["References"] = " ".join(refs)

    unsubscribe = _unsubscribe_header(email.unsubscribe_mailto, email.unsubscribe_https)
    if unsubscribe:
        msg["List-Unsubscribe"] = unsubscribe
        if email.unsubscribe_https:  # RFC 8058 one-click needs an https URL
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    msg.set_content(
        _body(email.body_text, "body"), subtype="plain", charset="utf-8", cte="quoted-printable"
    )
    if email.body_html:
        msg.add_alternative(
            _body(email.body_html, "HTML body"),
            subtype="html",
            charset="utf-8",
            cte="quoted-printable",
        )

    data = msg.as_bytes()
    if len(data) > MAX_MESSAGE_BYTES:
        raise MessageTooLargeError(
            f"message is {len(data)} bytes; the limit is {MAX_MESSAGE_BYTES}"
        )
    return BuiltMessage(
        raw=base64.urlsafe_b64encode(data).decode("ascii"),
        rfc_message_id=rfc_id,
        size_bytes=len(data),
    )


# ───────────────────────────── validation helpers ─────────────────────────────


def _no_breaks(value: str, what: str) -> str:
    if _UNSAFE_CHARS.search(value):
        raise UnsafeHeaderError(f"{what} contains a line break or NUL")
    return value


def _validated_address(value: str, what: str) -> str:
    address = _no_breaks(value.strip(), what)
    if (
        len(address) > 254
        or len(address.split("@", 1)[0]) > 64
        or not ADDRESS_RE.fullmatch(address)
    ):
        raise InvalidAddressError(f"{what} is not a valid ASCII e-mail address")
    return address


def _address(name: str | None, address: str) -> Address:
    display = _no_breaks((name or "").strip(), "display name")[:MAX_NAME_CHARS]
    local, domain = address.rsplit("@", 1)
    return Address(display_name=display, username=local, domain=domain)


def _subject(value: str) -> str:
    subject = _no_breaks(value.strip(), "subject")
    if not subject:
        raise MessageBuildError("subject is empty")
    if len(subject) > MAX_SUBJECT_CHARS:
        raise MessageBuildError(f"subject is longer than {MAX_SUBJECT_CHARS} characters")
    return subject


def _body(value: str, what: str) -> str:
    if "\x00" in value:
        raise MessageBuildError(f"{what} contains a NUL byte")
    if not value.strip():
        raise MessageBuildError(f"{what} is empty")
    return value


def _message_id(value: str) -> str:
    candidate = _no_breaks(value.strip(), "message id")
    if not _MESSAGE_ID_RE.fullmatch(candidate) or len(candidate) > 300:
        raise UnsafeHeaderError("not a valid Message-ID (expected <local@domain>)")
    return candidate


def _require_token(token: str) -> None:
    if not _TOKEN_RE.fullmatch(token):
        raise MessageBuildError("message token must be 8-64 alphanumeric characters")


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise MessageBuildError("the Date must be timezone-aware")
    return moment.astimezone(UTC)


def _unsubscribe_header(mailto: str | None, https_url: str | None) -> str:
    parts: list[str] = []
    if mailto:
        parts.append(f"<mailto:{_validated_address(mailto, 'unsubscribe address')}>")
    if https_url:
        url = _no_breaks(https_url.strip(), "unsubscribe URL")
        if (
            not url.startswith("https://")
            or len(url) > MAX_URL_CHARS
            or re.search(r"[\s<>\"]", url)
        ):
            raise UnsafeHeaderError("unsubscribe URL must be a plain https URL")
        parts.append(f"<{url}>")
    return ", ".join(parts)
