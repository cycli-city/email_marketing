"""Persisting discovered companies and contacts without ever creating a duplicate (ADR-028, ADR-040).

Three rules shape everything here:

* **One company per registrable domain** (else per normalised name + city + country), **one contact per address**, per
  workspace. The unique constraints are the last line of defence; the code below finds the existing row first and uses
  ``ON CONFLICT DO NOTHING`` so two concurrent discoveries of the same business cannot both insert it.
* **Merging only fills gaps.** A later search may add a phone number or a website to a company we already hold; it never
  overwrites something we already know, so a noisy vendor cannot degrade good data.
* **What we know about an address is separate from how we got it.** ``record_verification`` and ``record_mail_check`` are
  the only code that sets a verification status, and neither can produce ``VERIFIED`` for a guessed address, a catch-all
  domain, or from our own MX lookup alone.

Everything arriving here came from a vendor or a web page, so it is untrusted: text is whitespace-normalised and clipped to
its column, country codes are validated, and nothing is interpreted as an instruction.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.db.models import Company, Contact, EmailVerification, LeadSource
from app.db.session import session_scope
from app.domain.enums import EmailKind, EmailOrigin, SubscriberType, VerificationStatus
from app.domain.identity import (
    company_key,
    email_match_key,
    is_disposable,
    is_role_address,
    normalise_email,
    registrable_domain,
    subscriber_type_for,
    website_origin,
)
from app.domain.site_match import is_aggregator
from app.integrations.dns_checks import MailStatus, usable_for_mail
from app.providers.discovery import VerificationOutcome

_WHITESPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_GMAIL_DOMAINS = ("gmail.com", "googlemail.com")


def clean(value: str | None, limit: int) -> str | None:
    """Untrusted text → single-spaced, control-character-free, clipped to the column; ``None`` if nothing is left."""
    if not value:
        return None
    collapsed = _WHITESPACE.sub(" ", _CONTROL.sub(" ", value)).strip()
    return collapsed[:limit].rstrip() or None


def clean_country(value: str | None) -> str | None:
    code = (value or "").strip().upper()
    return code if len(code) == 2 and code.isalpha() else None


# ───────────────────────────── inputs and results ─────────────────────────────


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a record came from — stored with a timestamp so every lead can answer "how did we get this?"."""

    provider_key: str
    source_kind: str  # provider_api | public_website | directory | manual | import
    source_url: str | None = None
    payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CompanyRecord:
    name: str | None
    website: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None  # ISO 3166-1 alpha-2
    industry: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ContactRecord:
    email: str
    origin: EmailOrigin
    first_name: str | None = None
    last_name: str | None = None
    position: str | None = None
    generic: bool = False  # the vendor says this is a shared/role mailbox
    source_url: str | None = None
    country: str | None = None


@dataclass(frozen=True, slots=True)
class CompanyStored:
    company_id: uuid.UUID
    key: str
    created: bool  # False → we already held this company (a duplicate; nothing was duplicated)


@dataclass(frozen=True, slots=True)
class ContactStored:
    contact_id: uuid.UUID
    email: str
    created: bool
    kind: EmailKind
    status: VerificationStatus


@dataclass(frozen=True, slots=True)
class CompanyView:
    """What the enrichment needs to know about a stored company."""

    company_id: uuid.UUID
    name: str
    website: str | None
    domain: str | None
    phone: str | None
    city: str | None
    country: str | None


@dataclass(frozen=True, slots=True)
class ContactView:
    contact_id: uuid.UUID
    email: str
    kind: EmailKind
    email_origin: EmailOrigin
    status: VerificationStatus


# ───────────────────────────── verdict rules (pure) ─────────────────────────────


def status_for_outcome(outcome: VerificationOutcome, origin: EmailOrigin) -> VerificationStatus:
    """A vendor's verdict → our status. ``VERIFIED`` is reserved for an address a vendor calls deliverable that is *not*
    guessed, *not* on a catch-all domain (the vendor cannot confirm the mailbox exists) and *not* disposable."""
    if outcome.verdict == "undeliverable":
        return VerificationStatus.INVALID
    if outcome.verdict == "risky":
        return VerificationStatus.RISKY
    if outcome.verdict == "unknown":
        return VerificationStatus.UNVERIFIED
    # deliverable
    if origin is EmailOrigin.GUESSED or outcome.is_catch_all or outcome.is_disposable:
        return VerificationStatus.RISKY
    if is_disposable(outcome.email):
        return VerificationStatus.RISKY
    return VerificationStatus.VERIFIED


def kind_for(email: str, *, generic: bool, has_name: bool) -> EmailKind:
    if generic or is_role_address(email):
        return EmailKind.ROLE_BASED
    return EmailKind.NAMED if has_name else EmailKind.UNKNOWN


_PROVENANCE_RANK = {
    EmailOrigin.GUESSED: 0,
    EmailOrigin.IMPORTED: 1,
    EmailOrigin.MANUAL: 2,
    EmailOrigin.PROVIDER: 3,
    EmailOrigin.DISCOVERED: 4,
}


# ───────────────────────────── the store ─────────────────────────────


class LeadStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._factory = session_factory
        self._clock = clock

    # ── companies ──

    async def save_company(
        self, workspace_id: uuid.UUID, record: CompanyRecord, provenance: Provenance
    ) -> CompanyStored | None:
        """Insert the company or merge into the one we already hold. ``None`` when there is nothing to identify it by
        (no usable website and no name)."""
        now = self._clock.now()
        # A Facebook page, a Yelp listing or a Google profile given as a business's "website" is not its website: using its
        # domain as the identity would merge every business that has one into a single company.
        website = None if is_aggregator(record.website) else record.website
        domain = registrable_domain(website)
        name = clean(record.name, 300) or domain
        city = clean(record.city, 120)
        country = clean_country(record.country)
        key = company_key(website=website, name=name, city=city, country=country)
        if key is None or name is None:
            return None
        fields: dict[str, Any] = {
            "domain": domain,
            "website": website_origin(website),
            "phone": clean(record.phone, 64),
            "address": clean(record.address, 2000),
            "city": city,
            "region": clean(record.region, 120),
            "country": country,
            "industry": clean(record.industry, 120),
            "description": clean(record.description, 4000),
        }
        async with session_scope(self._factory) as s:
            company = await self._find_company(s, workspace_id, key)
            if company is None and domain is not None:
                company = await self._adopt_unsited(
                    s,
                    workspace_id,
                    name=name,
                    city=city,
                    country=country,
                    domain_key=key,
                    domain=domain,
                )
            created = False
            if company is None:
                inserted = (
                    await s.execute(
                        pg_insert(Company)
                        .values(
                            id=uuid.uuid4(),
                            workspace_id=workspace_id,
                            name=name,
                            dedupe_key=key,
                            discovered_at=now,
                            created_at=now,
                            updated_at=now,
                            **fields,
                        )
                        .on_conflict_do_nothing(index_elements=["workspace_id", "dedupe_key"])
                        .returning(Company.id)
                    )
                ).scalar_one_or_none()
                if inserted is not None:
                    created = True
                    company_id = inserted
                else:  # lost a race with a concurrent discovery of the same business
                    company = await self._find_company(s, workspace_id, key)
                    assert company is not None
            if not created:
                assert company is not None
                company_id = company.id
                if self._fill_gaps(company, fields):
                    company.updated_at = now
            await self._record_source(
                s, workspace_id, "company", company_id, provenance=provenance, now=now
            )
            return CompanyStored(company_id=company_id, key=key, created=created)

    @staticmethod
    async def _find_company(s: AsyncSession, workspace_id: uuid.UUID, key: str) -> Company | None:
        return (
            await s.execute(
                select(Company)
                .where(Company.workspace_id == workspace_id, Company.dedupe_key == key)
                .with_for_update()
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _adopt_unsited(
        s: AsyncSession,
        workspace_id: uuid.UUID,
        *,
        name: str,
        city: str | None,
        country: str | None,
        domain_key: str,
        domain: str,
    ) -> Company | None:
        """A business first found without a website (keyed by name + city) that now turns up *with* one is the same
        business: re-key it to its domain instead of creating a second company."""
        name_key = company_key(website=None, name=name, city=city, country=country)
        if name_key is None or name_key == domain_key:
            return None
        found = (
            await s.execute(
                select(Company)
                .where(
                    Company.workspace_id == workspace_id,
                    Company.dedupe_key == name_key,
                    Company.domain.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if found is not None:
            found.dedupe_key = domain_key
            found.domain = domain
        return found

    @staticmethod
    def _fill_gaps(row: Any, values: dict[str, Any]) -> bool:
        """Set only the attributes that are empty. Returns whether anything changed."""
        changed = False
        for attribute, value in values.items():
            if value is not None and getattr(row, attribute) in (None, ""):
                setattr(row, attribute, value)
                changed = True
        return changed

    async def company_view(
        self, workspace_id: uuid.UUID, company_id: uuid.UUID
    ) -> CompanyView | None:
        async with self._factory() as s:
            row = (
                await s.execute(
                    select(Company).where(
                        Company.workspace_id == workspace_id, Company.id == company_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return CompanyView(
                company_id=row.id,
                name=row.name,
                website=row.website,
                domain=row.domain,
                phone=row.phone,
                city=row.city,
                country=row.country,
            )

    async def mark_researched(self, workspace_id: uuid.UUID, company_id: uuid.UUID) -> None:
        now = self._clock.now()
        async with session_scope(self._factory) as s:
            company = (
                await s.execute(
                    select(Company)
                    .where(Company.workspace_id == workspace_id, Company.id == company_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if company is not None:
                company.last_researched_at = now
                company.research_error = None
                company.updated_at = now

    async def attach_website(
        self,
        workspace_id: uuid.UUID,
        *,
        company_id: uuid.UUID,
        website: str,
        provenance: Provenance,
    ) -> uuid.UUID | None:
        """Give a company that has no website the one we found for it.

        Returns ``None`` on success. If *another* company already holds this domain the two records are the same business:
        nothing is changed and that company's id is returned, so the caller can report a duplicate. Merging two records is a
        decision, never a side effect.
        """
        domain = registrable_domain(website)
        origin = website_origin(website)
        if domain is None or origin is None or is_aggregator(website):
            raise ValueError("the website has no usable domain of its own")
        now = self._clock.now()
        async with session_scope(self._factory) as s:
            holder = await self._find_company(s, workspace_id, domain)
            if holder is not None and holder.id != company_id:
                return holder.id
            company = (
                await s.execute(
                    select(Company)
                    .where(Company.workspace_id == workspace_id, Company.id == company_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if company is None:
                raise LookupError(f"company {company_id} not found in this workspace")
            if company.domain is None:
                company.domain = domain
                company.website = origin
                company.dedupe_key = domain
                company.updated_at = now
            await self._record_source(
                s, workspace_id, "company", company_id, provenance=provenance, now=now
            )
            return None

    # ── contacts ──

    async def contacts_of(
        self, workspace_id: uuid.UUID, company_id: uuid.UUID
    ) -> list[ContactView]:
        async with self._factory() as s:
            rows = (
                await s.execute(
                    select(Contact)
                    .where(
                        Contact.workspace_id == workspace_id,
                        Contact.company_id == company_id,
                        Contact.email.is_not(None),
                    )
                    .order_by(Contact.email)
                )
            ).scalars()
            return [
                ContactView(
                    contact_id=c.id,
                    email=c.email,
                    kind=c.email_kind,
                    email_origin=c.email_origin,
                    status=c.verification_status,
                )
                for c in rows
                if c.email is not None
            ]

    async def save_contact(
        self,
        workspace_id: uuid.UUID,
        company_id: uuid.UUID | None,
        record: ContactRecord,
        provenance: Provenance,
    ) -> ContactStored | None:
        """Insert the contact or merge into the one already holding this address. ``None`` for an invalid address."""
        email = normalise_email(record.email)
        if email is None:
            return None
        now = self._clock.now()
        first = clean(record.first_name, 150)
        last = clean(record.last_name, 150)
        full = clean(" ".join(part for part in (first, last) if part), 300)
        kind = kind_for(email, generic=record.generic, has_name=full is not None)
        subscriber = subscriber_type_for(email)
        fields: dict[str, Any] = {
            "company_id": company_id,
            "full_name": full,
            "first_name": first,
            "last_name": last,
            "position": clean(record.position, 300),
            "source_url": clean(record.source_url, 2000),
            "country": clean_country(record.country),
        }
        async with session_scope(self._factory) as s:
            contact = await self._find_contact(s, workspace_id, email)
            created = False
            if contact is None:
                inserted = (
                    await s.execute(
                        pg_insert(Contact)
                        .values(
                            id=uuid.uuid4(),
                            workspace_id=workspace_id,
                            email=email,
                            email_kind=kind,
                            email_origin=record.origin,
                            subscriber_type=subscriber,
                            verification_status=VerificationStatus.UNVERIFIED,
                            discovered_at=now,
                            created_at=now,
                            updated_at=now,
                            **fields,
                        )
                        .on_conflict_do_nothing(
                            index_elements=["workspace_id", "email"],
                            index_where=text("email IS NOT NULL"),
                        )
                        .returning(Contact.id)
                    )
                ).scalar_one_or_none()
                if inserted is not None:
                    created = True
                    contact_id = inserted
                else:  # a concurrent discovery stored this address first
                    contact = await self._find_contact(s, workspace_id, email)
                    assert contact is not None
            if not created:
                assert contact is not None
                contact_id = contact.id
                changed = self._fill_gaps(contact, fields)
                if contact.email_kind is EmailKind.UNKNOWN and kind is not EmailKind.UNKNOWN:
                    contact.email_kind = kind
                    changed = True
                if contact.subscriber_type is SubscriberType.UNKNOWN:
                    contact.subscriber_type = subscriber
                    changed = changed or subscriber is not SubscriberType.UNKNOWN
                # An address once *guessed* that a real source now publishes is no longer a guess; never the reverse.
                if (
                    _PROVENANCE_RANK[record.origin] > _PROVENANCE_RANK[contact.email_origin]
                    and contact.email_origin is EmailOrigin.GUESSED
                ):
                    contact.email_origin = record.origin
                    changed = True
                if changed:
                    contact.updated_at = now
            await self._record_source(
                s, workspace_id, "contact", contact_id, provenance=provenance, now=now
            )
            final = await s.get(Contact, contact_id)
            assert final is not None
            return ContactStored(
                contact_id=contact_id,
                email=email,
                created=created,
                kind=final.email_kind,
                status=final.verification_status,
            )

    @staticmethod
    async def _find_contact(s: AsyncSession, workspace_id: uuid.UUID, email: str) -> Contact | None:
        """The contact holding this address — or, for Gmail, any spelling of it (dots and ``+tags`` are ignored there)."""
        exact = (
            await s.execute(
                select(Contact)
                .where(Contact.workspace_id == workspace_id, Contact.email == email)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if exact is not None:
            return exact
        wanted = email_match_key(email)
        if wanted is None or not wanted.endswith("@gmail.com"):
            return None
        local = func.replace(
            func.split_part(func.split_part(Contact.email, "@", 1), "+", 1), ".", ""
        )
        return (
            (
                await s.execute(
                    select(Contact)
                    .where(
                        Contact.workspace_id == workspace_id,
                        func.split_part(Contact.email, "@", 2).in_(_GMAIL_DOMAINS),
                        local == wanted.split("@", 1)[0],
                    )
                    .order_by(Contact.created_at)
                    .limit(1)
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )

    # ── verification ──

    async def record_verification(
        self,
        workspace_id: uuid.UUID,
        contact_id: uuid.UUID,
        provider_key: str,
        outcome: VerificationOutcome,
    ) -> VerificationStatus:
        """File a vendor's verdict and update the contact. A vendor that merely *could not tell* (``unknown``) is kept in
        the history but never erases an earlier definitive result."""
        now = self._clock.now()
        async with session_scope(self._factory) as s:
            contact = await self._locked_contact(s, workspace_id, contact_id)
            status = status_for_outcome(outcome, contact.email_origin)
            checks: dict[str, Any] = {
                "detail": clean(outcome.detail, 200),
                "verdict": outcome.verdict,
                "catch_all": outcome.is_catch_all,
                "disposable": outcome.is_disposable,
                "role": outcome.is_role,
                "free_provider": outcome.is_free_provider,
            }
            if outcome.verdict == "deliverable" and contact.email_origin is EmailOrigin.GUESSED:
                checks["capped"] = "guessed_address"
            s.add(
                EmailVerification(
                    workspace_id=workspace_id,
                    contact_id=contact.id,
                    email=contact.email or "",
                    provider_key=provider_key,
                    status=status,
                    checks=checks,
                    checked_at=now,
                )
            )
            if outcome.verdict != "unknown":
                self._apply_status(contact, status, now)
            return contact.verification_status

    async def record_mail_check(
        self, workspace_id: uuid.UUID, contact_id: uuid.UUID, mail: MailStatus
    ) -> VerificationStatus:
        """File our own free DNS check. A domain that cannot receive mail makes the address ``INVALID``; a domain that can
        proves nothing about the mailbox, so it **never** yields ``VERIFIED``. An inconclusive lookup is not recorded."""
        usable = usable_for_mail(mail)
        async with session_scope(self._factory) as s:
            contact = await self._locked_contact(s, workspace_id, contact_id)
            if usable is None:
                return contact.verification_status
            now = self._clock.now()
            status = VerificationStatus.UNVERIFIED if usable else VerificationStatus.INVALID
            latest = (
                await s.execute(
                    select(EmailVerification.checks)
                    .where(
                        EmailVerification.contact_id == contact.id,
                        EmailVerification.provider_key == "dns",
                    )
                    .order_by(EmailVerification.checked_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if (
                latest is None or latest.get("mail_status") != mail
            ):  # the same finding twice is not news
                s.add(
                    EmailVerification(
                        workspace_id=workspace_id,
                        contact_id=contact.id,
                        email=contact.email or "",
                        provider_key="dns",
                        status=status,
                        checks={"mail_status": mail},
                        checked_at=now,
                    )
                )
            if not usable:
                self._apply_status(contact, VerificationStatus.INVALID, now)
            return contact.verification_status

    @staticmethod
    def _apply_status(contact: Contact, status: VerificationStatus, now: datetime) -> None:
        guessed_verified = (
            contact.email_origin is EmailOrigin.GUESSED and status is VerificationStatus.VERIFIED
        )
        if guessed_verified:  # the database CHECK forbids it; refuse before it ever gets that far
            status = VerificationStatus.RISKY
        contact.verification_status = status
        contact.last_verified_at = now
        contact.updated_at = now

    @staticmethod
    async def _locked_contact(
        s: AsyncSession, workspace_id: uuid.UUID, contact_id: uuid.UUID
    ) -> Contact:
        contact = (
            await s.execute(
                select(Contact)
                .where(Contact.workspace_id == workspace_id, Contact.id == contact_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if contact is None:
            raise LookupError(f"contact {contact_id} not found in this workspace")
        return contact

    # ── provenance ──

    @staticmethod
    async def _record_source(
        s: AsyncSession,
        workspace_id: uuid.UUID,
        entity_type: str,
        entity_id: uuid.UUID,
        *,
        provenance: Provenance,
        now: datetime,
    ) -> None:
        """One provenance row per (record, provider, page): finding the same business again via the same page adds nothing."""
        url = clean(provenance.source_url, 2000)
        existing = (
            await s.execute(
                select(LeadSource.id).where(
                    LeadSource.workspace_id == workspace_id,
                    LeadSource.entity_type == entity_type,
                    LeadSource.entity_id == entity_id,
                    LeadSource.provider_key == provenance.provider_key,
                    LeadSource.source_url.is_(None)
                    if url is None
                    else LeadSource.source_url == url,
                )
            )
        ).first()
        if existing is None:
            s.add(
                LeadSource(
                    workspace_id=workspace_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    provider_key=provenance.provider_key,
                    source_kind=provenance.source_kind,
                    source_url=url,
                    retrieved_at=now,
                    payload=provenance.payload or {},
                )
            )
