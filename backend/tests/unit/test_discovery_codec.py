"""What the ledger stores must come back identical — and damaged or older data must never crash a run."""

from __future__ import annotations

import json

from app.providers.discovery import ContactHit, DomainContacts, SearchHit, VerificationOutcome
from app.services.discovery.codec import (
    decode_domain_contacts,
    decode_hits,
    decode_outcome,
    encode_domain_contacts,
    encode_hits,
    encode_outcome,
)


def through_json(data: dict[str, object]) -> dict[str, object]:
    """The ledger stores JSONB: prove the encoded form survives a real JSON round trip."""
    loaded: dict[str, object] = json.loads(json.dumps(data))
    return loaded


def test_search_hits_round_trip() -> None:
    hits = [
        SearchHit("Smile Dental", "https://smiledental.com", "Family dentist"),
        SearchHit("", "https://x.com/a", ""),
    ]
    assert decode_hits(through_json(encode_hits(hits))) == hits


def test_search_hits_without_a_url_are_dropped() -> None:
    assert decode_hits(
        {"hits": [{"title": "t", "snippet": "s"}, {"url": "https://a.com"}, "junk", 3]}
    ) == [SearchHit("", "https://a.com", "")]


def test_domain_contacts_round_trip() -> None:
    found = DomainContacts(
        domain="clinic.example.com",
        organization="Clinic",
        pattern="{first}.{last}",
        accept_all=True,
        contacts=(
            ContactHit(
                email="jane@clinic.example.com",
                type="personal",
                first_name="Jane",
                last_name="Doe",
                position="Owner",
                department="management",
                seniority="executive",
                confidence=91,
                source_urls=("https://clinic.example.com/team",),
                vendor_verified=True,
            ),
            ContactHit(
                "info@clinic.example.com", "generic", None, None, None, None, None, None, (), False
            ),
        ),
    )
    assert decode_domain_contacts(through_json(encode_domain_contacts(found))) == found


def test_domain_contacts_decoder_survives_garbage() -> None:
    decoded = decode_domain_contacts(
        {
            "domain": 5,
            "accept_all": "yes",
            "contacts": [
                {
                    "email": "a@clinic.example.com",
                    "type": "weird",
                    "confidence": True,
                    "source_urls": [1, "https://u.example.com"],
                },
                {"email": ""},
                {"no_email": 1},
                "junk",
            ],
        }
    )
    assert decoded.domain == ""
    assert decoded.accept_all is False  # only a real boolean counts
    assert len(decoded.contacts) == 1
    contact = decoded.contacts[0]
    assert (
        contact.type == "personal"
    )  # unknown kinds are treated as a named person, the cautious reading
    assert contact.confidence is None  # a bool is not a score
    assert contact.source_urls == ("https://u.example.com",)
    assert decode_domain_contacts({}) == DomainContacts("", None, None, False, ())


def test_verification_outcome_round_trip() -> None:
    outcome = VerificationOutcome(
        "a@clinic.example.com",
        "risky",
        "accept_all",
        is_disposable=False,
        is_role=True,
        is_catch_all=True,
        is_free_provider=None,
    )
    assert decode_outcome(through_json(encode_outcome(outcome))) == outcome


def test_an_unrecognised_verdict_decodes_as_unknown_never_as_deliverable() -> None:
    assert (
        decode_outcome({"email": "a@clinic.example.com", "verdict": "perfect"}).verdict == "unknown"
    )
    assert decode_outcome({}).verdict == "unknown"
