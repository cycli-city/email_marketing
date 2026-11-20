"""MailboxValidator — e-mail verification (EMAIL_VERIFICATION capability), the second verifier behind Verifalia.

Verified on the owner's key on 2026-11-20: ``GET /v2/validation/single?key=…&email=…`` answers a flat object with booleans —
``is_syntax``, ``is_domain``, ``is_smtp``, ``is_verified`` (``null`` when the mailbox could not be checked), ``is_server_down``,
``is_greylisted``, ``is_disposable``, ``is_suppressed``, ``is_role``, ``is_high_risk``, ``is_free``. A bad key answers
``401 {"error": {"error_code": 10001, "error_message": "API key not found."}}``. The vendor only accepts the key in the query
string, which is why error text is scrubbed of the real value (``HttpProvider``).

Mapping to a verdict (conservative — anything we cannot be sure of is not "deliverable"):
syntax/domain failure → undeliverable; disposable → risky; ``is_verified`` true → deliverable, false → undeliverable;
``null`` → unknown (greylisted / server down / catch-all style checks cannot confirm a mailbox).
"""

from __future__ import annotations

import json
import re
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

URL = "https://api.mailboxvalidator.com/v2/validation/single"
_AUTH_CODES = {
    10001,
    10002,
    10003,
}  # key not found / disabled / invalid — per the vendor's error list
_CREDIT_CODES = {10004, 10005}
_ERROR_CODE = re.compile(r'"?error_code"?\s*:\s*(\d+)')


class MailboxValidatorProvider(HttpProvider, EmailVerificationProvider):
    def __init__(
        self, *, api_key: SecretStr | None, client: httpx.AsyncClient, clock: Clock
    ) -> None:
        super().__init__(
            client=client, clock=clock, secrets=[api_key.get_secret_value()] if api_key else []
        )
        self._api_key = api_key
        self.spec = ProviderSpec(
            "mailboxvalidator",
            Capability.EMAIL_VERIFICATION,
            "MailboxValidator",
            free_tier=True,
            default_priority=20,
            monthly_limit=300,
            rate_limit_per_minute=60,
            may_receive_personal_data=True,  # an address, by design
        )

    @property
    def configured(self) -> bool:
        return self._api_key is not None and bool(self._api_key.get_secret_value())

    def refine_error(self, status: int, text: str) -> ProviderError | None:
        match = _ERROR_CODE.search(text)
        if match is None:
            return None
        code = int(match.group(1))
        if code in _AUTH_CODES:
            return AuthError("MailboxValidator rejected the API key", http_status=status)
        if code in _CREDIT_CODES:
            return QuotaExhaustedError("MailboxValidator credits are used up", http_status=status)
        return None

    async def verify_email(self, email: str) -> ProviderResult[VerificationOutcome]:
        if self._api_key is None:
            raise self.auth_problem("MAILBOXVALIDATOR_API_KEY is not configured")
        address = email.strip().lower()
        if not ADDRESS_RE.fullmatch(address):
            raise PermanentProviderError("not an e-mail address")
        response = await self.request(
            "GET", URL, params={"key": self._api_key.get_secret_value(), "email": address}
        )
        body = p.mapping(self.json_body(response))
        if "error" in body:  # an error object delivered with a 200 — never mistake it for a verdict
            raise self.error_for(httpx.Response(400, text=json.dumps(body)))
        if "is_syntax" not in body:
            raise InvalidResponseError("MailboxValidator answered without a verdict")
        return ProviderResult(self._outcome(address, body), units_used=Decimal(1))

    @staticmethod
    def _outcome(address: str, body: dict[str, Any]) -> VerificationOutcome:
        verified = p.flag(body.get("is_verified"))
        verdict: Verdict
        if body.get("is_syntax") is False or body.get("is_domain") is False:
            verdict, detail = "undeliverable", "invalid_syntax_or_domain"
        elif body.get("is_disposable") is True:
            verdict, detail = "risky", "disposable"
        elif verified is True:
            verdict, detail = "deliverable", "verified"
        elif verified is False:
            verdict, detail = "undeliverable", "mailbox_not_found"
        else:
            verdict, detail = "unknown", "could_not_verify"
        return VerificationOutcome(
            email=address,
            verdict=verdict,
            detail=detail,
            is_disposable=p.flag(body.get("is_disposable")),
            is_role=p.flag(body.get("is_role")),
            is_free_provider=p.flag(body.get("is_free")),
        )
