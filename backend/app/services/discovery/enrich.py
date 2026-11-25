"""Turning a company record into contacts we can actually consider writing to (ADR-040/042).

One call, :meth:`CompanyEnricher.enrich`, walks a company through these steps, cheapest first, and records *what it found and
why* at every step:

1. **A website.** If the company has none, search the web for it (cached), accept a result only if :func:`choose_website`
   considers it plausible **and** the fetched page itself confirms the business by name or phone. A site that turns out to
   be a business we already hold is reported as a duplicate, never stored twice.
2. **Published addresses.** Read the company's own pages (SSRF-guarded, robots-respecting) and keep addresses the site
   publishes at its own domain, or free-mail addresses it publishes itself. Third-party addresses are never taken.
3. **Vendor contacts.** Ask the contact-discovery provider about the *domain* (cached; a negative answer is cached too).
4. **Free checks.** Does the address's domain accept mail at all? A certain "no" ends the matter without spending a credit.
5. **Vendor verification** of the few best candidates, cached per address.

An address that is only *guessed* from a name pattern is never produced here. A provider being unavailable never fails the
run: the step is skipped, the reason is noted and ``waiting_until`` says when to try again.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.clock import Clock
from app.domain.enums import EmailKind, EmailOrigin, VerificationStatus
from app.domain.identity import (
    email_domain,
    is_free_mail,
    normalise_name,
    registrable_domain,
)
from app.domain.site_match import (
    SearchCandidate,
    choose_website,
    confirms_identity,
)
from app.integrations.dns_checks import MailStatus, mail_status
from app.providers.errors import AllProvidersUnavailableError
from app.services.discovery.codec import (
    decode_domain_contacts,
    decode_hits,
    decode_outcome,
    encode_domain_contacts,
    encode_hits,
    encode_outcome,
)
from app.services.discovery.crawl import SiteReader, SiteReading
from app.services.discovery.gateway import DiscoveryGateway
from app.services.discovery.ledger import Asked, LookupLedger
from app.services.discovery.store import (
    CompanyView,
    ContactRecord,
    ContactView,
    LeadStore,
    Provenance,
)

SEARCH_OP = "search:website"
CONTACTS_OP = "contacts:domain"
VERIFY_OP = "verify:email"
#: "unknown" is a vendor's shrug, not an answer — ask again tomorrow rather than in a month.
UNKNOWN_VERDICT_TTL = timedelta(days=1)

MailChecker = Callable[[str], Awaitable[MailStatus]]


@dataclass(frozen=True, slots=True)
class EnrichmentPolicy:
    max_site_contacts: int = 5  # addresses taken from a company's own pages
    max_vendor_contacts: int = 5  # addresses taken from the contact-discovery provider
    max_verifications: int = 3  # vendor verification credits spent per company
    search_results: int = 5


@dataclass(frozen=True, slots=True)
class EnrichmentReport:
    company_id: uuid.UUID
    duplicate_of: uuid.UUID | None = (
        None  # the website turned out to belong to a company we already hold
    )
    website: str | None = None
    website_source: str | None = None  # "given" | "search"
    pages_read: int = 0
    site_problem: str | None = None
    contacts_new: int = 0
    contacts_existing: int = 0
    verified: int = 0
    risky: int = 0
    invalid: int = 0
    vendor_calls_avoided: int = 0  # answered from the ledger
    notes: tuple[str, ...] = ()
    waiting_until: datetime | None = None  # a provider is exhausted: resume then


@dataclass(slots=True)
class _Run:
    """Mutable bookkeeping for one enrichment, folded into the immutable report at the end."""

    company_id: uuid.UUID
    notes: list[str] = field(default_factory=list)
    waiting_until: datetime | None = None
    duplicate_of: uuid.UUID | None = None
    website: str | None = None
    website_source: str | None = None
    pages_read: int = 0
    site_problem: str | None = None
    contacts_new: int = 0
    contacts_existing: int = 0
    verified: int = 0
    risky: int = 0
    invalid: int = 0
    avoided: int = 0
    mail_cache: dict[str, MailStatus] = field(default_factory=dict)

    def park(self, what: str, exc: AllProvidersUnavailableError) -> None:
        self.notes.append(f"{what} unavailable right now")
        if exc.next_available_at is not None and (
            self.waiting_until is None or exc.next_available_at < self.waiting_until
        ):
            self.waiting_until = exc.next_available_at

    def report(self) -> EnrichmentReport:
        return EnrichmentReport(
            company_id=self.company_id,
            duplicate_of=self.duplicate_of,
            website=self.website,
            website_source=self.website_source,
            pages_read=self.pages_read,
            site_problem=self.site_problem,
            contacts_new=self.contacts_new,
            contacts_existing=self.contacts_existing,
            verified=self.verified,
            risky=self.risky,
            invalid=self.invalid,
            vendor_calls_avoided=self.avoided,
            notes=tuple(self.notes),
            waiting_until=self.waiting_until,
        )


_KIND_RANK = {EmailKind.NAMED: 0, EmailKind.ROLE_BASED: 1, EmailKind.UNKNOWN: 2}
_ORIGIN_RANK = {
    EmailOrigin.DISCOVERED: 0,
    EmailOrigin.MANUAL: 0,
    EmailOrigin.PROVIDER: 1,
    EmailOrigin.IMPORTED: 2,
    EmailOrigin.GUESSED: 3,
}


class CompanyEnricher:
    def __init__(
        self,
        *,
        store: LeadStore,
        ledger: LookupLedger,
        gateway: DiscoveryGateway,
        reader: SiteReader,
        clock: Clock,
        policy: EnrichmentPolicy | None = None,
        mail_checker: MailChecker = mail_status,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._gateway = gateway
        self._reader = reader
        self._clock = clock
        self._policy = policy or EnrichmentPolicy()
        self._mail = mail_checker

    async def enrich(self, workspace_id: uuid.UUID, company_id: uuid.UUID) -> EnrichmentReport:
        view = await self._store.company_view(workspace_id, company_id)
        if view is None:
            raise LookupError(f"company {company_id} not found in this workspace")
        run = _Run(company_id=company_id)

        reading = await self._website(workspace_id, view, run)
        if run.duplicate_of is not None:
            # This "new" company is a business we already hold: stop here, and let a person decide about merging.
            await self._store.mark_researched(workspace_id, company_id)
            return run.report()
        if run.website is not None:
            await self._published_addresses(workspace_id, company_id, reading, run)
            await self._vendor_addresses(workspace_id, company_id, run)
        await self._check_and_verify(workspace_id, company_id, run)
        if run.website is None and not (run.contacts_new or run.contacts_existing):
            run.notes.append("no website and no published address found")
        await self._store.mark_researched(workspace_id, company_id)
        return run.report()

    # ── 1 + 2: website and published addresses ──

    async def _website(
        self, workspace_id: uuid.UUID, view: CompanyView, run: _Run
    ) -> SiteReading | None:
        if view.website is not None:
            run.website, run.website_source = view.website, "given"
            return await self._read(view.website, run)
        try:
            choice_and_reading = await self._search_for_site(workspace_id, view, run)
        except AllProvidersUnavailableError as exc:
            run.park("web search", exc)
            return None
        if choice_and_reading is None:
            return None
        provider_key, website, reason, reading = choice_and_reading
        holder = await self._store.attach_website(
            workspace_id,
            company_id=run.company_id,
            website=website,
            provenance=Provenance(provider_key, "provider_api", website, {"matched_on": reason}),
        )
        if holder is not None:
            run.duplicate_of = holder
            run.notes.append("its website belongs to a company we already hold")
            return None
        run.website, run.website_source = website, "search"
        run.pages_read, run.site_problem = len(reading.pages), reading.problem
        return reading

    async def _read(self, website: str, run: _Run) -> SiteReading:
        reading = await self._reader.read(website)
        run.pages_read, run.site_problem = len(reading.pages), reading.problem
        if reading.problem and not reading.pages:
            run.notes.append(f"website could not be read ({reading.problem})")
        return reading

    async def _search_for_site(
        self, workspace_id: uuid.UUID, view: CompanyView, run: _Run
    ) -> tuple[str, str, str, SiteReading] | None:
        key = f"{normalise_name(view.name)}|{normalise_name(view.city)}|{(view.country or '').lower()}"
        query = " ".join(part for part in (view.name, view.city) if part)

        async def ask() -> Asked:
            called = await self._gateway.web_search(query, limit=self._policy.search_results)
            return Asked(bool(called.value), encode_hits(called.value), called.provider_key)

        answer = await self._ledger.through(workspace_id, SEARCH_OP, key, ask)
        run.avoided += answer.cached
        hits = [SearchCandidate(h.url, h.title, h.snippet) for h in decode_hits(answer.result)]
        choice = choose_website(view.name, view.city, hits)
        if choice is None:
            run.notes.append("no plausible website in the search results")
            return None
        reading = await self._reader.read(choice.website)
        if not confirms_identity(view.name, view.phone, reading.text):
            run.notes.append("a candidate website did not confirm the business; discarded")
            return None
        return answer.answered_by, choice.website, choice.reason, reading

    async def _published_addresses(
        self,
        workspace_id: uuid.UUID,
        company_id: uuid.UUID,
        reading: SiteReading | None,
        run: _Run,
    ) -> None:
        if reading is None:
            return
        taken = 0
        for found in reading.emails:
            # Only an address at the company's own domain, or a free-mail address the company itself publishes.
            if not (found.same_site or is_free_mail(found.address)):
                continue
            if taken >= self._policy.max_site_contacts:
                break
            stored = await self._store.save_contact(
                workspace_id,
                company_id,
                ContactRecord(
                    email=found.address,
                    origin=EmailOrigin.DISCOVERED,
                    source_url=found.page_url,
                ),
                Provenance("website", "public_website", found.page_url, {"method": found.method}),
            )
            if stored is None:
                continue
            taken += 1
            run.contacts_new += stored.created
            run.contacts_existing += not stored.created

    # ── 3: vendor contacts ──

    async def _vendor_addresses(
        self, workspace_id: uuid.UUID, company_id: uuid.UUID, run: _Run
    ) -> None:
        domain = registrable_domain(run.website)
        if domain is None:
            return

        async def ask() -> Asked:
            called = await self._gateway.domain_contacts(
                domain, limit=self._policy.max_vendor_contacts
            )
            return Asked(
                bool(called.value.contacts),
                encode_domain_contacts(called.value),
                called.provider_key,
            )

        try:
            answer = await self._ledger.through(workspace_id, CONTACTS_OP, domain, ask)
        except AllProvidersUnavailableError as exc:
            run.park("contact discovery", exc)
            return
        run.avoided += answer.cached
        found = decode_domain_contacts(answer.result)
        if found.accept_all:
            run.notes.append("the domain accepts all mail; its addresses cannot be confirmed")
        for hit in found.contacts[: self._policy.max_vendor_contacts]:
            stored = await self._store.save_contact(
                workspace_id,
                company_id,
                ContactRecord(
                    email=hit.email,
                    origin=EmailOrigin.PROVIDER,
                    first_name=hit.first_name,
                    last_name=hit.last_name,
                    position=hit.position,
                    generic=hit.type == "generic",
                    source_url=hit.source_urls[0] if hit.source_urls else None,
                ),
                Provenance(
                    answer.answered_by,
                    "provider_api",
                    hit.source_urls[0] if hit.source_urls else None,
                ),
            )
            if stored is None:
                continue
            run.contacts_new += stored.created
            run.contacts_existing += not stored.created

    # ── 4 + 5: free checks, then vendor verification ──

    async def _check_and_verify(
        self, workspace_id: uuid.UUID, company_id: uuid.UUID, run: _Run
    ) -> None:
        contacts = await self._store.contacts_of(workspace_id, company_id)
        candidates: list[ContactView] = []
        for contact in contacts:
            status = contact.status
            if (
                status is VerificationStatus.UNVERIFIED
                and contact.email_origin is not EmailOrigin.GUESSED
            ):
                status = await self._free_check(workspace_id, contact, run)
            if status is VerificationStatus.UNVERIFIED:
                candidates.append(contact)
            else:
                self._tally(status, run)
        candidates.sort(key=lambda c: (_KIND_RANK[c.kind], _ORIGIN_RANK[c.email_origin], c.email))
        for contact in candidates[: self._policy.max_verifications]:
            try:
                status = await self._vendor_verify(workspace_id, contact, run)
            except AllProvidersUnavailableError as exc:
                run.park("email verification", exc)
                break
            self._tally(status, run)

    async def _free_check(
        self, workspace_id: uuid.UUID, contact: ContactView, run: _Run
    ) -> VerificationStatus:
        domain = email_domain(contact.email)
        if domain is None:
            return contact.status
        if domain not in run.mail_cache:
            run.mail_cache[domain] = await self._mail(domain)
        return await self._store.record_mail_check(
            workspace_id, contact.contact_id, run.mail_cache[domain]
        )

    async def _vendor_verify(
        self, workspace_id: uuid.UUID, contact: ContactView, run: _Run
    ) -> VerificationStatus:
        async def ask() -> Asked:
            called = await self._gateway.verify_email(contact.email)
            return Asked(
                called.value.verdict != "unknown", encode_outcome(called.value), called.provider_key
            )

        answer = await self._ledger.through(
            workspace_id, VERIFY_OP, contact.email, ask, negative_ttl=UNKNOWN_VERDICT_TTL
        )
        run.avoided += answer.cached
        if answer.cached and not answer.found:
            return contact.status  # a remembered shrug adds nothing to the history
        outcome = decode_outcome(answer.result)
        return await self._store.record_verification(
            workspace_id, contact.contact_id, answer.answered_by, outcome
        )

    @staticmethod
    def _tally(status: VerificationStatus, run: _Run) -> None:
        if status is VerificationStatus.VERIFIED:
            run.verified += 1
        elif status is VerificationStatus.RISKY:
            run.risky += 1
        elif status is VerificationStatus.INVALID:
            run.invalid += 1
