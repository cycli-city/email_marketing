"""The outbound message builder: correct headers, and every injection attempt refused rather than sent."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from typing import Any, cast

import pytest

from app.integrations.gmail.mime import (
    MAX_MESSAGE_BYTES,
    InvalidAddressError,
    Mailbox,
    MessageBuildError,
    MessageTooLargeError,
    OutboundEmail,
    UnsafeHeaderError,
    build_message,
    message_id_for,
)

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
TOKEN = "0123456789abcdef0123456789abcdef"


def email(**overrides: object) -> OutboundEmail:
    base: dict[str, object] = {
        "sender": Mailbox("founder@example.com", "Divyansh K"),
        "to": Mailbox("lead@example.org", "Pat Lead"),
        "subject": "A quick idea for Acme",
        "body_text": "Hi Pat,\n\nA short note.\n\nDivyansh",
        "sent_at": NOW,
        "message_id": f"<{TOKEN}@example.com>",
    }
    base.update(overrides)
    return OutboundEmail(**base)  # type: ignore[arg-type]


def parse(raw: str) -> EmailMessage:
    return message_from_bytes(base64.urlsafe_b64decode(raw), policy=default)


def date_of(msg: EmailMessage) -> datetime:
    header: Any = msg["Date"]
    return cast(datetime, header.datetime)


class TestHeaders:
    def test_sets_our_identifiers_and_the_standard_headers(self) -> None:
        built = build_message(email())
        msg = parse(built.raw)
        assert msg["Message-ID"] == f"<{TOKEN}@example.com>" == built.rfc_message_id
        assert msg["X-AOI-Message"] == TOKEN
        assert msg["From"] == "Divyansh K <founder@example.com>"
        assert msg["To"] == "Pat Lead <lead@example.org>"
        assert msg["Subject"] == "A quick idea for Acme"
        assert date_of(msg) == NOW
        assert msg["MIME-Version"] == "1.0"
        assert msg.get_content_type() == "text/plain"
        assert msg.get_content().strip().startswith("Hi Pat,")

    def test_the_date_is_always_utc_whatever_zone_the_clock_value_is_in(self) -> None:
        ist = timezone(timedelta(hours=5, minutes=30))
        msg = parse(build_message(email(sent_at=datetime(2026, 11, 19, 15, 30, tzinfo=ist))).raw)
        assert date_of(msg) == NOW

    def test_a_naive_date_is_refused(self) -> None:
        with pytest.raises(MessageBuildError, match="timezone"):
            build_message(email(sent_at=datetime(2026, 11, 19, 10, 0)))

    def test_message_id_uses_the_sender_domain_lowercased(self) -> None:
        assert message_id_for(TOKEN, "Founder@Example.COM") == f"<{TOKEN}@example.com>"

    def test_non_ascii_text_is_encoded_not_mangled(self) -> None:
        built = build_message(
            email(
                subject="Grüße aus München — ₹ pricing",
                body_text="Héllo — naïve café ₹500",
                to=Mailbox("lead@example.org", "Zoë Müller"),
            )
        )
        msg = parse(built.raw)
        assert msg["Subject"] == "Grüße aus München — ₹ pricing"
        assert "Zoë Müller" in str(msg["To"])
        assert "naïve café ₹500" in msg.get_content()
        assert b"\r\n" in base64.urlsafe_b64decode(built.raw)  # CRLF line endings, as SMTP requires

    def test_reply_to_threading_and_references(self) -> None:
        msg = parse(
            build_message(
                email(
                    reply_to="owner@example.net",
                    in_reply_to="<first@example.com>",
                    references=("<first@example.com>", "<second@example.com>"),
                )
            ).raw
        )
        assert msg["Reply-To"] == "owner@example.net"
        assert msg["In-Reply-To"] == "<first@example.com>"
        assert msg["References"] == "<first@example.com> <second@example.com>"

    def test_html_is_an_alternative_to_the_plain_text(self) -> None:
        msg = parse(build_message(email(body_html="<p>Hi <b>Pat</b></p>")).raw)
        assert msg.get_content_type() == "multipart/alternative"
        assert [p.get_content_type() for p in msg.iter_parts()] == ["text/plain", "text/html"]


class TestUnsubscribe:
    def test_https_adds_the_one_click_header(self) -> None:
        msg = parse(
            build_message(
                email(
                    unsubscribe_https="https://api.example.com/u/abc123",
                    unsubscribe_mailto="unsubscribe@example.com",
                )
            ).raw
        )
        folded = " ".join(
            str(msg["List-Unsubscribe"]).split()
        )  # long headers fold onto a continuation line
        assert folded == "<mailto:unsubscribe@example.com>, <https://api.example.com/u/abc123>"
        assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    def test_mailto_alone_does_not_claim_one_click(self) -> None:
        msg = parse(build_message(email(unsubscribe_mailto="unsubscribe@example.com")).raw)
        assert msg["List-Unsubscribe"] == "<mailto:unsubscribe@example.com>"
        assert msg["List-Unsubscribe-Post"] is None

    def test_no_unsubscribe_means_no_header(self) -> None:
        msg = parse(build_message(email()).raw)
        assert msg["List-Unsubscribe"] is None

    @pytest.mark.parametrize(
        "url",
        [
            "http://api.example.com/u/1",  # not https
            "javascript:alert(1)",
            "https://api.example.com/u/1>, <https://evil.example.net/",  # breaks out of the angle brackets
            "https://api.example.com/u/ 1",
            "https://api.example.com/u/1\r\nBcc: victim@example.org",
            "https://api.example.com/" + "a" * 1100,
        ],
    )
    def test_unsafe_unsubscribe_urls_are_refused(self, url: str) -> None:
        with pytest.raises(UnsafeHeaderError):
            build_message(email(unsubscribe_https=url))


class TestInjection:
    """Every field that reaches a header: a line break must be refused, never sent."""

    @pytest.mark.parametrize("breaker", ["\r\n", "\n", "\r", "\x00"])
    def test_subject(self, breaker: str) -> None:
        with pytest.raises(UnsafeHeaderError):
            build_message(email(subject=f"Hello{breaker}Bcc: victim@example.org"))

    @pytest.mark.parametrize("breaker", ["\r\n", "\n"])
    def test_display_names(self, breaker: str) -> None:
        with pytest.raises(UnsafeHeaderError):
            build_message(
                email(to=Mailbox("lead@example.org", f"Pat{breaker}Bcc: victim@example.org"))
            )
        with pytest.raises(UnsafeHeaderError):
            build_message(email(sender=Mailbox("founder@example.com", f"Me{breaker}X: y")))

    @pytest.mark.parametrize(
        "address",
        [
            "lead@example.org\r\nBcc: victim@example.org",
            "lead@example.org, victim@example.net",  # a second recipient smuggled into To
            "lead@example.org>, <victim@example.net",
            "Pat <lead@example.org>",
            "lead@",
            "@example.org",
            "lead@example",  # no dot
            "lé@example.org",  # non-ASCII local part
            "lead@exa mple.org",
            "a" * 65 + "@example.org",
        ],
    )
    def test_recipient_must_be_a_single_plain_address(self, address: str) -> None:
        with pytest.raises(MessageBuildError):
            build_message(email(to=Mailbox(address)))

    def test_sender_and_reply_to_are_validated_too(self) -> None:
        with pytest.raises(MessageBuildError):
            build_message(email(sender=Mailbox("a@example.com\nBcc: x@example.org")))
        with pytest.raises(InvalidAddressError):
            build_message(email(reply_to="not an address"))

    @pytest.mark.parametrize(
        "value",
        [
            "<a@example.com>\r\nBcc: x@example.org",
            "not-an-id",
            "<a b@example.net>",
            "<a@example.com> <d@example.org>",
        ],
    )
    def test_threading_headers(self, value: str) -> None:
        with pytest.raises(UnsafeHeaderError):
            build_message(email(in_reply_to=value))
        with pytest.raises(UnsafeHeaderError):
            build_message(email(references=("<ok@example.com>", value)))

    @pytest.mark.parametrize(
        "token", ["", "short", "has space 123456", "x" * 65, "abc\r\ndef12345", "ünïcödé12345"]
    )
    def test_a_token_must_be_a_plain_random_string(self, token: str) -> None:
        with pytest.raises(MessageBuildError):
            message_id_for(token, "founder@example.com")

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "no-brackets@example.com",
            "<a b@example.com>",
            "<a@example.com>\r\nBcc: x@example.org",
        ],
    )
    def test_the_message_id_must_be_well_formed(self, value: str) -> None:
        with pytest.raises(MessageBuildError):
            build_message(email(message_id=value))

    def test_a_body_cannot_add_headers(self) -> None:
        # Line breaks are legitimate in a body; they must never be able to start a header.
        msg = parse(
            build_message(
                email(body_text="Hi\r\n\r\nBcc: victim@example.org\r\nSubject: hijack")
            ).raw
        )
        assert msg["Bcc"] is None
        assert msg["Subject"] == "A quick idea for Acme"


class TestLimits:
    def test_empty_subject_or_body_is_refused(self) -> None:
        for field in ("subject", "body_text"):
            with pytest.raises(MessageBuildError):
                build_message(email(**{field: "   "}))

    def test_subject_length_is_capped(self) -> None:
        with pytest.raises(MessageBuildError, match="longer"):
            build_message(email(subject="s" * 301))

    def test_a_huge_message_is_refused(self) -> None:
        with pytest.raises(MessageTooLargeError):
            build_message(email(body_text="word " * (MAX_MESSAGE_BYTES // 4)))

    def test_nul_in_the_body_is_refused(self) -> None:
        with pytest.raises(MessageBuildError, match="NUL"):
            build_message(email(body_text="Hi\x00there"))

    def test_the_raw_value_is_url_safe_base64_without_padding_problems(self) -> None:
        raw = build_message(email()).raw
        assert "+" not in raw
        assert "/" not in raw
        base64.urlsafe_b64decode(raw)  # decodes cleanly
