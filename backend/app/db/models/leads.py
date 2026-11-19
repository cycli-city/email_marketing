"""Companies, contacts, leads (campaign membership), provenance, verification history and grounding facts."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, WorkspaceScopedMixin
from app.db.types import enum_col
from app.domain.enums import (
    EmailKind,
    EmailOrigin,
    FactKind,
    FollowUpStatus,
    LawfulBasis,
    LeadStage,
    ReplyCategory,
    SubscriberType,
    VerificationStatus,
)

_EMPTY_OBJ = text("'{}'::jsonb")
_EMPTY_ARR = text("'[]'::jsonb")


class Company(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """A business or employer. Deduplicated per workspace by ``dedupe_key`` (registrable domain, else name+city)."""

    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint("workspace_id", "dedupe_key"),
        Index("ix_companies_workspace_domain", "workspace_id", "domain"),
        Index("ix_companies_search_tsv", "search_tsv", postgresql_using="gin"),
    )

    name: Mapped[str] = mapped_column(String(300))
    domain: Mapped[str | None] = mapped_column(String(255))
    website: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(400))
    industry: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str | None] = mapped_column(String(2))  # ISO 3166-1 alpha-2
    region: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(64))
    size_hint: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    last_researched_at: Mapped[datetime | None]
    research_error: Mapped[str | None] = mapped_column(Text)
    search_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(name, '') || ' ' || coalesce(domain, '') || ' ' || "
            "coalesce(industry, '') || ' ' || coalesce(city, '') || ' ' || coalesce(country, ''))",
            persisted=True,
        ),
        deferred=True,
    )


class Contact(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """A person/mailbox at a company.

    ``email_origin`` (how we got it) and ``verification_status`` (what we proved) are separate on
    purpose: *discovered, guessed, unverified, risky, verified* are all expressible and a guessed
    address can never be marked verified (DB check).
    """

    __tablename__ = "contacts"
    __table_args__ = (
        Index(
            "uq_contacts_workspace_email",
            "workspace_id",
            "email",
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
        Index("ix_contacts_company", "company_id"),
        Index("ix_contacts_search_tsv", "search_tsv", postgresql_using="gin"),
        CheckConstraint(
            "NOT (email_origin = 'guessed' AND verification_status = 'verified')",
            name="guessed_never_verified",
        ),
    )

    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE")
    )
    full_name: Mapped[str | None] = mapped_column(String(300))
    first_name: Mapped[str | None] = mapped_column(String(150))
    last_name: Mapped[str | None] = mapped_column(String(150))
    position: Mapped[str | None] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(320))  # lower-cased + trimmed
    email_kind: Mapped[EmailKind] = mapped_column(enum_col(EmailKind), default=EmailKind.UNKNOWN)
    email_origin: Mapped[EmailOrigin] = mapped_column(
        enum_col(EmailOrigin), default=EmailOrigin.DISCOVERED
    )
    verification_status: Mapped[VerificationStatus] = mapped_column(
        enum_col(VerificationStatus), default=VerificationStatus.UNVERIFIED
    )
    last_verified_at: Mapped[datetime | None]
    role_relevance: Mapped[int | None] = mapped_column(SmallInteger)  # 0-100 target-role match
    subscriber_type: Mapped[SubscriberType] = mapped_column(
        enum_col(SubscriberType), default=SubscriberType.UNKNOWN
    )
    lawful_basis: Mapped[LawfulBasis] = mapped_column(
        enum_col(LawfulBasis), default=LawfulBasis.NONE
    )
    country: Mapped[str | None] = mapped_column(String(2))
    source_url: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    notes: Mapped[str | None] = mapped_column(Text)
    search_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(full_name, '') || ' ' || coalesce(position, '') || ' ' || "
            "coalesce(email, ''))",
            persisted=True,
        ),
        deferred=True,
    )


class Lead(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """A company inside a campaign — the unit that moves through the pipeline (ARCHITECTURE §3.1)."""

    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("campaign_id", "company_id"),
        CheckConstraint("score IS NULL OR score BETWEEN 0 AND 100", name="score_range"),
        Index("ix_leads_campaign_stage", "campaign_id", "stage"),
        Index(
            "ix_leads_parked_until",
            "parked_until",
            postgresql_where=text("parked_until IS NOT NULL"),
        ),
    )

    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    primary_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    stage: Mapped[LeadStage] = mapped_column(enum_col(LeadStage), default=LeadStage.DISCOVERED)
    stage_updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    # Parked (WAITING_PROVIDER etc.): the stage is preserved so work resumes where it stopped.
    parked_until: Mapped[datetime | None]
    parked_reason: Mapped[str | None] = mapped_column(Text)
    claimed_by: Mapped[str | None] = mapped_column(String(80))
    claimed_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)

    score: Mapped[int | None] = mapped_column(SmallInteger)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_OBJ
    )
    qualification_explanation: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_ARR
    )
    outreach_angle: Mapped[str | None] = mapped_column(Text)
    ai_summary: Mapped[str | None] = mapped_column(Text)
    disqualified_reason: Mapped[str | None] = mapped_column(Text)

    last_contacted_at: Mapped[datetime | None]
    last_reply_at: Mapped[datetime | None]
    reply_category: Mapped[ReplyCategory | None] = mapped_column(enum_col(ReplyCategory))
    followup_status: Mapped[FollowUpStatus] = mapped_column(
        enum_col(FollowUpStatus), default=FollowUpStatus.NONE
    )
    stop_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


class LeadSource(Base, UUIDPrimaryKeyMixin, WorkspaceScopedMixin):
    """Provenance: every provider/page that contributed a company or contact, with timestamp."""

    __tablename__ = "lead_sources"
    __table_args__ = (
        CheckConstraint("entity_type IN ('company', 'contact')", name="entity_type_valid"),
        Index("ix_lead_sources_entity", "entity_type", "entity_id"),
    )

    entity_type: Mapped[str] = mapped_column(String(16))
    entity_id: Mapped[uuid.UUID]
    provider_key: Mapped[str] = mapped_column(String(64))
    source_kind: Mapped[str] = mapped_column(
        String(32)
    )  # provider_api | public_website | directory | manual | import
    source_url: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_OBJ)


class EmailVerification(Base, UUIDPrimaryKeyMixin, WorkspaceScopedMixin):
    """History of verification attempts (the contact row keeps only the latest status)."""

    __tablename__ = "email_verifications"
    __table_args__ = (Index("ix_email_verifications_contact_time", "contact_id", "checked_at"),)

    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(320))
    provider_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[VerificationStatus] = mapped_column(enum_col(VerificationStatus))
    score: Mapped[int | None] = mapped_column(SmallInteger)
    checks: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=_EMPTY_OBJ)
    checked_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Fact(Base, UUIDPrimaryKeyMixin, WorkspaceScopedMixin):
    """A grounded statement about a company, with the verbatim quote that proves it (ARCHITECTURE §8)."""

    __tablename__ = "facts"
    __table_args__ = (UniqueConstraint("company_id", "content_hash"),)

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    kind: Mapped[FactKind] = mapped_column(enum_col(FactKind), default=FactKind.OTHER)
    # NB: not called ``text`` — that would shadow sqlalchemy.text inside this class body.
    statement: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    quote: Mapped[str] = mapped_column(Text)
    quote_verified: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    content_hash: Mapped[str] = mapped_column(String(64))
    extracted_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
