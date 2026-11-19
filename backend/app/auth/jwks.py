"""Cache of the identity provider's public signing keys (Supabase publishes them at ``/.well-known/jwks.json``).

Security properties:

* Only asymmetric algorithms are ever accepted (``ES256`` / ``RS256``) and the algorithm of a key comes from the
  *key set* — a trusted source — never from the token being verified. That is what stops algorithm-confusion attacks
  (``alg: none``, or HS256 "signed" with the public key as the secret).
* The key set is refetched on a TTL and when an unknown ``kid`` shows up (key rotation), but **at most once per
  ``min_refetch_seconds``** — a flood of forged tokens with random ``kid`` values cannot become a flood of outbound requests.
* If the provider is unreachable we keep verifying with the cached keys for up to ``max_stale_seconds`` (so a brief
  outage does not lock the owner out); with no usable keys we fail **closed**.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import jwt
import structlog

from app.core.clock import Clock
from app.core.http import is_loopback_http

log = structlog.get_logger("auth.jwks")

ALLOWED_ALGORITHMS = frozenset({"ES256", "RS256"})


class JwksUnavailableError(Exception):
    """No usable signing keys and the provider cannot be reached. Callers fail closed (HTTP 503)."""


@dataclass(frozen=True, slots=True)
class SigningKey:
    kid: str
    algorithm: str
    key: Any  # a cryptography public-key object


def parse_jwks(payload: Any) -> dict[str, SigningKey]:
    """Keep only well-formed signature-verification keys using an allowed algorithm; skip everything else."""
    keys: dict[str, SigningKey] = {}
    entries = payload.get("keys") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return keys
    for jwk in entries:
        if not isinstance(jwk, dict):
            continue
        kid, kty, alg = jwk.get("kid"), jwk.get("kty"), jwk.get("alg")
        if not isinstance(kid, str) or alg not in ALLOWED_ALGORITHMS:
            continue
        if jwk.get("use") not in (None, "sig"):
            continue
        ops = jwk.get("key_ops")
        if ops is not None and "verify" not in ops:
            continue
        consistent = (alg == "ES256" and kty == "EC" and jwk.get("crv") == "P-256") or (
            alg == "RS256" and kty == "RSA"
        )
        if not consistent:
            continue
        try:
            keys[kid] = SigningKey(kid, alg, jwt.PyJWK.from_dict(jwk, algorithm=alg).key)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            log.warning("jwks.bad_key_skipped", kid=kid)
    return keys


class JwksCache:
    def __init__(
        self,
        url: str,
        client: httpx.AsyncClient,
        clock: Clock,
        *,
        ttl_seconds: float = 600.0,
        min_refetch_seconds: float = 30.0,
        max_stale_seconds: float = 86_400.0,
        allow_loopback_http: bool = False,
    ) -> None:
        if not (url.startswith("https://") or (allow_loopback_http and is_loopback_http(url))):
            raise ValueError("JWKS URL must be HTTPS")
        self._url = url
        self._client = client
        self._clock = clock
        self._ttl = ttl_seconds
        self._min_refetch = min_refetch_seconds
        self._max_stale = max_stale_seconds
        self._keys: dict[str, SigningKey] = {}
        self._fetched_at: datetime | None = None
        self._last_attempt: datetime | None = None
        self._lock = asyncio.Lock()

    async def get(self, kid: str) -> SigningKey | None:
        """The key for ``kid``, or ``None`` if the provider does not publish it."""
        now = self._clock.now()
        key = self._keys.get(kid)
        if key is not None and not self._expired(now):
            return key  # hot path: no lock, no I/O
        async with self._lock:
            now = self._clock.now()
            key = self._keys.get(kid)
            if key is not None and not self._expired(now):
                return key
            if self._may_attempt(now):
                await self._refresh(now)
            elif not self._keys:
                raise JwksUnavailableError("signing keys unavailable (recent fetch failed)")
            return self._keys.get(kid)

    # ───────────────────────────── internals ─────────────────────────────

    def _expired(self, now: datetime) -> bool:
        return self._fetched_at is None or (now - self._fetched_at).total_seconds() >= self._ttl

    def _may_attempt(self, now: datetime) -> bool:
        return (
            self._last_attempt is None
            or (now - self._last_attempt).total_seconds() >= self._min_refetch
        )

    async def _refresh(self, now: datetime) -> None:
        self._last_attempt = now
        try:
            response = await self._client.get(self._url)
            if response.status_code != 200:
                raise httpx.HTTPStatusError(
                    "unexpected status", request=response.request, response=response
                )
            fresh = parse_jwks(response.json())
            if not fresh:
                raise ValueError("no usable signing keys in the key set")
        except (httpx.HTTPError, ValueError) as exc:
            usable = (
                self._fetched_at is not None
                and (now - self._fetched_at).total_seconds() <= self._max_stale
            )
            log.warning("jwks.refresh_failed", error=type(exc).__name__, serving_stale=usable)
            if not usable:
                raise JwksUnavailableError("cannot fetch signing keys") from exc
            return
        self._keys, self._fetched_at = fresh, now
        log.info("jwks.refreshed", keys=len(fresh))
