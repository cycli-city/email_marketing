"""A fake identity provider for tests: real EC/RSA keys, real signed JWTs, a JWKS endpoint over ``httpx.MockTransport``.

Nothing about the cryptography is mocked — the verifier under test checks genuine ES256 signatures; only the network
is faked.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

SUPABASE_URL = "https://abcdefghij.supabase.co"
ISSUER = f"{SUPABASE_URL}/auth/v1"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
OWNER_EMAIL = "owner@example.com"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class FakeIdp:
    """Signs tokens with its current EC P-256 key and serves a matching JWKS."""

    def __init__(self, kid: str = "key-1") -> None:
        self.kid = kid
        self._keys: dict[str, ec.EllipticCurvePrivateKey] = {
            kid: ec.generate_private_key(ec.SECP256R1())
        }
        self.fetches = 0
        self.outage = False
        self.extra_jwks: list[dict[str, Any]] = []

    # ───────────────────────────── keys ─────────────────────────────

    def add_key(self, kid: str) -> None:
        self._keys[kid] = ec.generate_private_key(ec.SECP256R1())

    def private_key(self, kid: str | None = None) -> ec.EllipticCurvePrivateKey:
        return self._keys[kid or self.kid]

    def public_pem(self, kid: str | None = None) -> bytes:
        return (
            self.private_key(kid)
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )

    def jwks(self, kids: list[str] | None = None) -> dict[str, Any]:
        keys: list[dict[str, Any]] = []
        for kid in kids or list(self._keys):
            jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self._keys[kid].public_key()))
            keys.append({**jwk, "kid": kid, "alg": "ES256", "use": "sig", "key_ops": ["verify"]})
        return {"keys": keys + self.extra_jwks}

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) != JWKS_URL:
                return httpx.Response(404)
            self.fetches += 1
            if self.outage:
                return httpx.Response(503, text="down")
            return httpx.Response(200, json=self.jwks())

        return httpx.MockTransport(handler)

    # ───────────────────────────── tokens ─────────────────────────────

    def claims(self, now: datetime, **overrides: Any) -> dict[str, Any]:
        ts = int(now.timestamp())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": "authenticated",
            "sub": str(uuid.UUID(int=1)),
            "iat": ts,
            "exp": ts + 3600,
            "email": OWNER_EMAIL,
            "role": "authenticated",
            "aal": "aal1",
            "session_id": "11111111-1111-1111-1111-111111111111",
            "is_anonymous": False,
            "user_metadata": {"email_verified": True},
        }
        claims.update(overrides)
        return {k: v for k, v in claims.items() if v is not _DROP}

    def token(
        self,
        now: datetime,
        *,
        kid: str | None = None,
        headers: dict[str, Any] | None = None,
        key: Any = None,
        algorithm: str = "ES256",
        **overrides: Any,
    ) -> str:
        use_kid = kid or self.kid
        signing_key = key if key is not None else self._keys.get(use_kid, self.private_key())
        return jwt.encode(
            self.claims(now, **overrides),
            signing_key,
            algorithm=algorithm,
            headers={"kid": use_kid, **(headers or {})},
        )


_DROP = object()
DROP: Any = _DROP  # pass as an override value to remove a claim entirely


def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def unsigned_token(claims: dict[str, Any], kid: str) -> str:
    """``alg: none`` — the classic bypass attempt."""
    header = b64url(json.dumps({"alg": "none", "typ": "JWT", "kid": kid}).encode())
    return f"{header}.{b64url(json.dumps(claims).encode())}."


def hs256_with_public_key_as_secret(claims: dict[str, Any], kid: str, public_pem: bytes) -> str:
    """The algorithm-confusion attack: sign with HMAC using the (public) verification key as the secret.

    PyJWT refuses to build this, so it is assembled by hand.
    """
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}).encode())
    payload = b64url(json.dumps(claims).encode())
    signature = hmac.new(public_pem, f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{b64url(signature)}"
