"""The pure rules behind the lead store: what a vendor verdict is allowed to mean, and how untrusted text is cleaned."""

from __future__ import annotations

import pytest

from app.domain.enums import EmailKind, EmailOrigin, VerificationStatus
from app.providers.discovery import VerificationOutcome
from app.services.discovery.store import clean, clean_country, kind_for, status_for_outcome

ADDRESS = "owner@example.com"


def outcome(verdict: str, **flags: bool | None) -> VerificationOutcome:
    return VerificationOutcome(email=ADDRESS, verdict=verdict, detail="vendor words", **flags)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("verdict", "origin", "flags", "expected"),
    [
        ("deliverable", EmailOrigin.DISCOVERED, {}, VerificationStatus.VERIFIED),
        ("deliverable", EmailOrigin.PROVIDER, {}, VerificationStatus.VERIFIED),
        ("deliverable", EmailOrigin.MANUAL, {}, VerificationStatus.VERIFIED),
        # a guess can never be verified, however good the vendor says it looks
        ("deliverable", EmailOrigin.GUESSED, {}, VerificationStatus.RISKY),
        # a catch-all domain accepts anything, so the vendor cannot have confirmed this mailbox
        ("deliverable", EmailOrigin.DISCOVERED, {"is_catch_all": True}, VerificationStatus.RISKY),
        ("deliverable", EmailOrigin.DISCOVERED, {"is_disposable": True}, VerificationStatus.RISKY),
        (
            "deliverable",
            EmailOrigin.DISCOVERED,
            {"is_catch_all": False, "is_disposable": False},
            VerificationStatus.VERIFIED,
        ),
        ("risky", EmailOrigin.DISCOVERED, {}, VerificationStatus.RISKY),
        ("undeliverable", EmailOrigin.DISCOVERED, {}, VerificationStatus.INVALID),
        ("undeliverable", EmailOrigin.GUESSED, {}, VerificationStatus.INVALID),
        ("unknown", EmailOrigin.DISCOVERED, {}, VerificationStatus.UNVERIFIED),
    ],
)
def test_verdict_mapping(
    verdict: str, origin: EmailOrigin, flags: dict[str, bool], expected: VerificationStatus
) -> None:
    assert status_for_outcome(outcome(verdict, **flags), origin) is expected


def test_a_disposable_domain_we_know_ourselves_is_never_verified_even_if_the_vendor_misses_it() -> (
    None
):
    mailinator = VerificationOutcome(
        email="x@" + "mailinator.com", verdict="deliverable", detail="valid"
    )
    assert status_for_outcome(mailinator, EmailOrigin.DISCOVERED) is VerificationStatus.RISKY


@pytest.mark.parametrize(
    ("email", "generic", "has_name", "expected"),
    [
        ("info@example.com", False, False, EmailKind.ROLE_BASED),
        (
            "info@example.com",
            False,
            True,
            EmailKind.ROLE_BASED,
        ),  # a name does not make info@ a person
        ("jane.doe@example.com", True, True, EmailKind.ROLE_BASED),  # the vendor says it is shared
        ("jane.doe@example.com", False, True, EmailKind.NAMED),
        ("jane.doe@example.com", False, False, EmailKind.UNKNOWN),
    ],
)
def test_email_kind(email: str, generic: bool, has_name: bool, expected: EmailKind) -> None:
    assert kind_for(email, generic=generic, has_name=has_name) is expected


@pytest.mark.parametrize(
    ("raw", "limit", "expected"),
    [
        ("  Acme \n\t Dental  ", 50, "Acme Dental"),
        ("A\x00B\x07C\x1bD", 50, "A B C D"),  # control characters never reach the database
        ("x" * 500, 10, "x" * 10),
        ("word word", 5, "word"),  # clipped on the space, then the trailing space is removed
        ("   ", 10, None),
        ("", 10, None),
        (None, 10, None),
    ],
)
def test_clean(raw: str | None, limit: int, expected: str | None) -> None:
    assert clean(raw, limit) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("us", "US"),
        (" gb ", "GB"),
        ("USA", None),
        ("U", None),
        ("1A", None),
        ("", None),
        (None, None),
    ],
)
def test_clean_country(raw: str | None, expected: str | None) -> None:
    assert clean_country(raw) == expected
