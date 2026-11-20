"""Abstract API — e-mail verification (EMAIL_VERIFICATION capability), via its **Email Reputation** API.

Abstract renamed its "Email Validation" product to "Email Reputation" (the owner's dashboard calls it "Email Intelligence"); the
old ``emailvalidation.abstractapi.com`` host rejects the new keys with ``401 Invalid API key``. Verified on the owner's key on
2026-11-20: ``GET https://emailreputation.abstractapi.com/v1/?api_key=…&email=…`` answers a nested object —
``email_deliverability.{status, status_detail, is_format_valid, is_smtp_valid, is_mx_valid}``,
``email_quality.{score, is_free_email, is_disposable, is_catchall, is_role, is_username_suspicious}``, ``email_risk``,
plus domain details and a list of data breaches. **Only the deliverability verdict and the quality flags are kept**: the breach
history and sender details are information about a person this system has no use for and must not store. A malformed address is
answered with HTTP 200 and ``is_format_valid: false`` (not an error); a bad key is ``401``. The key is only accepted in the
query string (hence error scrubbing and the pinned HTTP-client loggers, ADR-039).

Free tier: about 100 requests/month (docs/PROVIDERS.md §3).
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
    InvalidResponseError,
    PermanentProviderError,
    ProviderError,
    QuotaExhaustedError,
)
from app.providers.http_base import HttpProvider
from app.providers.spec import ProviderResult, ProviderSpec

URL = "https://emailreputation.abstractapi.com/v1/"
_STATUS: dict[str, Verdict] = {
    "deliverable": "deliverable",
    "undeliverable": "undeliverable",
    "risky": "risky",
    "unknown": "unknown",
}


class AbstractEmailProvider(HttpProvider, EmailVerificationProvider):
    def __init__(
        self, *, api_key: SecretStr | None, client: httpx.AsyncClient, clock: Clock
    ) -> None:
        super().__init__(
            client=client, clock=clock, secrets=[api_key.get_secret_value()] if api_key else []
        )
        self._api_key = api_key
        self.spec = ProviderSpec(
            "abstract",
            Capability.EMAIL_VERIFICATION,
            "Abstract Email Reputation",
            free_tier=True,
            default_priority=40,
            monthly_limit=100,
            rate_limit_per_minute=60,
            may_receive_personal_data=True,  # an address, by design
        )

    @property
    def configured(self) -> bool:
        return self._api_key is not None and bool(self._api_key.get_secret_value())

    def refine_error(self, status: int, text: str) -> ProviderError | None:
        lowered = text.lower()
        if "quota" in lowered or "limit_reached" in lowered or "out of credits" in lowered:
            return QuotaExhaustedError("Abstract's plan allowance is used up", http_status=status)
        return None

    async def verify_email(self, email: str) -> ProviderResult[VerificationOutcome]:
        if self._api_key is None:
            raise self.auth_problem("ABSTRACT_EMAIL_API_KEY is not configured")
        address = email.strip().lower()
        if not ADDRESS_RE.fullmatch(address):
            raise PermanentProviderError("not an e-mail address")
        response = await self.request(
            "GET", URL, params={"api_key": self._api_key.get_secret_value(), "email": address}
        )
        body = p.mapping(self.json_body(response))
        deliverability = p.mapping(body.get("email_deliverability"))
        if "status" not in deliverability:
            raise InvalidResponseError("Abstract answered without a deliverability verdict")
        return ProviderResult(
            self._outcome(address, deliverability, p.mapping(body.get("email_quality"))),
            units_used=Decimal(1),
        )

    @staticmethod
    def _outcome(
        address: str, deliverability: dict[str, Any], quality: dict[str, Any]
    ) -> VerificationOutcome:
        verdict = _STATUS.get(str(deliverability.get("status") or "").lower(), "unknown")
        catch_all = p.flag(quality.get("is_catchall"))
        disposable = p.flag(quality.get("is_disposable"))
        if verdict == "deliverable" and (catch_all or disposable):
            verdict = (
                "risky"  # the mailbox cannot be confirmed (catch-all) or is throw-away (disposable)
            )
        return VerificationOutcome(
            email=address,
            verdict=verdict,
            detail=p.text(deliverability.get("status_detail"), limit=100)
            or str(deliverability.get("status")),
            is_disposable=disposable,
            is_role=p.flag(quality.get("is_role")),
            is_catch_all=catch_all,
            is_free_provider=p.flag(quality.get("is_free_email")),
        )
