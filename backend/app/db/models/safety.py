"""Suppression list, jurisdiction policies and consent records (REQUIREMENTS §13)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, WorkspaceScopedMixin
from app.db.types import enum_col
from app.domain.enums import ConsentPolicy, LawfulBasis, SuppressionReason


class SuppressionEntry(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Central do-not-contact list. An entry is either an exact email or a whole domain."""

    __tablename__ = "suppression_list"
    __table_args__ = (
        CheckConstraint("email IS NOT NULL OR domain IS NOT NULL", name="target_present"),
        Index(
            "uq_suppression_workspace_email",
            "workspace_id",
            "email",
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
        Index(
            "uq_suppression_workspace_domain",
            "workspace_id",
            "domain",
            unique=True,
            postgresql_where=text("email IS NULL AND domain IS NOT NULL"),
        ),
    )

    email: Mapped[str | None] = mapped_column(String(320))
    domain: Mapped[str | None] = mapped_column(String(255))
    reason: Mapped[SuppressionReason] = mapped_column(enum_col(SuppressionReason))
    source: Mapped[str] = mapped_column(
        String(64)
    )  # reply | unsubscribe_link | bounce | manual | import
    note: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None]


class JurisdictionPolicy(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Per-country sending rules. Data, not code, so the owner can review and adjust them.

    Defaults are conservative and are NOT legal advice (docs/COMPLIANCE.md, ADR-010).
    """

    __tablename__ = "jurisdiction_policies"
    __table_args__ = (UniqueConstraint("workspace_id", "country"),)

    country: Mapped[str] = mapped_column(String(2))  # ISO alpha-2, or "ZZ" for the fallback rule
    cold_b2b_marketing: Mapped[ConsentPolicy] = mapped_column(
        enum_col(ConsentPolicy, name="consentpolicy_b2b")
    )
    cold_to_individuals: Mapped[ConsentPolicy] = mapped_column(
        enum_col(ConsentPolicy, name="consentpolicy_individual")
    )
    requires_unsubscribe: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    requires_postal_address: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    requires_sender_identification: Mapped[bool] = mapped_column(
        default=True, server_default=text("true")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    reviewed_on: Mapped[date | None] = mapped_column(Date)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))


class ConsentRecord(Base, UUIDPrimaryKeyMixin, WorkspaceScopedMixin):
    """Evidence for the lawful basis of contacting someone."""

    __tablename__ = "consent_records"

    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    basis: Mapped[LawfulBasis] = mapped_column(enum_col(LawfulBasis, name="lawfulbasis_record"))
    evidence_url: Mapped[str | None] = mapped_column(Text)
    evidence_note: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
