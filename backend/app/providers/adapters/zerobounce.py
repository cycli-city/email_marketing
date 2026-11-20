"""ZeroBounce — e-mail verification (EMAIL_VERIFICATION capability). Free tier: 100 credits/month (docs/PROVIDERS.md §3).

Verified on the owner's account on 2026-11-20:

* ``GET /v2/validate?api_key=…&email=…&ip_address=`` answers a flat object: ``status`` (``valid``, ``invalid``, ``catch-all``,
  ``unknown``, ``spamtrap``, ``abuse``, ``do_not_mail``), ``sub_status`` (``does_not_accept_mail``, ``role_based``,
  ``disposable``, …), ``free_email``, ``mx_found``. It also returns personal fields (name, gender, location) which this adapter
  **drops** — nothing but the verdict is kept.
* **Failures arrive as HTTP 200** with ``{"error": "Invalid API key or your account ran out of credits"}`` — a bad key and an
  empty account are indistinguishable there. The free ``GET /v2/getcredits`` tells them apart: ``{"Credits": "-1"}`` means the
  key is invalid, ``"0"`` means no credits left. So an error body is resolved with that call before anything is raised.
* The key is only accepted in the query string (hence error scrubbing and the pinned HTTP-client loggers, ADR-039).

Mapping is conservative: only ``valid`` is deliverable; catch-all and role-based are *risky* (the mailbox may exist, but
verification cannot confirm it); disposable, spam traps, abuse and everything else ZeroBounce refuses are undeliverable.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
from pydantic import SecretStr

from app.core.clock import Clock
from app.domain.enums import Capability
from app.integrations.gmail.mime import ADDRESS_RE
from app.providers.adapters import _parse as p
from app.providers.discovery import EmailVerificationProvider, Verdict, VerificationOutcome
from app.providers.errors import (
    AuthError,
    InvalidResponseError,
    PermanentProviderError,
    ProviderError,
    QuotaExhaustedError,
)
from app.providers.http_base import HttpProvider
from app.providers.spec import ProviderResult, ProviderSpec

BASE = "https://api.zerobounce.net/v2"


class ZeroBounceProvider(HttpProvider, EmailVerificationProvider):
    def __init__(
        self, *, api_key: SecretStr | None, client: httpx.AsyncClient, clock: Clock
    ) -> None:
        super().__init__(
            client=client, clock=clock, secrets=[api_key.get_secret_value()] if api_key else []
        )
        self._api_key = api_key
        self.spec = ProviderSpec(
            "zerobounce",
            Capability.EMAIL_VERIFICATION,
            "ZeroBounce",
            free_tier=True,
            default_priority=30,
            monthly_limit=100,
            rate_limit_per_minute=60,
            may_receive_personal_data=True,  # an address, by design
        )

    @property
    def configured(self) -> bool:
        return self._api_key is not None and bool(self._api_key.get_secret_value())

    async def verify_email(self, email: str) -> ProviderResult[VerificationOutcome]:
        if self._api_key is None:
            raise self.auth_problem("ZEROBOUNCE_API_KEY is not configured")
        address = email.strip().lower()
        if not ADDRESS_RE.fullmatch(address):
            raise PermanentProviderError("not an e-mail address")
        response = await self.request(
            "GET",
            f"{BASE}/validate",
            params={
                "api_key": self._api_key.get_secret_value(),
                "email": address,
                "ip_address": "",
            },
        )
        body = p.mapping(self.json_body(response))
        if "error" in body:
            raise await self._explain(str(body["error"]))
        if "status" not in body:
            raise InvalidResponseError("ZeroBounce answered without a verdict")
        return ProviderResult(self._outcome(address, body), units_used=Decimal(1))

    async def credits(self) -> int:
        """Credits left (free call). ``-1`` is how ZeroBounce reports an invalid key."""
        if self._api_key is None:
            raise self.auth_problem("ZEROBOUNCE_API_KEY is not configured")
        response = await self.request(
            "GET", f"{BASE}/getcredits", params={"api_key": self._api_key.get_secret_value()}
        )
        value = p.mapping(self.json_body(response)).get("Credits")
        try:
            return int(str(value))
        except ValueError as exc:
            raise InvalidResponseError(
                "ZeroBounce answered the credit check with something unusable"
            ) from exc

    async def _explain(self, message: str) -> ProviderError:
        """An error delivered with HTTP 200: decide between 'bad key' and 'no credits' with the free credit check."""
        try:
            remaining = await self.credits()
        except ProviderError:
            return PermanentProviderError(f"ZeroBounce refused the request: {message[:200]}")
        if remaining < 0:
            return AuthError("ZeroBounce rejected the API key")
        if remaining == 0:
            return QuotaExhaustedError("ZeroBounce credits are used up")
        return PermanentProviderError(f"ZeroBounce refused the request: {message[:200]}")

    @staticmethod
    def _outcome(address: str, body: dict[str, Any]) -> VerificationOutcome:
        status = str(body.get("status") or "").lower()
        sub = str(body.get("sub_status") or "").lower()
        verdict: Verdict
        if status == "valid":
            verdict = "deliverable"
        elif status == "catch-all":
            verdict = "risky"
        elif status == "do_not_mail" and sub.startswith("role_based"):
            verdict = "risky"  # a shared mailbox: it exists, whether to use it is the compliance gate's decision
        elif status in {"invalid", "spamtrap", "abuse", "do_not_mail"}:
            verdict = "undeliverable"
        else:
            verdict = "unknown"
        return VerificationOutcome(
            email=address,
            verdict=verdict,
            detail=f"{status}/{sub}" if sub else status or "unknown",
            is_disposable=True if sub == "disposable" else None,
            is_role=True if sub.startswith("role_based") else None,
            is_catch_all=True if status == "catch-all" or sub.endswith("catch_all") else None,
            is_free_provider=p.flag(body.get("free_email")),
        )
