"""Verification of Supabase access tokens (JWTs). Every API request goes through :meth:`TokenVerifier.verify`.

What is enforced, and why:

* **Algorithm pinning** — the token's ``alg`` must equal the algorithm of the trusted JWKS key selected by its ``kid``;
  ``none``, symmetric (HS*) and anything not in :data:`~app.auth.jwks.ALLOWED_ALGORITHMS` are rejected before any crypto.
* **Header injection** — ``jku`` / ``jwk`` / ``x5u`` / ``x5c`` headers (attacker-supplied key material or key URLs) are
  rejected outright; keys only ever come from our own fetch of the configured provider.
* **Claims** — ``iss`` and ``aud`` must match; ``exp``/``iat``/``sub`` are required; ``exp``/``nbf``/``iat`` are checked
  against the injected clock (small leeway); ``role`` must be ``authenticated`` (so the public *anon* key and the
  *service-role* key are never accepted as a login); anonymous sessions are rejected.
* **Bounded input** — oversized tokens are refused before parsing.
* **Fail closed** — if signing keys cannot be obtained the request is refused (503), never waved through.

The reason for a rejection is logged server-side; the client only ever sees a generic message.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import jwt
import structlog

from app.auth.claims import Principal
from app.auth.jwks import ALLOWED_ALGORITHMS, JwksCache
from app.core.clock import Clock

log = structlog.get_logger("auth.verifier")

MAX_TOKEN_BYTES = 8_192
_FORBIDDEN_HEADERS = frozenset({"jku", "jwk", "x5u", "x5c"})
_REQUIRED_CLAIMS = ["exp", "iat", "sub", "aud", "iss"]


class InvalidTokenError(Exception):
    """The token is not acceptable. ``reason`` is a short machine slug for logs — never shown to clients."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class VerifierConfig:
    issuer: str
    audience: str = "authenticated"
    leeway_seconds: float = 10.0


def _number(claims: dict[str, Any], name: str) -> float:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidTokenError(f"claim_{name}_not_numeric")
    return float(value)


class TokenVerifier:
    def __init__(self, jwks: JwksCache, config: VerifierConfig, clock: Clock) -> None:
        self._jwks = jwks
        self._config = config
        self._clock = clock

    async def verify(self, token: str) -> Principal:
        if not token or len(token) > MAX_TOKEN_BYTES:
            raise InvalidTokenError("token_size")
        key = await self._select_key(token)
        claims = self._decode(token, key.key, key.algorithm)
        self._check_times(claims)
        return self._principal(claims)

    # ───────────────────────────── steps ─────────────────────────────

    async def _select_key(self, token: str) -> Any:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidTokenError("malformed_token") from exc
        if _FORBIDDEN_HEADERS & header.keys():
            raise InvalidTokenError("forbidden_header")
        alg, kid = header.get("alg"), header.get("kid")
        if alg not in ALLOWED_ALGORITHMS:
            raise InvalidTokenError("algorithm_not_allowed")
        if not isinstance(kid, str) or not kid:
            raise InvalidTokenError("missing_kid")
        key = await self._jwks.get(kid)
        if key is None:
            raise InvalidTokenError("unknown_kid")
        if key.algorithm != alg:
            raise InvalidTokenError(
                "algorithm_mismatch"
            )  # the key set, not the token, decides the algorithm
        return key

    def _decode(self, token: str, key: Any, algorithm: str) -> dict[str, Any]:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                audience=self._config.audience,
                issuer=self._config.issuer,
                options={
                    "require": _REQUIRED_CLAIMS,
                    # Times are checked below against OUR clock (testable, and one place for the leeway).
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                },
            )
        except jwt.InvalidSignatureError as exc:
            raise InvalidTokenError("bad_signature") from exc
        except jwt.InvalidAudienceError as exc:
            raise InvalidTokenError("bad_audience") from exc
        except jwt.InvalidIssuerError as exc:
            raise InvalidTokenError("bad_issuer") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise InvalidTokenError(f"missing_claim_{exc.claim}") from exc
        except jwt.PyJWTError as exc:
            raise InvalidTokenError("invalid_token") from exc
        return claims

    def _check_times(self, claims: dict[str, Any]) -> None:
        now = self._clock.now().timestamp()
        leeway = self._config.leeway_seconds
        if _number(claims, "exp") <= now - leeway:
            raise InvalidTokenError("expired")
        if _number(claims, "iat") > now + leeway:
            raise InvalidTokenError("issued_in_the_future")
        if "nbf" in claims and _number(claims, "nbf") > now + leeway:
            raise InvalidTokenError("not_yet_valid")

    def _principal(self, claims: dict[str, Any]) -> Principal:
        if claims.get("role") != "authenticated":
            raise InvalidTokenError("role_not_authenticated")  # blocks anon / service_role tokens
        if claims.get("is_anonymous") is True:
            raise InvalidTokenError("anonymous_session")
        try:
            user_id = uuid.UUID(str(claims["sub"]))
        except ValueError as exc:
            raise InvalidTokenError("sub_not_uuid") from exc
        email = claims.get("email")
        if not isinstance(email, str) or "@" not in email:
            raise InvalidTokenError("missing_email")
        metadata = claims.get("user_metadata")
        if isinstance(metadata, dict) and metadata.get("email_verified") is False:
            raise InvalidTokenError("email_not_verified")
        aal = claims.get("aal")
        session_id = claims.get("session_id")
        return Principal(
            user_id=user_id,
            email=email.strip().lower(),
            aal=aal if isinstance(aal, str) else "aal1",
            session_id=session_id if isinstance(session_id, str) else None,
            expires_at=datetime.fromtimestamp(_number(claims, "exp"), tz=UTC),
        )
