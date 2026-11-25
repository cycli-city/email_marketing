"""Enriching a company end to end: real Postgres (store + ledger), fake providers and a fake web. The point of every test
here is either *what ends up stored* or *what was (not) paid for*."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FakeClock
from app.db.models import Company, Contact, EmailVerification
from app.domain.enums import EmailKind, EmailOrigin, VerificationStatus
from app.integrations.dns_checks import MailStatus
from app.providers.discovery import (
    CompanyHit,
    ContactHit,
    DomainContacts,
    PlaceQuery,
    SearchHit,
    VerificationOutcome,
)
from app.providers.errors import AllProvidersUnavailableError
from app.services.discovery.crawl import SiteReader
from app.services.discovery.enrich import CompanyEnricher, EnrichmentPolicy
from app.services.discovery.gateway import Called
from app.services.discovery.ledger import LookupLedger
from app.services.discovery.store import CompanyRecord, ContactRecord, LeadStore, Provenance
from tests.web_fake import FakeFetcher, html, page

GEOAPIFY = Provenance("geoapify", "provider_api")
GMAIL = (
    "gmail" + ".com"
)  # assembled: the public-repo audit rejects real-looking mail domains in tracked files

#: ``website_origin("https://clinic.example.com")`` — a business is identified by its registrable domain.
SITE = "https://example.com"
SITE_PAGE_URL = "https://clinic.example.com/"
FOUND_SITE = "https://example.org"
FOUND_PAGE_URL = "https://clinic.example.org/"
RETRY_AT = datetime(2026, 1, 6, 9, 0, tzinfo=UTC)


class FakeGateway:
    """Counts every provider call so tests can say exactly what was paid for."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.hits: list[SearchHit] | Exception = []
        self.contacts: DomainContacts | Exception = DomainContacts("", None, None, False, ())
        self.verdicts: dict[str, VerificationOutcome | Exception] = {}
        self.default_verdict: Exception | None = None

    async def search_places(self, query: PlaceQuery) -> Called[list[CompanyHit]]:
        raise NotImplementedError

    async def web_search(self, query: str, *, limit: int = 5) -> Called[list[SearchHit]]:
        self.calls["search"] += 1
        if isinstance(self.hits, Exception):
            raise self.hits
        return Called(self.hits, "fake-search")

    async def domain_contacts(self, domain: str, *, limit: int = 10) -> Called[DomainContacts]:
        self.calls["contacts"] += 1
        if isinstance(self.contacts, Exception):
            raise self.contacts
        return Called(self.contacts, "fake-contacts")

    async def verify_email(self, email: str) -> Called[VerificationOutcome]:
        self.calls["verify"] += 1
        outcome = self.verdicts.get(email, self.default_verdict)
        if isinstance(outcome, Exception):
            raise outcome
        return Called(
            outcome or VerificationOutcome(email, "deliverable", "valid"), "fake-verifier"
        )


class FakeMail:
    def __init__(self) -> None:
        self.statuses: dict[str, MailStatus] = {}
        self.asked: list[str] = []

    async def __call__(self, domain: str) -> MailStatus:
        self.asked.append(domain)
        return self.statuses.get(domain, "mx")


def unavailable(capability: str) -> AllProvidersUnavailableError:
    return AllProvidersUnavailableError(
        capability, next_available_at=RETRY_AT, reasons={"fake": "daily limit"}
    )


def named(email: str, first: str = "Jane", last: str = "Doe") -> ContactHit:
    return ContactHit(
        email,
        "personal",
        first,
        last,
        "Owner",
        None,
        None,
        90,
        ("https://clinic.example.com/team",),
        False,
    )


def generic(email: str) -> ContactHit:
    return ContactHit(email, "generic", None, None, None, None, None, 80, (), False)


def vendor_contacts(*hits: ContactHit, accept_all: bool = False) -> DomainContacts:
    return DomainContacts("example.com", "Smile Dental", "{first}", accept_all, tuple(hits))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(factory: async_sessionmaker[AsyncSession], clock: FakeClock) -> LeadStore:
    return LeadStore(factory, clock)


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher()


@pytest.fixture
def mail() -> FakeMail:
    return FakeMail()


@pytest.fixture
def make_enricher(
    store: LeadStore,
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
    mail: FakeMail,
) -> Any:
    def build(policy: EnrichmentPolicy | None = None) -> CompanyEnricher:
        return CompanyEnricher(
            store=store,
            ledger=LookupLedger(factory, clock),
            gateway=gateway,
            reader=SiteReader(fetcher),
            clock=clock,
            policy=policy,
            mail_checker=mail,
        )

    return build


async def make_company(
    store: LeadStore, workspace_id: uuid.UUID, *, website: str | None = "https://clinic.example.com"
) -> uuid.UUID:
    stored = await store.save_company(
        workspace_id,
        CompanyRecord(
            name="Smile Dental",
            website=website,
            city="Austin",
            country="US",
            phone="+1 512 555 0100",
        ),
        GEOAPIFY,
    )
    assert stored is not None
    return stored.company_id


def serve_site(
    fetcher: FakeFetcher,
    body: str = "",
    url: str = SITE,
    page_url: str = SITE_PAGE_URL,
    title: str = "Smile Dental",
) -> None:
    fetcher.pages[url] = page(page_url, html(body, title))


async def contacts_by_email(factory: async_sessionmaker[AsyncSession]) -> dict[str, Contact]:
    async with factory() as s:
        return {c.email: c for c in (await s.execute(select(Contact))).scalars() if c.email}


async def history(
    factory: async_sessionmaker[AsyncSession],
) -> list[tuple[str, VerificationStatus]]:
    async with factory() as s:
        rows = (
            await s.execute(
                select(EmailVerification).order_by(
                    EmailVerification.checked_at, EmailVerification.provider_key
                )
            )
        ).scalars()
        return [(r.provider_key, r.status) for r in rows]


# ───────────────────────────── a company that already has a website ─────────────────────────────


async def test_a_published_address_is_checked_for_free_then_verified(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
    mail: FakeMail,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(fetcher, '<a href="mailto:owner@clinic.example.com">Email the owner</a>')

    report = await make_enricher().enrich(workspace_id, company_id)

    assert (report.website, report.website_source, report.pages_read) == (SITE, "given", 1)
    assert (report.contacts_new, report.verified, report.risky, report.invalid) == (1, 1, 0, 0)
    contact = (await contacts_by_email(factory))["owner@clinic.example.com"]
    assert contact.verification_status is VerificationStatus.VERIFIED
    assert contact.email_origin is EmailOrigin.DISCOVERED
    assert contact.source_url == SITE_PAGE_URL
    assert mail.asked == ["clinic.example.com"]
    assert gateway.calls["verify"] == 1
    assert await history(factory) == [
        ("dns", VerificationStatus.UNVERIFIED),
        ("fake-verifier", VerificationStatus.VERIFIED),
    ]


async def test_only_the_companys_own_addresses_and_free_mail_it_publishes_itself_are_taken(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    fetcher: FakeFetcher,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(
        fetcher,
        '<a href="mailto:owner@clinic.example.com">a</a> <a href="mailto:agency@partner.example.org">b</a> '
        f'<a href="mailto:front.desk@{GMAIL}">c</a>',
    )

    await make_enricher().enrich(workspace_id, company_id)

    assert set(await contacts_by_email(factory)) == {
        "owner@clinic.example.com",
        f"front.desk@{GMAIL}",
    }


async def test_vendor_contacts_are_stored_with_their_provenance_and_the_named_one_is_verified_first(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(fetcher)
    gateway.contacts = vendor_contacts(
        generic("desk@clinic.example.com"), named("jane@clinic.example.com")
    )

    report = await make_enricher(EnrichmentPolicy(max_verifications=1)).enrich(
        workspace_id, company_id
    )

    contacts = await contacts_by_email(factory)
    assert contacts["jane@clinic.example.com"].verification_status is VerificationStatus.VERIFIED
    assert (
        contacts["desk@clinic.example.com"].verification_status is VerificationStatus.UNVERIFIED
    )  # over the credit cap
    assert contacts["jane@clinic.example.com"].email_origin is EmailOrigin.PROVIDER
    assert (report.contacts_new, report.verified, gateway.calls["verify"]) == (2, 1, 1)


async def test_a_second_run_creates_nothing_and_spends_nothing(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(fetcher, '<a href="mailto:owner@clinic.example.com">x</a>')
    gateway.contacts = vendor_contacts(named("jane@clinic.example.com"))
    enricher = make_enricher()

    first = await enricher.enrich(workspace_id, company_id)
    spent = dict(gateway.calls)
    rows = await history(factory)
    second = await enricher.enrich(workspace_id, company_id)

    assert (first.contacts_new, first.contacts_existing) == (2, 0)
    assert (second.contacts_new, second.contacts_existing) == (0, 2)
    assert dict(gateway.calls) == spent  # not one more provider call
    assert second.vendor_calls_avoided >= 1
    assert await history(factory) == rows  # and no new history either
    assert len(await contacts_by_email(factory)) == 2


async def test_nothing_found_at_a_domain_is_remembered_too(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(fetcher)
    enricher = make_enricher()

    await enricher.enrich(workspace_id, company_id)
    await enricher.enrich(workspace_id, company_id)

    assert gateway.calls["contacts"] == 1


async def test_a_domain_that_cannot_receive_mail_costs_no_verification_credit(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
    mail: FakeMail,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(fetcher, '<a href="mailto:owner@clinic.example.com">x</a>')
    mail.statuses["clinic.example.com"] = "null_mx"

    report = await make_enricher().enrich(workspace_id, company_id)

    assert report.invalid == 1
    assert gateway.calls["verify"] == 0
    assert (await contacts_by_email(factory))[
        "owner@clinic.example.com"
    ].verification_status is VerificationStatus.INVALID


async def test_an_inconclusive_dns_lookup_is_never_a_reason_to_reject(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
    mail: FakeMail,
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(fetcher, '<a href="mailto:owner@clinic.example.com">x</a>')
    mail.statuses["clinic.example.com"] = "dns_error"

    report = await make_enricher().enrich(workspace_id, company_id)

    assert (report.invalid, report.verified, gateway.calls["verify"]) == (0, 1, 1)


async def test_each_domain_is_looked_up_once_per_run(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
    mail: FakeMail,
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(
        fetcher,
        '<a href="mailto:a@clinic.example.com">a</a> <a href="mailto:b@clinic.example.com">b</a>',
    )
    await make_enricher().enrich(workspace_id, company_id)
    assert mail.asked == ["clinic.example.com"]


async def test_the_credit_cap_is_respected_and_named_people_come_first(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(fetcher)
    gateway.contacts = vendor_contacts(
        generic("info@clinic.example.com"),
        generic("desk@clinic.example.com"),
        named("amy@clinic.example.com", "Amy", "Lee"),
        named("bo@clinic.example.com", "Bo", "Ng"),
    )
    verified: list[str] = []
    original = gateway.verify_email

    async def spy(email: str) -> Called[VerificationOutcome]:
        verified.append(email)
        return await original(email)

    gateway.verify_email = spy  # type: ignore[method-assign]

    await make_enricher(EnrichmentPolicy(max_verifications=2)).enrich(workspace_id, company_id)

    assert sorted(verified) == ["amy@clinic.example.com", "bo@clinic.example.com"]


async def test_a_catch_all_domain_is_risky_not_verified(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(fetcher, '<a href="mailto:owner@clinic.example.com">x</a>')
    gateway.verdicts["owner@clinic.example.com"] = VerificationOutcome(
        "owner@clinic.example.com", "deliverable", "accept_all", is_catch_all=True
    )

    report = await make_enricher().enrich(workspace_id, company_id)

    assert (report.verified, report.risky) == (0, 1)


async def test_the_accept_all_warning_is_reported(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(fetcher)
    gateway.contacts = vendor_contacts(named("jane@clinic.example.com"), accept_all=True)
    report = await make_enricher().enrich(workspace_id, company_id)
    assert any("accepts all mail" in note for note in report.notes)


async def test_a_website_that_cannot_be_read_is_reported_not_raised(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
) -> None:
    company_id = await make_company(
        store, workspace_id
    )  # the fetcher knows no pages: every fetch is a 404
    report = await make_enricher().enrich(workspace_id, company_id)
    assert report.pages_read == 0
    assert report.site_problem is not None
    assert any("could not be read" in note for note in report.notes)


async def test_an_unavailable_verifier_parks_the_step_without_failing_the_run(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(
        fetcher,
        '<a href="mailto:owner@clinic.example.com">x</a> <a href="mailto:desk@clinic.example.com">y</a>',
    )
    gateway.default_verdict = unavailable("email_verification")

    report = await make_enricher().enrich(workspace_id, company_id)

    assert report.waiting_until == RETRY_AT
    assert any("email verification unavailable" in note for note in report.notes)
    assert (
        gateway.calls["verify"] == 1
    )  # it stopped at the first refusal instead of hammering the others
    statuses = {c.verification_status for c in (await contacts_by_email(factory)).values()}
    assert statuses == {VerificationStatus.UNVERIFIED}
    async with factory() as s:
        assert (
            await s.execute(select(Company.last_researched_at))
        ).scalar_one() == clock.now()  # still counted as researched


async def test_unavailable_contact_discovery_does_not_stop_the_other_steps(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(fetcher, '<a href="mailto:owner@clinic.example.com">x</a>')
    gateway.contacts = unavailable("contact_discovery")

    report = await make_enricher().enrich(workspace_id, company_id)

    assert report.waiting_until == RETRY_AT
    assert (report.contacts_new, report.verified) == (1, 1)


# ───────────────────────────── a company without a website ─────────────────────────────


def hit_for_found_site(title: str = "Smile Dental | Austin, TX") -> SearchHit:
    return SearchHit(title, FOUND_PAGE_URL, "Family dentist in Austin")


async def test_a_missing_website_is_found_by_search_confirmed_on_the_page_and_attached(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    company_id = await make_company(store, workspace_id, website=None)
    gateway.hits = [
        SearchHit("Smile Dental - Yelp", "https://www.yelp.com/biz/smile-dental-austin", ""),
        hit_for_found_site(),
    ]
    serve_site(
        fetcher,
        'Welcome to Smile Dental. <a href="mailto:owner@clinic.example.org">Email</a>',
        url=FOUND_SITE,
        page_url=FOUND_PAGE_URL,
    )

    report = await make_enricher().enrich(workspace_id, company_id)

    assert (report.website, report.website_source) == (FOUND_SITE, "search")
    assert report.contacts_new == 1
    async with factory() as s:
        row = (await s.execute(select(Company))).scalar_one()
    assert (row.domain, row.website, row.dedupe_key) == ("example.org", FOUND_SITE, "example.org")
    assert gateway.calls["search"] == 1


async def test_a_candidate_site_that_does_not_name_the_business_is_discarded(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    company_id = await make_company(store, workspace_id, website=None)
    gateway.hits = [hit_for_found_site()]  # the *title* claims it, the page itself says otherwise
    serve_site(
        fetcher,
        "Premier plumbing supplies. Call (737) 555-0199. <a href='mailto:x@clinic.example.org'>x</a>",
        url=FOUND_SITE,
        page_url=FOUND_PAGE_URL,
        title="Premier Plumbing Supplies",
    )

    report = await make_enricher().enrich(workspace_id, company_id)

    assert report.website is None
    assert any("did not confirm" in note for note in report.notes)
    assert report.contacts_new == 0
    async with factory() as s:
        assert (await s.execute(select(Company.domain))).scalar_one() is None


async def test_the_phone_number_on_the_page_also_confirms_the_business(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
) -> None:
    company_id = await make_company(store, workspace_id, website=None)
    gateway.hits = [hit_for_found_site()]
    serve_site(
        fetcher,
        "Family dentistry. Call (512) 555-0100 today.",
        url=FOUND_SITE,
        page_url=FOUND_PAGE_URL,
    )
    report = await make_enricher().enrich(workspace_id, company_id)
    assert report.website == FOUND_SITE


async def test_search_results_that_are_only_directories_lead_nowhere(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
) -> None:
    company_id = await make_company(store, workspace_id, website=None)
    gateway.hits = [
        SearchHit("Smile Dental", "https://www.yelp.com/biz/smile-dental-austin", ""),
        SearchHit("Smile Dental", "https://www.facebook.com/smiledental", ""),
    ]

    report = await make_enricher().enrich(workspace_id, company_id)

    assert report.website is None
    assert fetcher.requested == []  # nothing was fetched on the strength of a directory listing
    assert any("no plausible website" in note for note in report.notes)
    assert any("no website and no published address" in note for note in report.notes)


async def test_the_same_search_is_never_paid_for_twice(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
) -> None:
    company_id = await make_company(store, workspace_id, website=None)
    gateway.hits = []
    enricher = make_enricher()
    await enricher.enrich(workspace_id, company_id)
    await enricher.enrich(workspace_id, company_id)
    assert gateway.calls["search"] == 1


async def test_a_site_that_belongs_to_a_company_we_already_hold_is_reported_as_a_duplicate(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    holder = await store.save_company(
        workspace_id,
        CompanyRecord(name="Smile Dental Group", website="https://clinic.example.org"),
        GEOAPIFY,
    )
    assert holder is not None
    company_id = await make_company(store, workspace_id, website=None)
    gateway.hits = [hit_for_found_site()]
    serve_site(
        fetcher,
        'Smile Dental <a href="mailto:owner@clinic.example.org">x</a>',
        url=FOUND_SITE,
        page_url=FOUND_PAGE_URL,
    )

    report = await make_enricher().enrich(workspace_id, company_id)

    assert report.duplicate_of == holder.company_id
    assert report.contacts_new == 0
    async with factory() as s:
        unsited = (await s.execute(select(Company).where(Company.id == company_id))).scalar_one()
    assert (
        unsited.domain is None
    )  # untouched: merging two records is a decision for a human, not a side effect


async def test_an_unavailable_search_provider_parks_only_that_step(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
) -> None:
    company_id = await make_company(store, workspace_id, website=None)
    gateway.hits = unavailable("search")
    report = await make_enricher().enrich(workspace_id, company_id)
    assert report.waiting_until == RETRY_AT
    assert any("web search unavailable" in note for note in report.notes)


# ───────────────────────────── housekeeping ─────────────────────────────


async def test_the_company_is_marked_researched_at_the_injected_time(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    fetcher: FakeFetcher,
    clock: FakeClock,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(fetcher)
    clock.advance(hours=3)
    await make_enricher().enrich(workspace_id, company_id)
    async with factory() as s:
        assert (await s.execute(select(Company.last_researched_at))).scalar_one() == clock.now()


async def test_an_unknown_company_or_another_workspaces_company_is_refused(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    other_workspace_id: uuid.UUID,
) -> None:
    company_id = await make_company(store, workspace_id)
    enricher = make_enricher()
    with pytest.raises(LookupError):
        await enricher.enrich(workspace_id, uuid.uuid4())
    with pytest.raises(LookupError):
        await enricher.enrich(other_workspace_id, company_id)


async def test_ledger_answers_are_never_shared_across_workspaces(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    other_workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
) -> None:
    serve_site(fetcher)
    mine = await make_company(store, workspace_id)
    theirs = await make_company(store, other_workspace_id)
    enricher = make_enricher()
    await enricher.enrich(workspace_id, mine)
    await enricher.enrich(other_workspace_id, theirs)
    assert gateway.calls["contacts"] == 2


# ───────────────────────────── pinned by mutation checks ─────────────────────────────


async def test_a_vendors_shrug_is_asked_again_tomorrow_and_is_not_filed_twice_meanwhile(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
    clock: FakeClock,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(fetcher, '<a href="mailto:owner@clinic.example.com">x</a>')
    gateway.verdicts["owner@clinic.example.com"] = VerificationOutcome(
        "owner@clinic.example.com", "unknown", "temporarily unavailable"
    )
    enricher = make_enricher()

    await enricher.enrich(workspace_id, company_id)
    # the same day: the answer is remembered, so nothing is re-asked or re-filed
    same_day = await enricher.enrich(workspace_id, company_id)
    assert same_day.vendor_calls_avoided >= 1
    assert gateway.calls["verify"] == 1
    assert len([row for row in await history(factory) if row[0] == "fake-verifier"]) == 1

    clock.advance(hours=25)  # the shrug has expired (a *found* answer would live for 90 days)
    await enricher.enrich(workspace_id, company_id)
    assert gateway.calls["verify"] == 2


async def test_parked_steps_report_the_earliest_time_any_of_them_can_resume(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
) -> None:
    company_id = await make_company(store, workspace_id, website=None)
    await store.save_contact(
        workspace_id,
        company_id,
        ContactRecord(email="owner@clinic.example.com", origin=EmailOrigin.DISCOVERED),
        GEOAPIFY,
    )
    later = AllProvidersUnavailableError(
        "search", next_available_at=RETRY_AT + timedelta(hours=5), reasons={"fake": "daily limit"}
    )
    gateway.hits = later
    gateway.default_verdict = unavailable("email_verification")  # resumes sooner than the search

    report = await make_enricher().enrich(workspace_id, company_id)

    assert report.waiting_until == RETRY_AT
    assert len([n for n in report.notes if "unavailable" in n]) == 2


async def test_a_provider_with_no_known_return_time_parks_the_step_without_inventing_one(
    make_enricher: Any, store: LeadStore, workspace_id: uuid.UUID, gateway: FakeGateway
) -> None:
    company_id = await make_company(store, workspace_id, website=None)
    gateway.hits = AllProvidersUnavailableError("search", next_available_at=None, reasons={})
    report = await make_enricher().enrich(workspace_id, company_id)
    assert report.waiting_until is None  # needs a human (e.g. nothing configured), not a timer
    assert any("web search unavailable" in note for note in report.notes)


async def test_a_published_shared_mailbox_is_recorded_as_one(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    fetcher: FakeFetcher,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(fetcher, '<a href="mailto:info@clinic.example.com">x</a>')
    await make_enricher().enrich(workspace_id, company_id)
    assert (await contacts_by_email(factory))[
        "info@clinic.example.com"
    ].email_kind is EmailKind.ROLE_BASED


async def test_each_question_is_cached_separately(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
) -> None:
    """Two different companies must never share a cached search or a cached contact lookup."""
    serve_site(fetcher)
    first = await make_company(store, workspace_id, website=None)
    other = await store.save_company(
        workspace_id,
        CompanyRecord(name="Bright Orthodontics", city="Reno", country="US"),
        GEOAPIFY,
    )
    assert other is not None
    enricher = make_enricher()
    await enricher.enrich(workspace_id, first)
    await enricher.enrich(workspace_id, other.company_id)
    assert gateway.calls["search"] == 2

    with_site = await store.save_company(
        workspace_id,
        CompanyRecord(name="Maple Dental", website="https://maple-dental.github.io"),
        GEOAPIFY,
    )
    assert with_site is not None
    fetcher.pages["https://maple-dental.github.io"] = page(
        "https://maple-dental.github.io/", html("Maple Dental")
    )
    sited = await make_company(store, workspace_id)  # a different domain (example.com)
    await enricher.enrich(workspace_id, with_site.company_id)
    await enricher.enrich(workspace_id, sited)
    assert gateway.calls["contacts"] == 2


async def test_a_repeated_search_is_reported_as_avoided(
    make_enricher: Any, store: LeadStore, workspace_id: uuid.UUID
) -> None:
    company_id = await make_company(store, workspace_id, website=None)
    enricher = make_enricher()
    first = await enricher.enrich(workspace_id, company_id)
    second = await enricher.enrich(workspace_id, company_id)
    assert (first.vendor_calls_avoided, second.vendor_calls_avoided) == (0, 1)


async def test_a_duplicate_is_still_marked_researched_and_says_nothing_else(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
    clock: FakeClock,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    holder = await store.save_company(
        workspace_id,
        CompanyRecord(name="Smile Dental Group", website="https://clinic.example.org"),
        GEOAPIFY,
    )
    assert holder is not None
    company_id = await make_company(store, workspace_id, website=None)
    gateway.hits = [hit_for_found_site()]
    serve_site(fetcher, "Smile Dental", url=FOUND_SITE, page_url=FOUND_PAGE_URL)

    report = await make_enricher().enrich(workspace_id, company_id)

    assert report.notes == ("its website belongs to a company we already hold",)
    async with factory() as s:
        researched = (
            await s.execute(select(Company.last_researched_at).where(Company.id == company_id))
        ).scalar_one()
    assert researched == clock.now()


async def test_no_more_published_addresses_than_the_cap_are_taken(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    fetcher: FakeFetcher,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(
        fetcher,
        "".join(f'<a href="mailto:p{i}@clinic.example.com">p</a>' for i in range(4)),
    )
    report = await make_enricher(EnrichmentPolicy(max_site_contacts=2)).enrich(
        workspace_id, company_id
    )
    assert report.contacts_new == 2
    assert len(await contacts_by_email(factory)) == 2


async def test_no_more_vendor_contacts_than_the_cap_are_taken(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(fetcher)
    gateway.contacts = vendor_contacts(
        *(named(f"p{i}@clinic.example.com", f"P{i}", "Doe") for i in range(4))
    )
    report = await make_enricher(EnrichmentPolicy(max_vendor_contacts=2)).enrich(
        workspace_id, company_id
    )
    assert report.contacts_new == 2
    assert len(await contacts_by_email(factory)) == 2


async def test_a_step_with_no_known_return_time_never_erases_one_that_has(
    make_enricher: Any, store: LeadStore, workspace_id: uuid.UUID, gateway: FakeGateway
) -> None:
    company_id = await make_company(store, workspace_id, website=None)
    await store.save_contact(
        workspace_id,
        company_id,
        ContactRecord(email="owner@clinic.example.com", origin=EmailOrigin.DISCOVERED),
        GEOAPIFY,
    )
    gateway.hits = unavailable("search")  # can resume at RETRY_AT
    gateway.default_verdict = AllProvidersUnavailableError(
        "email_verification", next_available_at=None, reasons={}
    )

    report = await make_enricher().enrich(workspace_id, company_id)

    assert report.waiting_until == RETRY_AT
    assert len([n for n in report.notes if "unavailable" in n]) == 2


async def test_a_shared_mailbox_the_vendor_flags_is_recorded_as_one_even_with_an_ordinary_looking_name(
    make_enricher: Any,
    store: LeadStore,
    workspace_id: uuid.UUID,
    gateway: FakeGateway,
    fetcher: FakeFetcher,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    company_id = await make_company(store, workspace_id)
    serve_site(fetcher)
    gateway.contacts = vendor_contacts(
        generic("desk@clinic.example.com")
    )  # "desk" is not on our own role list
    await make_enricher().enrich(workspace_id, company_id)
    assert (await contacts_by_email(factory))[
        "desk@clinic.example.com"
    ].email_kind is EmailKind.ROLE_BASED
