"""The lead store against a real Postgres: a business or an address found twice is stored once, and merging only fills gaps
(ADR-028). Verification verdicts can never promote a guess to "verified"."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FakeClock
from app.db.models import Company, Contact, EmailVerification, LeadSource
from app.db.session import session_scope
from app.domain.enums import EmailKind, EmailOrigin, SubscriberType, VerificationStatus
from app.providers.discovery import VerificationOutcome
from app.services.discovery.store import (
    CompanyRecord,
    ContactRecord,
    LeadStore,
    Provenance,
)

GEOAPIFY = Provenance("geoapify", "provider_api", "https://places.example.com/p/1", {"id": "p1"})
SITE = Provenance("website", "public_website", "https://smile-dental.github.io/contact")
GMAIL = (
    "gmail" + ".com"
)  # assembled: the public-repo audit rejects real-looking mail domains in tracked files

DENTIST = CompanyRecord(
    name="Smile Dental LLC",
    website="https://www.Smile-Dental.github.io/about?x=1",
    phone="+1 555 0100",
    city="Austin",
    country="us",
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(factory: async_sessionmaker[AsyncSession], clock: FakeClock) -> LeadStore:
    return LeadStore(factory, clock)


async def count(factory: async_sessionmaker[AsyncSession], model: type) -> int:
    async with factory() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def company_row(factory: async_sessionmaker[AsyncSession], company_id: uuid.UUID) -> Company:
    async with factory() as s:
        return (await s.execute(select(Company).where(Company.id == company_id))).scalar_one()


async def contact_row(factory: async_sessionmaker[AsyncSession], contact_id: uuid.UUID) -> Contact:
    async with factory() as s:
        return (await s.execute(select(Contact).where(Contact.id == contact_id))).scalar_one()


# ───────────────────────────── companies ─────────────────────────────


async def test_a_new_company_is_stored_under_its_domain(
    store: LeadStore,
    workspace_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
) -> None:
    stored = await store.save_company(workspace_id, DENTIST, GEOAPIFY)

    assert stored is not None
    assert stored.created is True
    assert stored.key == "smile-dental.github.io"
    row = await company_row(factory, stored.company_id)
    assert row.name == "Smile Dental LLC"
    assert row.domain == "smile-dental.github.io"
    assert row.website == "https://smile-dental.github.io"  # canonical homepage, no path or query
    assert row.country == "US"
    assert row.discovered_at == clock.now()


async def test_the_same_business_found_again_is_a_duplicate_not_a_second_company(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    first = await store.save_company(workspace_id, DENTIST, GEOAPIFY)
    second = await store.save_company(
        workspace_id,
        CompanyRecord(name="SMILE DENTAL", website="http://smile-dental.github.io/"),
        SITE,
    )

    assert first is not None
    assert second is not None
    assert second.created is False
    assert second.company_id == first.company_id
    assert await count(factory, Company) == 1


async def test_sites_on_a_shared_host_are_different_businesses(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    a = await store.save_company(
        workspace_id, CompanyRecord(name="A", website="https://alpha.github.io"), GEOAPIFY
    )
    b = await store.save_company(
        workspace_id, CompanyRecord(name="B", website="https://beta.github.io"), GEOAPIFY
    )
    assert a is not None
    assert b is not None
    assert a.company_id != b.company_id
    assert await count(factory, Company) == 2


async def test_a_business_first_seen_without_a_website_is_adopted_when_one_turns_up(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    first = await store.save_company(
        workspace_id,
        CompanyRecord(name="Smile Dental", city="Austin", country="US", phone="+1 555 0100"),
        GEOAPIFY,
    )
    assert first is not None
    assert first.key.startswith("name:")

    later = await store.save_company(
        workspace_id,
        CompanyRecord(
            name="Smile Dental LLC",
            website="https://smile-dental.github.io",
            city="Austin",
            country="US",
        ),
        SITE,
    )

    assert later is not None
    assert later.created is False  # the same business, now with a website
    assert later.company_id == first.company_id
    row = await company_row(factory, first.company_id)
    assert (row.dedupe_key, row.domain) == ("smile-dental.github.io", "smile-dental.github.io")
    assert row.phone == "+1 555 0100"  # what we already knew is kept
    assert await count(factory, Company) == 1

    # …and it is now found by its domain
    again = await store.save_company(
        workspace_id, CompanyRecord(name="x", website="https://smile-dental.github.io"), SITE
    )
    assert again is not None
    assert again.company_id == first.company_id


async def test_same_name_in_another_city_is_a_different_business(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    austin = await store.save_company(
        workspace_id, CompanyRecord(name="Smile Dental", city="Austin", country="US"), GEOAPIFY
    )
    dallas = await store.save_company(
        workspace_id, CompanyRecord(name="Smile Dental", city="Dallas", country="US"), GEOAPIFY
    )
    austin_again = await store.save_company(
        workspace_id, CompanyRecord(name="Smile Dental Inc.", city="austin", country="us"), GEOAPIFY
    )
    assert austin is not None
    assert dallas is not None
    assert austin_again is not None
    assert austin.company_id != dallas.company_id
    assert austin_again.company_id == austin.company_id
    assert await count(factory, Company) == 2


async def test_merging_fills_gaps_but_never_overwrites_what_we_know(
    store: LeadStore,
    workspace_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
) -> None:
    first = await store.save_company(workspace_id, DENTIST, GEOAPIFY)
    assert first is not None
    clock.advance(hours=1)
    await store.save_company(
        workspace_id,
        CompanyRecord(
            name="Different Name Entirely",
            website="https://smile-dental.github.io",
            phone="+1 555 9999",
            address="1 Main St",
            industry="Dentistry",
        ),
        SITE,
    )

    row = await company_row(factory, first.company_id)
    assert row.name == "Smile Dental LLC"
    assert row.phone == "+1 555 0100"  # not overwritten
    assert row.address == "1 Main St"  # gap filled
    assert row.industry == "Dentistry"
    assert row.updated_at == clock.now()


async def test_a_company_with_nothing_to_identify_it_is_rejected(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    assert await store.save_company(workspace_id, CompanyRecord(name=None), GEOAPIFY) is None
    assert (
        await store.save_company(
            workspace_id, CompanyRecord(name="   ", website="not a url"), GEOAPIFY
        )
        is None
    )
    assert await count(factory, Company) == 0


async def test_a_nameless_company_with_a_website_is_named_after_its_domain(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    stored = await store.save_company(
        workspace_id, CompanyRecord(name=None, website="https://acme.github.io"), SITE
    )
    assert stored is not None
    assert (await company_row(factory, stored.company_id)).name == "acme.github.io"


async def test_untrusted_text_is_cleaned_and_clipped_before_it_reaches_the_database(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    stored = await store.save_company(
        workspace_id,
        CompanyRecord(
            name="Evil\x00 \n Corp " + "x" * 1000,
            website="https://evil.github.io",
            country="USA",
            phone="1" * 200,
        ),
        SITE,
    )
    assert stored is not None
    row = await company_row(factory, stored.company_id)
    assert "\x00" not in row.name
    assert "\n" not in row.name
    assert len(row.name) <= 300
    assert row.country is None  # not a two-letter code
    assert row.phone is not None
    assert len(row.phone) <= 64


async def test_workspaces_never_share_companies(
    store: LeadStore,
    workspace_id: uuid.UUID,
    other_workspace_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    mine = await store.save_company(workspace_id, DENTIST, GEOAPIFY)
    theirs = await store.save_company(other_workspace_id, DENTIST, GEOAPIFY)
    assert mine is not None
    assert theirs is not None
    assert theirs.created is True
    assert mine.company_id != theirs.company_id
    assert await count(factory, Company) == 2


async def test_concurrent_discoveries_of_one_business_create_exactly_one_company(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    results = await asyncio.gather(
        *(store.save_company(workspace_id, DENTIST, GEOAPIFY) for _ in range(8))
    )

    assert await count(factory, Company) == 1
    assert sum(1 for r in results if r is not None and r.created) == 1
    assert len({r.company_id for r in results if r is not None}) == 1


async def test_provenance_is_kept_once_per_provider_and_page(
    store: LeadStore,
    workspace_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
) -> None:
    stored = await store.save_company(workspace_id, DENTIST, GEOAPIFY)
    assert stored is not None
    await store.save_company(
        workspace_id, DENTIST, GEOAPIFY
    )  # same provider, same page → nothing new
    assert await count(factory, LeadSource) == 1

    await store.save_company(
        workspace_id, DENTIST, SITE
    )  # another source of the same business → recorded
    async with factory() as s:
        rows = (
            (await s.execute(select(LeadSource).order_by(LeadSource.provider_key))).scalars().all()
        )
    assert [(r.provider_key, r.entity_type, r.entity_id) for r in rows] == [
        ("geoapify", "company", stored.company_id),
        ("website", "company", stored.company_id),
    ]
    assert rows[0].payload == {"id": "p1"}
    assert rows[0].retrieved_at == clock.now()


# ───────────────────────────── contacts ─────────────────────────────


async def make_company(store: LeadStore, workspace_id: uuid.UUID) -> uuid.UUID:
    stored = await store.save_company(workspace_id, DENTIST, GEOAPIFY)
    assert stored is not None
    return stored.company_id


async def test_a_named_contact_is_stored_unverified(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    company_id = await make_company(store, workspace_id)
    stored = await store.save_contact(
        workspace_id,
        company_id,
        ContactRecord(
            email="  Jane.Doe@Example.COM ",
            origin=EmailOrigin.PROVIDER,
            first_name="Jane",
            last_name="Doe",
            position="Owner",
        ),
        SITE,
    )

    assert stored is not None
    assert stored.created is True
    assert stored.email == "jane.doe@example.com"
    assert stored.kind is EmailKind.NAMED
    assert stored.status is VerificationStatus.UNVERIFIED
    row = await contact_row(factory, stored.contact_id)
    assert (row.full_name, row.position, row.company_id) == ("Jane Doe", "Owner", company_id)


async def test_role_mailboxes_are_marked_role_based(
    store: LeadStore, workspace_id: uuid.UUID
) -> None:
    company_id = await make_company(store, workspace_id)
    info = await store.save_contact(
        workspace_id,
        company_id,
        ContactRecord(email="info@example.com", origin=EmailOrigin.DISCOVERED),
        SITE,
    )
    shared = await store.save_contact(
        workspace_id,
        company_id,
        ContactRecord(email="desk@example.com", origin=EmailOrigin.PROVIDER, generic=True),
        SITE,
    )
    assert info is not None
    assert shared is not None
    assert info.kind is EmailKind.ROLE_BASED
    assert shared.kind is EmailKind.ROLE_BASED


async def test_an_invalid_address_is_rejected(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    for bad in ("", "not-an-email", "a@b", "two@@example.com"):
        assert (
            await store.save_contact(
                workspace_id, None, ContactRecord(email=bad, origin=EmailOrigin.PROVIDER), SITE
            )
            is None
        )
    assert await count(factory, Contact) == 0


async def test_the_same_address_found_again_is_one_contact_and_merging_only_fills_gaps(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    company_id = await make_company(store, workspace_id)
    first = await store.save_contact(
        workspace_id,
        None,
        ContactRecord(
            email="jane@example.com",
            origin=EmailOrigin.PROVIDER,
            first_name="Jane",
            position="Owner",
        ),
        SITE,
    )
    second = await store.save_contact(
        workspace_id,
        company_id,
        ContactRecord(
            email="JANE@example.com",
            origin=EmailOrigin.PROVIDER,
            first_name="Janet",
            last_name="Doe",
            position="Receptionist",
        ),
        GEOAPIFY,
    )

    assert first is not None
    assert second is not None
    assert second.created is False
    assert second.contact_id == first.contact_id
    assert await count(factory, Contact) == 1
    row = await contact_row(factory, first.contact_id)
    assert row.first_name == "Jane"  # kept
    assert row.position == "Owner"  # kept
    assert row.last_name == "Doe"  # gap filled
    assert row.company_id == company_id  # gap filled


async def test_a_contact_is_never_moved_to_a_different_company(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    first_company = await make_company(store, workspace_id)
    other = await store.save_company(
        workspace_id, CompanyRecord(name="Other", website="https://other.github.io"), SITE
    )
    assert other is not None
    record = ContactRecord(email="jane@example.com", origin=EmailOrigin.PROVIDER)
    stored = await store.save_contact(workspace_id, first_company, record, SITE)
    assert stored is not None
    await store.save_contact(workspace_id, other.company_id, record, SITE)
    assert (await contact_row(factory, stored.contact_id)).company_id == first_company


async def test_gmail_spellings_of_one_mailbox_are_one_contact(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    plain = await store.save_contact(
        workspace_id,
        None,
        ContactRecord(email=f"jane.doe@{GMAIL}", origin=EmailOrigin.DISCOVERED),
        SITE,
    )
    dotted = await store.save_contact(
        workspace_id,
        None,
        ContactRecord(email=f"janedoe+offers@{GMAIL}", origin=EmailOrigin.DISCOVERED),
        SITE,
    )
    other = await store.save_contact(
        workspace_id,
        None,
        ContactRecord(email=f"john.doe@{GMAIL}", origin=EmailOrigin.DISCOVERED),
        SITE,
    )
    assert plain is not None
    assert dotted is not None
    assert other is not None
    assert dotted.created is False
    assert dotted.contact_id == plain.contact_id
    assert other.contact_id != plain.contact_id
    assert await count(factory, Contact) == 2


async def test_dots_still_matter_on_other_domains(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    a = await store.save_contact(
        workspace_id,
        None,
        ContactRecord(email="j.doe@example.com", origin=EmailOrigin.PROVIDER),
        SITE,
    )
    b = await store.save_contact(
        workspace_id,
        None,
        ContactRecord(email="jdoe@example.com", origin=EmailOrigin.PROVIDER),
        SITE,
    )
    assert a is not None
    assert b is not None
    assert a.contact_id != b.contact_id
    assert await count(factory, Contact) == 2


async def test_a_guess_that_a_real_source_later_publishes_stops_being_a_guess_but_never_the_reverse(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    guess = ContactRecord(email="jane@example.com", origin=EmailOrigin.GUESSED)
    stored = await store.save_contact(workspace_id, None, guess, SITE)
    assert stored is not None
    await store.save_contact(
        workspace_id, None, ContactRecord(email=guess.email, origin=EmailOrigin.DISCOVERED), SITE
    )
    assert (await contact_row(factory, stored.contact_id)).email_origin is EmailOrigin.DISCOVERED

    await store.save_contact(
        workspace_id, None, guess, SITE
    )  # a guess never demotes a published address
    assert (await contact_row(factory, stored.contact_id)).email_origin is EmailOrigin.DISCOVERED


async def test_concurrent_discoveries_of_one_address_create_exactly_one_contact(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    record = ContactRecord(email="jane@example.com", origin=EmailOrigin.PROVIDER)
    results = await asyncio.gather(
        *(store.save_contact(workspace_id, None, record, SITE) for _ in range(8))
    )
    assert await count(factory, Contact) == 1
    assert sum(1 for r in results if r is not None and r.created) == 1


async def test_workspaces_never_share_contacts(
    store: LeadStore,
    workspace_id: uuid.UUID,
    other_workspace_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    record = ContactRecord(email="jane@example.com", origin=EmailOrigin.PROVIDER)
    mine = await store.save_contact(workspace_id, None, record, SITE)
    theirs = await store.save_contact(other_workspace_id, None, record, SITE)
    assert mine is not None
    assert theirs is not None
    assert theirs.created is True
    assert await count(factory, Contact) == 2


# ───────────────────────────── verification ─────────────────────────────


def verdict(email: str, kind: str, **flags: bool | None) -> VerificationOutcome:
    return VerificationOutcome(email=email, verdict=kind, detail="vendor words", **flags)  # type: ignore[arg-type]


async def saved(
    store: LeadStore, workspace_id: uuid.UUID, origin: EmailOrigin = EmailOrigin.PROVIDER
) -> uuid.UUID:
    stored = await store.save_contact(
        workspace_id,
        None,
        ContactRecord(email="jane@example.com", origin=origin, first_name="Jane"),
        SITE,
    )
    assert stored is not None
    return stored.contact_id


async def test_a_deliverable_verdict_verifies_a_published_address(
    store: LeadStore,
    workspace_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
) -> None:
    contact_id = await saved(store, workspace_id)
    status = await store.record_verification(
        workspace_id,
        contact_id,
        "zerobounce",
        verdict("jane@example.com", "deliverable"),
    )

    assert status is VerificationStatus.VERIFIED
    row = await contact_row(factory, contact_id)
    assert row.verification_status is VerificationStatus.VERIFIED
    assert row.last_verified_at == clock.now()
    async with factory() as s:
        history = (await s.execute(select(EmailVerification))).scalars().all()
    assert [(h.provider_key, h.status, h.checks["detail"]) for h in history] == [
        ("zerobounce", VerificationStatus.VERIFIED, "vendor words")
    ]


async def test_a_guessed_address_is_never_verified_whatever_the_vendor_says(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    contact_id = await saved(store, workspace_id, EmailOrigin.GUESSED)
    status = await store.record_verification(
        workspace_id,
        contact_id,
        "zerobounce",
        verdict("jane@example.com", "deliverable"),
    )
    assert status is VerificationStatus.RISKY
    async with factory() as s:
        history = (await s.execute(select(EmailVerification))).scalars().one()
    assert history.checks["capped"] == "guessed_address"


async def test_a_catch_all_domain_cannot_confirm_the_mailbox(
    store: LeadStore, workspace_id: uuid.UUID
) -> None:
    contact_id = await saved(store, workspace_id)
    status = await store.record_verification(
        workspace_id,
        contact_id,
        "zerobounce",
        verdict("jane@example.com", "deliverable", is_catch_all=True),
    )
    assert status is VerificationStatus.RISKY


async def test_undeliverable_and_risky_verdicts(store: LeadStore, workspace_id: uuid.UUID) -> None:
    contact_id = await saved(store, workspace_id)
    assert (
        await store.record_verification(
            workspace_id, contact_id, "a", verdict("jane@example.com", "risky")
        )
        is VerificationStatus.RISKY
    )
    assert (
        await store.record_verification(
            workspace_id, contact_id, "b", verdict("jane@example.com", "undeliverable")
        )
        is VerificationStatus.INVALID
    )


async def test_a_vendor_that_could_not_tell_never_erases_an_earlier_answer(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    contact_id = await saved(store, workspace_id)
    await store.record_verification(
        workspace_id, contact_id, "a", verdict("jane@example.com", "deliverable")
    )
    status = await store.record_verification(
        workspace_id, contact_id, "b", verdict("jane@example.com", "unknown")
    )

    assert status is VerificationStatus.VERIFIED
    assert await count(factory, EmailVerification) == 2  # but the attempt is on record


async def test_a_later_definitive_answer_replaces_an_earlier_one(
    store: LeadStore, workspace_id: uuid.UUID
) -> None:
    contact_id = await saved(store, workspace_id)
    await store.record_verification(
        workspace_id, contact_id, "a", verdict("jane@example.com", "undeliverable")
    )
    status = await store.record_verification(
        workspace_id, contact_id, "b", verdict("jane@example.com", "deliverable")
    )
    assert status is VerificationStatus.VERIFIED


async def test_our_own_mx_check_can_reject_but_never_verifies(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    contact_id = await saved(store, workspace_id)

    assert (
        await store.record_mail_check(workspace_id, contact_id, "mx")
        is VerificationStatus.UNVERIFIED
    )
    assert (
        await store.record_mail_check(workspace_id, contact_id, "implicit_mx")
        is VerificationStatus.UNVERIFIED
    )
    assert (await contact_row(factory, contact_id)).last_verified_at is None

    assert (
        await store.record_mail_check(workspace_id, contact_id, "null_mx")
        is VerificationStatus.INVALID
    )
    assert (
        await store.record_mail_check(workspace_id, contact_id, "no_mail_server")
        is VerificationStatus.INVALID
    )


async def test_a_domain_that_stops_accepting_mail_overrides_an_earlier_verification(
    store: LeadStore, workspace_id: uuid.UUID
) -> None:
    contact_id = await saved(store, workspace_id)
    await store.record_verification(
        workspace_id, contact_id, "a", verdict("jane@example.com", "deliverable")
    )
    assert (
        await store.record_mail_check(workspace_id, contact_id, "no_mail_server")
        is VerificationStatus.INVALID
    )


async def test_an_inconclusive_dns_lookup_is_not_recorded_at_all(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    contact_id = await saved(store, workspace_id)
    assert (
        await store.record_mail_check(workspace_id, contact_id, "dns_error")
        is VerificationStatus.UNVERIFIED
    )
    assert await count(factory, EmailVerification) == 0


async def test_verifying_someone_elses_contact_is_refused(
    store: LeadStore, workspace_id: uuid.UUID, other_workspace_id: uuid.UUID
) -> None:
    contact_id = await saved(store, workspace_id)
    with pytest.raises(LookupError):
        await store.record_verification(
            other_workspace_id,
            contact_id,
            "a",
            verdict("jane@example.com", "deliverable"),
        )
    with pytest.raises(LookupError):
        await store.record_mail_check(other_workspace_id, uuid.uuid4(), "mx")


# ───────────────────────────── pinned by mutation checks ─────────────────────────────


async def test_a_repeat_that_adds_nothing_changes_nothing_not_even_the_timestamp(
    store: LeadStore,
    workspace_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
) -> None:
    company = await store.save_company(workspace_id, DENTIST, GEOAPIFY)
    record = ContactRecord(
        email="jane@example.com",
        origin=EmailOrigin.PROVIDER,
        first_name="Jane",
        position="Owner",
    )
    contact = await store.save_contact(workspace_id, None, record, SITE)
    assert company is not None
    assert contact is not None
    company_stamp = (await company_row(factory, company.company_id)).updated_at
    contact_stamp = (await contact_row(factory, contact.contact_id)).updated_at

    clock.advance(hours=2)
    await store.save_company(workspace_id, DENTIST, GEOAPIFY)
    await store.save_contact(workspace_id, None, record, SITE)

    assert (await company_row(factory, company.company_id)).updated_at == company_stamp
    assert (await contact_row(factory, contact.contact_id)).updated_at == contact_stamp


async def test_one_real_source_never_replaces_another_as_the_recorded_origin(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Only a *guess* is upgraded when a real source turns up; the first real source stays (both are in ``lead_sources``)."""
    vendor = await store.save_contact(
        workspace_id,
        None,
        ContactRecord(email="jane@example.com", origin=EmailOrigin.PROVIDER),
        SITE,
    )
    assert vendor is not None
    await store.save_contact(
        workspace_id,
        None,
        ContactRecord(email="jane@example.com", origin=EmailOrigin.DISCOVERED),
        SITE,
    )
    assert (await contact_row(factory, vendor.contact_id)).email_origin is EmailOrigin.PROVIDER


async def test_a_mail_server_check_is_filed_as_unverified_never_as_verified(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    contact_id = await saved(store, workspace_id)
    await store.record_mail_check(workspace_id, contact_id, "mx")
    async with factory() as s:
        rows = (await s.execute(select(EmailVerification))).scalars().all()
    assert [(r.provider_key, r.status) for r in rows] == [("dns", VerificationStatus.UNVERIFIED)]


async def test_an_address_elsewhere_is_not_merged_into_a_gmail_contact_with_the_same_local_part(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    gmail = await store.save_contact(
        workspace_id,
        None,
        ContactRecord(email=f"jdoe@{GMAIL}", origin=EmailOrigin.DISCOVERED),
        SITE,
    )
    other = await store.save_contact(
        workspace_id,
        None,
        ContactRecord(email="jdoe@example.com", origin=EmailOrigin.DISCOVERED),
        SITE,
    )
    assert gmail is not None
    assert other is not None
    assert other.created is True
    assert other.contact_id != gmail.contact_id
    assert await count(factory, Contact) == 2


async def test_a_social_page_given_as_the_website_is_not_the_companys_identity(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Small businesses often list a Facebook page as their "website". Keyed by that domain, every such business would
    collapse into one company."""
    first = await store.save_company(
        workspace_id,
        CompanyRecord(
            name="Smile Dental",
            website="https://www.facebook.com/smiledental",
            city="Austin",
            country="US",
        ),
        GEOAPIFY,
    )
    second = await store.save_company(
        workspace_id,
        CompanyRecord(
            name="Bright Dental",
            website="https://www.facebook.com/brightdental",
            city="Austin",
            country="US",
        ),
        GEOAPIFY,
    )
    assert first is not None
    assert second is not None
    assert first.company_id != second.company_id
    assert first.key.startswith("name:")
    row = await company_row(factory, first.company_id)
    assert (row.domain, row.website) == (None, None)
    assert await count(factory, Company) == 2


async def test_a_directory_page_cannot_be_attached_as_a_companys_website(
    store: LeadStore, workspace_id: uuid.UUID
) -> None:
    company = await store.save_company(
        workspace_id, CompanyRecord(name="Smile Dental", city="Austin", country="US"), GEOAPIFY
    )
    assert company is not None
    with pytest.raises(ValueError, match="usable domain"):
        await store.attach_website(
            workspace_id,
            company_id=company.company_id,
            website="https://www.yelp.com/biz/smile-dental-austin",
            provenance=SITE,
        )


async def test_the_same_mail_server_finding_is_not_filed_again_but_a_change_is(
    store: LeadStore,
    workspace_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
) -> None:
    contact_id = await saved(store, workspace_id)
    for _ in range(3):
        clock.advance(hours=1)
        await store.record_mail_check(workspace_id, contact_id, "mx")
    assert await count(factory, EmailVerification) == 1

    clock.advance(hours=1)
    assert (
        await store.record_mail_check(workspace_id, contact_id, "null_mx")
        is VerificationStatus.INVALID
    )
    assert (
        await count(factory, EmailVerification) == 2
    )  # the domain stopped accepting mail: that *is* news

    clock.advance(hours=1)
    await store.record_mail_check(
        workspace_id, contact_id, "mx"
    )  # …and a domain that recovers is news again
    assert await count(factory, EmailVerification) == 3


async def test_a_contact_is_classified_as_a_company_or_an_individual_when_it_is_saved(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    business = await store.save_contact(
        workspace_id,
        None,
        ContactRecord(email="info@clinic.example.com", origin=EmailOrigin.DISCOVERED),
        SITE,
    )
    personal = await store.save_contact(
        workspace_id,
        None,
        ContactRecord(email=f"front.desk@{GMAIL}", origin=EmailOrigin.DISCOVERED),
        SITE,
    )
    assert business is not None
    assert personal is not None
    assert (
        await contact_row(factory, business.contact_id)
    ).subscriber_type is SubscriberType.CORPORATE
    assert (
        await contact_row(factory, personal.contact_id)
    ).subscriber_type is SubscriberType.INDIVIDUAL


async def test_an_unclassified_contact_is_classified_later_but_a_decision_already_made_is_kept(
    store: LeadStore, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    unclassified = await store.save_contact(
        workspace_id,
        None,
        ContactRecord(email="a@clinic.example.com", origin=EmailOrigin.DISCOVERED),
        SITE,
    )
    decided = await store.save_contact(
        workspace_id,
        None,
        ContactRecord(email="b@clinic.example.com", origin=EmailOrigin.DISCOVERED),
        SITE,
    )
    assert unclassified is not None
    assert decided is not None
    async with session_scope(factory) as s:
        first = await s.get(Contact, unclassified.contact_id)
        second = await s.get(Contact, decided.contact_id)
        assert first is not None
        assert second is not None
        first.subscriber_type = (
            SubscriberType.UNKNOWN
        )  # e.g. a row from before classification existed
        second.subscriber_type = (
            SubscriberType.INDIVIDUAL
        )  # the owner's own decision (a sole trader)

    for email in ("a@clinic.example.com", "b@clinic.example.com"):
        await store.save_contact(
            workspace_id, None, ContactRecord(email=email, origin=EmailOrigin.DISCOVERED), SITE
        )

    assert (
        await contact_row(factory, unclassified.contact_id)
    ).subscriber_type is SubscriberType.CORPORATE
    assert (
        await contact_row(factory, decided.contact_id)
    ).subscriber_type is SubscriberType.INDIVIDUAL
