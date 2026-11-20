"""Hunter.io — publicly sourced contacts at a domain (contact discovery capability).

Verified on the owner's key on 2026-11-20: ``GET /v2/domain-search?domain=…&limit=…`` (key in the ``X-API-KEY`` header, so it
never appears in a URL) answers ``{"data": {"domain", "organization", "pattern", "accept_all", "emails": [...]}}``; each email has
``value``, ``type`` (``personal``/``generic``), ``confidence``, ``first_name``, ``last_name``, ``position``, ``seniority``,
``department``, ``sources[]`` (public pages: ``uri``) and ``verification.{status,date}``. The credits a call used arrive in
the ``x-credits-used`` header. Errors: ``401 {"errors":[{"id":"authentication_failed",…}]}``.

Hunter also returns LinkedIn/Twitter handles and phone numbers; this adapter deliberately drops them (no LinkedIn use, and
nothing the outreach needs). Free tier: 50 credits/month, 1 credit per call (docs/PROVIDERS.md §2).
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from pydantic import SecretStr

from app.core.clock import Clock
from app.domain.enums import Capability
from app.providers.adapters import _parse as p
from app.providers.discovery import (
    ContactDiscoveryProvider,
    ContactHit,
    ContactType,
    DomainContacts,
)
from app.providers.errors import (
    InvalidResponseError,
    PermanentProviderError,
    ProviderError,
    QuotaExhaustedError,
)
from app.providers.http_base import HttpProvider
from app.providers.spec import ProviderResult, ProviderSpec

URL = "https://api.hunter.io/v2/domain-search"
MAX_LIMIT = 10  # the free plan returns at most 10 addresses per call
_DOMAIN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


class HunterProvider(HttpProvider, ContactDiscoveryProvider):
    def __init__(
        self, *, api_key: SecretStr | None, client: httpx.AsyncClient, clock: Clock
    ) -> None:
        super().__init__(
            client=client, clock=clock, secrets=[api_key.get_secret_value()] if api_key else []
        )
        self._api_key = api_key
        self.spec = ProviderSpec(
            "hunter",
            Capability.CONTACT_DISCOVERY,
            "Hunter.io",
            free_tier=True,
            default_priority=10,
            monthly_limit=50,
            rate_limit_per_minute=60,
        )

    @property
    def configured(self) -> bool:
        return self._api_key is not None and bool(self._api_key.get_secret_value())

    def refine_error(self, status: int, text: str) -> ProviderError | None:
        lowered = text.lower()
        if status in (402, 403, 429) and (
            "usage_limit" in lowered or "limit_reached" in lowered or "monthly" in lowered
        ):
            return QuotaExhaustedError("Hunter's monthly credits are used up", http_status=status)
        if status == 400:
            return PermanentProviderError(f"Hunter rejected the request: {text}", http_status=400)
        return None

    async def domain_contacts(
        self, domain: str, *, limit: int = 10
    ) -> ProviderResult[DomainContacts]:
        if self._api_key is None:
            raise self.auth_problem("HUNTER_API_KEY is not configured")
        clean = domain.strip().lower()
        if not _DOMAIN.fullmatch(clean):
            raise PermanentProviderError("not a domain name")
        response = await self.request(
            "GET",
            URL,
            params={"domain": clean, "limit": max(1, min(limit, MAX_LIMIT))},
            headers={"X-API-KEY": self._api_key.get_secret_value()},
        )
        data = p.mapping(self.json_body(response)).get("data")
        if not isinstance(data, dict):
            raise InvalidResponseError("Hunter answered without a data object")
        contacts = tuple(c for c in (self._contact(e) for e in p.sequence(data.get("emails"))) if c)
        return ProviderResult(
            DomainContacts(
                domain=p.text(data.get("domain")) or clean,
                organization=p.text(data.get("organization")),
                pattern=p.text(data.get("pattern")),
                accept_all=bool(data.get("accept_all")),
                contacts=contacts,
            ),
            units_used=p.decimal_header(response.headers.get("x-credits-used")),
        )

    @staticmethod
    def _contact(entry: Any) -> ContactHit | None:
        data = p.mapping(entry)
        address = p.email(data.get("value"))
        if address is None:
            return None
        kind: ContactType = "generic" if data.get("type") == "generic" else "personal"
        sources = tuple(
            uri
            for uri in (
                p.text(p.mapping(s).get("uri"), limit=1000) for s in p.sequence(data.get("sources"))
            )
            if uri and uri.startswith(("http://", "https://"))
        )
        return ContactHit(
            email=address,
            type=kind,
            first_name=p.text(data.get("first_name"), limit=100),
            last_name=p.text(data.get("last_name"), limit=100),
            position=p.text(data.get("position"), limit=200),
            department=p.text(data.get("department"), limit=100),
            seniority=p.text(data.get("seniority"), limit=50),
            confidence=p.integer(data.get("confidence")),
            source_urls=sources,
            vendor_verified=p.mapping(data.get("verification")).get("status") == "valid",
        )
