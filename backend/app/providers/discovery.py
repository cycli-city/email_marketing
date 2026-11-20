"""Vendor-neutral types for the lead-discovery capabilities (company discovery, contact discovery, e-mail verification,
web search). Business logic builds these requests and reads these results; it never imports a vendor (ARCHITECTURE §4).

Everything a vendor returns is **untrusted data**: names, URLs and text go through the same normalisation and extraction
as anything scraped, and nothing here is ever followed as an instruction.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Literal

from app.providers.registry import Provider
from app.providers.spec import ProviderResult

# ───────────────────────────── company discovery ─────────────────────────────


@dataclass(frozen=True, slots=True)
class PlaceQuery:
    """Businesses of some categories around a point. ``categories`` use the vendor-neutral names in
    :data:`app.providers.adapters.geoapify.CATEGORY_MAP` or the vendor's own (``healthcare.dentist``)."""

    categories: tuple[str, ...]
    latitude: float
    longitude: float
    radius_m: int = 5000
    limit: int = 20
    offset: int = 0


@dataclass(frozen=True, slots=True)
class CompanyHit:
    name: str
    website: str | None
    phone: str | None
    email: (
        str | None
    )  # an address the business itself published (e.g. in OpenStreetMap) — never a guess
    address: str | None
    city: str | None
    country_code: str | None
    latitude: float | None
    longitude: float | None
    categories: tuple[str, ...]
    source_id: str  # the vendor's stable id for this record (provenance)


class CompanyDiscoveryProvider(Provider):
    @abstractmethod
    async def search_places(self, query: PlaceQuery) -> ProviderResult[list[CompanyHit]]:
        """Businesses near a point. Raises ``app.providers.errors`` types only."""


# ───────────────────────────── contact discovery ─────────────────────────────

ContactType = Literal["personal", "generic"]


@dataclass(frozen=True, slots=True)
class ContactHit:
    email: str
    type: ContactType  # "generic" = a role mailbox (info@, sales@) — treated differently by the compliance gate
    first_name: str | None
    last_name: str | None
    position: str | None
    department: str | None
    seniority: str | None
    confidence: int | None  # the vendor's own 0-100 score, if it gives one
    source_urls: tuple[str, ...]  # public pages the vendor says it found the address on
    vendor_verified: bool  # the vendor reports a recent positive verification


@dataclass(frozen=True, slots=True)
class DomainContacts:
    domain: str
    organization: str | None
    pattern: str | None  # e.g. "{first}.{last}" — used to explain, never to GUESS addresses
    accept_all: bool  # the domain accepts any local part, so verification cannot confirm a mailbox
    contacts: tuple[ContactHit, ...] = field(default_factory=tuple)


class ContactDiscoveryProvider(Provider):
    @abstractmethod
    async def domain_contacts(
        self, domain: str, *, limit: int = 10
    ) -> ProviderResult[DomainContacts]:
        """Publicly sourced contacts at a domain. Raises ``app.providers.errors`` types only."""


# ───────────────────────────── e-mail verification ─────────────────────────────

Verdict = Literal["deliverable", "undeliverable", "risky", "unknown"]


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    email: str
    verdict: Verdict
    detail: str  # the vendor's own status word, kept for audit ("DomainHasNullMx", "valid", …)
    is_disposable: bool | None = None
    is_role: bool | None = None
    is_catch_all: bool | None = None
    is_free_provider: bool | None = None


class EmailVerificationProvider(Provider):
    @abstractmethod
    async def verify_email(self, email: str) -> ProviderResult[VerificationOutcome]:
        """Is this address deliverable? Sends the address (personal data) to the vendor — only providers cleared for it."""


# ───────────────────────────── web search ─────────────────────────────


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str


class SearchProvider(Provider):
    @abstractmethod
    async def web_search(self, query: str, *, limit: int = 5) -> ProviderResult[list[SearchHit]]:
        """Web results for a query. Result text is untrusted data. Raises ``app.providers.errors`` types only."""
