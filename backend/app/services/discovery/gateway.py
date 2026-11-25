"""The one place where discovery asks providers for things.

The enrichment pipeline never imports a vendor and never talks to the provider manager directly: it holds a
:class:`DiscoveryGateway`. In production that is :class:`ManagerGateway`, which routes every call through the provider
manager (quota accounting, pacing, failover between *different* vendors for the same capability, parking when none is
available — ADR-030/031). Tests substitute a fake that counts calls.

Failure contract: ``AllProvidersUnavailableError`` (nothing eligible right now — park until ``next_available_at``) and the
errors of :mod:`app.providers.errors` propagate unchanged; the caller decides whether to park, skip or retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from app.domain.enums import Capability
from app.providers.discovery import (
    CompanyHit,
    DomainContacts,
    PlaceQuery,
    SearchHit,
    VerificationOutcome,
)
from app.providers.manager import ProviderManager

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Called(Generic[T]):
    """A provider's answer together with *which* provider gave it (provenance)."""

    value: T
    provider_key: str


class DiscoveryGateway(Protocol):
    async def search_places(self, query: PlaceQuery) -> Called[list[CompanyHit]]: ...

    async def web_search(self, query: str, *, limit: int = 5) -> Called[list[SearchHit]]: ...

    async def domain_contacts(self, domain: str, *, limit: int = 10) -> Called[DomainContacts]: ...

    async def verify_email(self, email: str) -> Called[VerificationOutcome]: ...


class ManagerGateway:
    def __init__(self, manager: ProviderManager) -> None:
        self._manager = manager

    async def search_places(self, query: PlaceQuery) -> Called[list[CompanyHit]]:
        outcome = await self._manager.call(
            Capability.COMPANY_DISCOVERY, lambda provider: provider.search_places(query)
        )
        return Called(outcome.value, outcome.provider_key)

    async def web_search(self, query: str, *, limit: int = 5) -> Called[list[SearchHit]]:
        outcome = await self._manager.call(
            Capability.SEARCH, lambda provider: provider.web_search(query, limit=limit)
        )
        return Called(outcome.value, outcome.provider_key)

    async def domain_contacts(self, domain: str, *, limit: int = 10) -> Called[DomainContacts]:
        # Only a domain name leaves the building, no personal data.
        outcome = await self._manager.call(
            Capability.CONTACT_DISCOVERY,
            lambda provider: provider.domain_contacts(domain, limit=limit),
        )
        return Called(outcome.value, outcome.provider_key)

    async def verify_email(self, email: str) -> Called[VerificationOutcome]:
        # An address is personal data: only providers cleared to receive it are eligible.
        outcome = await self._manager.call(
            Capability.EMAIL_VERIFICATION,
            lambda provider: provider.verify_email(email),
            personal_data=True,
        )
        return Called(outcome.value, outcome.provider_key)
