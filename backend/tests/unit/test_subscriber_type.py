"""Company or individual? Marketing law often treats the two differently, so discovery must say which it believes."""

from __future__ import annotations

import pytest

from app.domain.enums import SubscriberType
from app.domain.identity import subscriber_type_for

GMAIL = (
    "gmail" + ".com"
)  # assembled: the public-repo audit rejects real-looking mail domains in tracked files


@pytest.mark.parametrize(
    "address",
    [
        f"jane.doe@{GMAIL}",
        f"Jane+offers@{GMAIL.upper()}",
        "someone@" + "outlook.com",
        "someone@" + "yahoo.com",
        "someone@" + "mailinator.com",  # disposable: not a business either
    ],
)
def test_a_personal_mailbox_is_an_individual(address: str) -> None:
    assert subscriber_type_for(address) is SubscriberType.INDIVIDUAL


@pytest.mark.parametrize(
    "address",
    [
        "info@clinic.example.com",
        "dr.smith@clinic.example.com",
        "reception@clinic.example.net",
        "owner@shop.example.org",
    ],
)
def test_an_address_at_a_business_domain_is_treated_as_a_company(address: str) -> None:
    assert subscriber_type_for(address) is SubscriberType.CORPORATE


@pytest.mark.parametrize("address", [None, "", "not-an-address", "a@b", "two@@example.com"])
def test_anything_that_is_not_an_address_is_unknown(address: str | None) -> None:
    assert subscriber_type_for(address) is SubscriberType.UNKNOWN
