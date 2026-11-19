from __future__ import annotations

from app.core.redact import scrub_error


def test_known_secrets_are_removed_verbatim_even_without_a_recognisable_shape() -> None:
    odd_secret = "plain-looking-secret-value"
    cleaned = scrub_error(
        f"401 from vendor: invalid credential {odd_secret}", known_secrets=(odd_secret,)
    )
    assert odd_secret not in cleaned
    assert "401 from vendor" in cleaned


def test_empty_known_secrets_are_ignored_rather_than_wiping_the_message() -> None:
    assert scrub_error("hello world", known_secrets=("",)) == "hello world"


def test_a_vendor_that_echoes_a_key_in_json_text_is_scrubbed() -> None:
    key = "gsk_" + "AbCdEf0123456789AbCdEf01"
    cleaned = scrub_error('{"error": {"message": "Invalid API Key: ' + key + '"}}')
    assert key not in cleaned
