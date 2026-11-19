"""Cryptographic helpers: encryption-at-rest for credentials and signed, purpose-bound action tokens."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import jwt
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.clock import SYSTEM_CLOCK, Clock

MIN_SECRET_LENGTH = 32


class CryptoError(Exception):
    """Raised for any encryption/decryption/token failure. Messages never contain secret material."""


class Encryptor:
    """Fernet (AES-128-CBC + HMAC-SHA256) with key rotation via ``MultiFernet``.

    The first key encrypts; every key can decrypt. To rotate: prepend a new key to ``ENCRYPTION_KEY``,
    run ``rotate`` over stored ciphertexts, then drop the old key.
    """

    def __init__(self, keys: Sequence[str]) -> None:
        if not keys:
            raise CryptoError("No encryption key configured (set ENCRYPTION_KEY)")
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])
        except (ValueError, TypeError) as exc:
            raise CryptoError("ENCRYPTION_KEY contains an invalid Fernet key") from exc

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise CryptoError("Could not decrypt value (wrong key or corrupted data)") from exc

    def rotate(self, token: str) -> str:
        try:
            return self._fernet.rotate(token.encode()).decode()
        except InvalidToken as exc:
            raise CryptoError("Could not rotate value (wrong key or corrupted data)") from exc


def generate_secret(nbytes: int = 48) -> str:
    return secrets.token_urlsafe(nbytes)


# ── signed action tokens (unsubscribe links, OAuth state, internal webhooks) ──────────────────────

_ALGORITHM = "HS256"


def create_action_token(
    *,
    secret: str,
    purpose: str,
    subject: str,
    ttl: timedelta | None,
    clock: Clock = SYSTEM_CLOCK,
    claims: dict[str, Any] | None = None,
) -> str:
    """Create an HS256 token bound to a ``purpose`` so a token for one flow can't be replayed in another.

    ``ttl=None`` produces a non-expiring token (used for unsubscribe links, which must keep working).
    """
    if len(secret) < MIN_SECRET_LENGTH:
        raise CryptoError(f"JWT_SECRET must be at least {MIN_SECRET_LENGTH} characters")
    now = clock.now()
    payload: dict[str, Any] = {
        **(claims or {}),
        "sub": subject,
        "pur": purpose,
        "iat": int(now.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    if ttl is not None:
        payload["exp"] = int((now + ttl).timestamp())
    return jwt.encode(payload, secret, algorithm=_ALGORITHM)


def verify_action_token(
    *,
    secret: str,
    token: str,
    purpose: str,
    clock: Clock = SYSTEM_CLOCK,
) -> dict[str, Any]:
    """Verify signature, purpose and (if present) expiry. Raises :class:`CryptoError` on any failure."""
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            options={"require": ["sub", "pur", "iat"], "verify_exp": False, "verify_iat": False},
        )
    except jwt.PyJWTError as exc:
        raise CryptoError("Invalid token") from exc
    if payload.get("pur") != purpose:
        raise CryptoError("Token was issued for a different purpose")
    exp = payload.get("exp")
    if exp is not None and datetime.fromtimestamp(float(exp), tz=clock.now().tzinfo) <= clock.now():
        raise CryptoError("Token expired")
    return payload
