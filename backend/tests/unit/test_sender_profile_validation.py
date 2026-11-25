"""What may be saved as a sender profile: it ends up in every message's footer, so it is cleaned and bounded."""

from __future__ import annotations

import pytest

from app.services.outreach.profile import (
    MAX_ADDRESS,
    MAX_NAME,
    MAX_SIGNATURE,
    ProfileError,
    ProfileInput,
    validated,
)


def test_a_complete_profile_is_accepted_and_normalised() -> None:
    clean = validated(
        ProfileInput(
            display_name="  Alex   Founder ",
            postal_address=" 1 Example Street \r\n\r\n  Example City  \n",
            reply_to=" Founder@Example.COM ",
            signature_text="Alex\nNEXORA",
            website="https://www.Example.com/about?x=1",
        )
    )
    assert clean.display_name == "Alex Founder"
    assert clean.postal_address == "1 Example Street\nExample City"
    assert clean.reply_to == "founder@example.com"
    assert clean.signature_text == "Alex\nNEXORA"
    assert clean.website == "https://example.com"


def test_only_the_name_is_required() -> None:
    clean = validated(ProfileInput(display_name="Jo Applicant"))
    assert (clean.postal_address, clean.reply_to, clean.signature_text, clean.website) == (
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize("name", ["", " ", "A", "\n\t "])
def test_a_missing_or_one_letter_name_is_refused(name: str) -> None:
    with pytest.raises(ProfileError) as error:
        validated(ProfileInput(display_name=name))
    assert error.value.field == "display_name"


def test_control_characters_never_reach_the_footer() -> None:
    clean = validated(
        ProfileInput(
            display_name="Alex\x00 Fou\x07nder\r\nBcc: x@example.com",
            postal_address="1 Main\x1b St\x00",
        )
    )
    assert "\x00" not in clean.display_name
    assert "\n" not in clean.display_name  # a name can never smuggle a second header line
    assert clean.postal_address == "1 Main St"


@pytest.mark.parametrize(
    ("field", "kwargs", "limit"),
    [
        ("display_name", {"display_name": "x" * (MAX_NAME + 1)}, MAX_NAME),
        (
            "postal_address",
            {"display_name": "Alex", "postal_address": "x" * (MAX_ADDRESS + 1)},
            MAX_ADDRESS,
        ),
        (
            "signature_text",
            {"display_name": "Alex", "signature_text": "x" * (MAX_SIGNATURE + 1)},
            MAX_SIGNATURE,
        ),
    ],
)
def test_overlong_text_is_refused(field: str, kwargs: dict[str, str], limit: int) -> None:
    with pytest.raises(ProfileError, match=str(limit)) as error:
        validated(ProfileInput(**kwargs))
    assert error.value.field == field


@pytest.mark.parametrize("name_length", [MAX_NAME])
def test_text_exactly_at_the_limit_is_accepted(name_length: int) -> None:
    assert validated(ProfileInput(display_name="x" * name_length)).display_name == "x" * name_length


@pytest.mark.parametrize(
    "reply_to", ["not-an-address", "a@b", "two@@example.com", "x@example.com\r\nBcc: y@example.com"]
)
def test_a_bad_reply_to_is_refused(reply_to: str) -> None:
    with pytest.raises(ProfileError) as error:
        validated(ProfileInput(display_name="Alex", reply_to=reply_to))
    assert error.value.field == "reply_to"


@pytest.mark.parametrize("website", ["not a url", "http://127.0.0.1/", "localhost", "https://"])
def test_a_bad_website_is_refused(website: str) -> None:
    with pytest.raises(ProfileError) as error:
        validated(ProfileInput(display_name="Alex", website=website))
    assert error.value.field == "website"


@pytest.mark.parametrize("empty", [None, "", "   ", "\n"])
def test_empty_optional_fields_mean_none(empty: str | None) -> None:
    clean = validated(
        ProfileInput(
            display_name="Alex",
            postal_address=empty,
            reply_to=empty,
            signature_text=empty,
            website=empty,
        )
    )
    assert (clean.postal_address, clean.reply_to, clean.signature_text, clean.website) == (
        None,
        None,
        None,
        None,
    )
