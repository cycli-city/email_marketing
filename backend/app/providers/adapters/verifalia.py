"""Verifalia — e-mail verification (EMAIL_VERIFICATION capability). 25 free credits/day, resetting by themselves.

Verified on the owner's account on 2026-11-20: ``POST /v2.6/email-validations`` with ``{"entries":[{"inputData": "…"}],
"quality":"Standard"}`` (HTTP Basic auth) answers **200 with the job already ``Completed``** for a single address (202 if it is
still running); ``GET /v2.6/email-validations/{id}/entries`` then returns ``data[0]`` with ``classification``
(``Deliverable``/``Risky``/``Undeliverable``/``Unknown``), a finer ``status`` (e.g. ``DomainHasNullMx``), ``isRoleAccount`` and
``isFreeEmailAddress``. Bad credentials answer ``401`` (+ ``WWW-Authenticate``).

The address is sent to Verifalia (personal data of a prospect). The owner chose this service for exactly that purpose; the
provider is therefore cleared for e-mail addresses only — nothing else about a prospect is ever sent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
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
    TransientProviderError,
)
from app.providers.http_base import HttpProvider
from app.providers.spec import ProviderResult, ProviderSpec

BASE = "https://api.verifalia.com/v2.6"
_CLASSIFICATION: dict[str, Verdict] = {
    "deliverable": "deliverable",
    "undeliverable": "undeliverable",
    "risky": "risky",
    "unknown": "unknown",
}
MAX_POLLS = 6


class VerifaliaProvider(HttpProvider, EmailVerificationProvider):
    def __init__(
        self,
        *,
        username: SecretStr | None,
        password: SecretStr | None,
        client: httpx.AsyncClient,
        clock: Clock,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        secrets = [s.get_secret_value() for s in (username, password) if s]
        super().__init__(client=client, clock=clock, secrets=secrets)
        self._username, self._password = username, password
        self._sleep = sleep
        self.spec = ProviderSpec(
            "verifalia",
            Capability.EMAIL_VERIFICATION,
            "Verifalia",
            free_tier=True,
            default_priority=10,
            daily_limit=25,
            rate_limit_per_minute=30,
            may_receive_personal_data=True,  # an address, by design (see module docstring)
        )

    @property
    def configured(self) -> bool:
        return bool(
            self._username
            and self._password
            and self._username.get_secret_value()
            and self._password.get_secret_value()
        )

    def refine_error(self, status: int, text: str) -> ProviderError | None:
        if (
            status == 402
            or "insufficient credit" in text.lower()
            or "not enough credit" in text.lower()
        ):
            return QuotaExhaustedError("Verifalia credits are used up", http_status=status)
        return None

    async def verify_email(self, email: str) -> ProviderResult[VerificationOutcome]:
        if not self.configured or self._username is None or self._password is None:
            raise self.auth_problem("Verifalia credentials are not configured")
        address = email.strip().lower()
        if not ADDRESS_RE.fullmatch(address):
            raise PermanentProviderError("not an e-mail address")
        auth = (self._username.get_secret_value(), self._password.get_secret_value())
        submitted = await self.request(
            "POST",
            f"{BASE}/email-validations",
            json={"entries": [{"inputData": address}], "quality": "Standard"},
            auth=auth,
        )
        job = p.mapping(p.mapping(self.json_body(submitted)).get("overview"))
        job_id = p.text(job.get("id"), limit=64)
        if job_id is None:
            raise InvalidResponseError("Verifalia answered without a job id")
        await self._wait_until_complete(job_id, str(job.get("status")), auth)
        entries = await self.request("GET", f"{BASE}/email-validations/{job_id}/entries", auth=auth)
        data = p.sequence(p.mapping(self.json_body(entries)).get("data"))
        if not data:
            raise InvalidResponseError("Verifalia answered without a verdict")
        return ProviderResult(self._outcome(address, p.mapping(data[0])), units_used=Decimal(1))

    async def _wait_until_complete(self, job_id: str, status: str, auth: tuple[str, str]) -> None:
        for _ in range(MAX_POLLS):
            if status == "Completed":
                return
            await self._sleep(1.5)
            overview = await self.request("GET", f"{BASE}/email-validations/{job_id}", auth=auth)
            status = str(
                p.mapping(p.mapping(self.json_body(overview)).get("overview")).get("status")
            )
        raise TransientProviderError("Verifalia did not finish the validation in time")

    @staticmethod
    def _outcome(address: str, entry: dict[str, Any]) -> VerificationOutcome:
        classification = str(entry.get("classification") or "").lower()
        status = p.text(entry.get("status"), limit=100) or classification or "unknown"
        return VerificationOutcome(
            email=address,
            verdict=_CLASSIFICATION.get(classification, "unknown"),
            detail=status,
            is_role=p.flag(entry.get("isRoleAccount")),
            is_free_provider=p.flag(entry.get("isFreeEmailAddress")),
            is_disposable=True if "disposable" in status.lower() else None,
            is_catch_all=True
            if "catchall" in status.lower().replace("-", "").replace("_", "")
            else None,
        )
