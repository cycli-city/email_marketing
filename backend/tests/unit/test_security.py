from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.clock import FakeClock
from app.core.security import (
    CryptoError,
    Encryptor,
    create_action_token,
    generate_secret,
    verify_action_token,
)

SECRET = "s" * 48


class TestEncryptor:
    def test_round_trip(self) -> None:
        enc = Encryptor([Encryptor.generate_key()])
        token = enc.encrypt("refresh-token-value")
        assert token != "refresh-token-value"
        assert "refresh-token-value" not in token
        assert enc.decrypt(token) == "refresh-token-value"

    def test_ciphertext_is_non_deterministic(self) -> None:
        enc = Encryptor([Encryptor.generate_key()])
        assert enc.encrypt("x") != enc.encrypt("x")

    def test_wrong_key_cannot_decrypt(self) -> None:
        token = Encryptor([Encryptor.generate_key()]).encrypt("secret")
        with pytest.raises(CryptoError):
            Encryptor([Encryptor.generate_key()]).decrypt(token)

    def test_tampered_ciphertext_is_rejected(self) -> None:
        enc = Encryptor([Encryptor.generate_key()])
        token = enc.encrypt("secret")
        tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        with pytest.raises(CryptoError):
            enc.decrypt(tampered)

    def test_key_rotation_keeps_old_data_readable_and_reencrypts(self) -> None:
        old, new = Encryptor.generate_key(), Encryptor.generate_key()
        token_old = Encryptor([old]).encrypt("value")
        rotating = Encryptor([new, old])  # new key first: encrypts with new, decrypts with both
        assert rotating.decrypt(token_old) == "value"
        rotated = rotating.rotate(token_old)
        assert Encryptor([new]).decrypt(rotated) == "value"  # old key no longer needed

    def test_requires_a_key(self) -> None:
        with pytest.raises(CryptoError):
            Encryptor([])

    def test_invalid_key_is_rejected_without_leaking_it(self) -> None:
        with pytest.raises(CryptoError) as exc:
            Encryptor(["definitely-not-valid"])
        assert "definitely-not-valid" not in str(exc.value)


class TestActionTokens:
    def test_round_trip_carries_subject_and_claims(self) -> None:
        clock = FakeClock()
        token = create_action_token(
            secret=SECRET,
            purpose="unsubscribe",
            subject="contact-123",
            ttl=None,
            clock=clock,
            claims={"ws": "w1"},
        )
        payload = verify_action_token(
            secret=SECRET, token=token, purpose="unsubscribe", clock=clock
        )
        assert payload["sub"] == "contact-123"
        assert payload["ws"] == "w1"

    def test_token_for_other_purpose_is_rejected(self) -> None:
        token = create_action_token(secret=SECRET, purpose="oauth_state", subject="x", ttl=None)
        with pytest.raises(CryptoError, match="different purpose"):
            verify_action_token(secret=SECRET, token=token, purpose="unsubscribe")

    def test_wrong_secret_is_rejected(self) -> None:
        token = create_action_token(secret=SECRET, purpose="p", subject="x", ttl=None)
        with pytest.raises(CryptoError):
            verify_action_token(secret="t" * 48, token=token, purpose="p")

    def test_expiry_is_enforced_with_injected_clock(self) -> None:
        clock = FakeClock()
        token = create_action_token(
            secret=SECRET,
            purpose="oauth_state",
            subject="x",
            ttl=timedelta(minutes=10),
            clock=clock,
        )
        clock.advance(minutes=9)
        verify_action_token(secret=SECRET, token=token, purpose="oauth_state", clock=clock)
        clock.advance(minutes=2)
        with pytest.raises(CryptoError, match="expired"):
            verify_action_token(secret=SECRET, token=token, purpose="oauth_state", clock=clock)

    def test_non_expiring_token_stays_valid(self) -> None:
        clock = FakeClock()
        token = create_action_token(
            secret=SECRET, purpose="unsubscribe", subject="x", ttl=None, clock=clock
        )
        clock.advance(days=3650)
        assert verify_action_token(secret=SECRET, token=token, purpose="unsubscribe", clock=clock)

    def test_garbage_and_alg_none_are_rejected(self) -> None:
        with pytest.raises(CryptoError):
            verify_action_token(secret=SECRET, token="garbage", purpose="p")
        # unsigned "alg: none" token must never validate
        unsigned = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJ4IiwicHVyIjoicCIsImlhdCI6MX0."
        with pytest.raises(CryptoError):
            verify_action_token(secret=SECRET, token=unsigned, purpose="p")

    def test_short_secret_is_refused(self) -> None:
        with pytest.raises(CryptoError, match="at least"):
            create_action_token(secret="short", purpose="p", subject="x", ttl=None)

    def test_generate_secret_is_long_and_unique(self) -> None:
        a, b = generate_secret(), generate_secret()
        assert a != b
        assert len(a) >= 32
