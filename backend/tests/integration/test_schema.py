"""Schema guarantees enforced by the database itself (real PostgreSQL)."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from enum import Enum

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.base import Base
from app.db.models import AuditLog, Company, Contact, EmailMessage, Lead
from app.domain.enums import (
    ActorType,
    EmailOrigin,
    MessageKind,
    VerificationStatus,
)
from tests.conftest import run_alembic
from tests.factories import (
    make_campaign,
    make_company,
    make_contact,
    make_lead,
    make_workspace,
)

# ───────────────────────────── migrations ─────────────────────────────


def test_migrations_round_trip_and_match_models(fresh_database: str) -> None:
    """upgrade → check (no model/migration drift) → downgrade → upgrade."""
    for args in (
        ("upgrade", "head"),
        ("check",),
        ("downgrade", "base"),
        ("upgrade", "head"),
    ):
        result = run_alembic(fresh_database, *args)
        assert result.returncode == 0, (
            f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
        )


async def test_every_table_has_row_level_security(session: AsyncSession) -> None:
    rows = await session.execute(
        sa.text(
            "SELECT c.relname, c.relrowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname <> 'alembic_version'"
        )
    )
    missing = [name for name, enabled in rows if not enabled]
    assert not missing, (
        f"RLS not enabled on: {missing} — call enable_rls() in the migration (ADR-004)"
    )


async def test_all_model_tables_exist(session: AsyncSession) -> None:
    rows = await session.execute(
        sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    )
    existing = {r[0] for r in rows}
    assert set(Base.metadata.tables) <= existing


async def test_enum_check_constraints_match_python_enums(session: AsyncSession) -> None:
    """VARCHAR+CHECK enums must never drift from the Python enums (alembic can't detect this)."""
    problems: list[str] = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if not isinstance(column.type, sa.Enum) or column.type.enum_class is None:
                continue
            enum_cls: type[Enum] = column.type.enum_class
            rows = await session.execute(
                sa.text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE contype = 'c' AND conrelid = CAST(:t AS regclass)"
                ),
                {"t": f"public.{table.name}"},
            )
            # (?<![\w]) stops "mode" from matching inside "approval_mode", "category" inside "owner_override_category"
            pattern = rf"(?<!\w)\(*\"?{re.escape(column.name)}\"?\)*::text = ANY"
            defs = [r[0] for r in rows if re.search(pattern, r[0])]
            if len(defs) != 1:
                problems.append(f"{table.name}.{column.name}: expected 1 CHECK, found {len(defs)}")
                continue
            db_values = set(re.findall(r"'([^']+)'::character varying", defs[0]))
            py_values = {m.value for m in enum_cls}
            if db_values != py_values:
                problems.append(
                    f"{table.name}.{column.name}: db={sorted(db_values)} py={sorted(py_values)}"
                )
    assert not problems, "\n".join(problems)


# ───────────────────────────── data-integrity rules ─────────────────────────────


async def test_guessed_email_can_never_be_verified(session: AsyncSession) -> None:
    ws = await make_workspace(session)
    with pytest.raises(IntegrityError, match="guessed_never_verified"):
        await make_contact(
            session,
            ws,
            email="guess@example.com",
            email_origin=EmailOrigin.GUESSED,
            verification_status=VerificationStatus.VERIFIED,
        )


async def test_guessed_email_can_be_unverified_or_risky(session: AsyncSession) -> None:
    ws = await make_workspace(session)
    c = await make_contact(
        session,
        ws,
        email="guess@example.com",
        email_origin=EmailOrigin.GUESSED,
        verification_status=VerificationStatus.RISKY,
    )
    assert c.id


async def test_contact_email_unique_per_workspace_but_nulls_allowed(session: AsyncSession) -> None:
    ws1, ws2 = await make_workspace(session), await make_workspace(session)
    await make_contact(session, ws1, email="same@example.com")
    await make_contact(session, ws2, email="same@example.com")  # other workspace: fine
    session.add_all(
        [
            Contact(workspace_id=ws1.id, full_name="No Email 1"),
            Contact(workspace_id=ws1.id, full_name="No Email 2"),
        ]
    )
    await session.flush()  # multiple NULL emails are allowed
    with pytest.raises(IntegrityError):
        await make_contact(session, ws1, email="same@example.com")


async def test_company_dedupe_key_unique_per_workspace(session: AsyncSession) -> None:
    ws = await make_workspace(session)
    await make_company(session, ws, domain="dup.example.com")
    with pytest.raises(IntegrityError):
        await make_company(session, ws, domain="dup.example.com")


async def test_lead_unique_per_campaign_and_company(session: AsyncSession) -> None:
    ws = await make_workspace(session)
    camp, company = await make_campaign(session, ws), await make_company(session, ws)
    await make_lead(session, ws, camp, company)
    with pytest.raises(IntegrityError):
        await make_lead(session, ws, camp, company)


async def test_lead_score_must_be_between_0_and_100(session: AsyncSession) -> None:
    ws = await make_workspace(session)
    camp, company = await make_campaign(session, ws), await make_company(session, ws)
    lead = await make_lead(session, ws, camp, company)
    lead.score = 101
    with pytest.raises(IntegrityError, match="score_range"):
        await session.flush()


def _message(
    ws_id: uuid.UUID, campaign_id: uuid.UUID, contact_id: uuid.UUID, **kw: object
) -> EmailMessage:
    token = uuid.uuid4().hex
    fields: dict[str, object] = {
        "workspace_id": ws_id,
        "campaign_id": campaign_id,
        "contact_id": contact_id,
        "to_email": "pat@example.com",
        "subject": "Hello",
        "body_text": "Hi",
        "idempotency_key": f"idem-{token}",
        "rfc_message_id": f"<{token}@example.com>",
        "step_no": 0,
    }
    fields.update(kw)
    return EmailMessage(**fields)


async def test_only_one_message_per_campaign_contact_step(session: AsyncSession) -> None:
    """The DB-level last line of defence against double sends (ARCHITECTURE §5)."""
    ws = await make_workspace(session)
    camp = await make_campaign(session, ws)
    contact = await make_contact(session, ws)
    session.add(_message(ws.id, camp.id, contact.id, step_no=0))
    await session.flush()
    session.add(_message(ws.id, camp.id, contact.id, step_no=1))  # next step is fine
    await session.flush()
    session.add(_message(ws.id, camp.id, contact.id, step_no=0))  # duplicate step → rejected
    with pytest.raises(IntegrityError, match="uq_email_messages_sequence_step"):
        await session.flush()


async def test_owner_approved_replies_do_not_collide_with_sequence(session: AsyncSession) -> None:
    ws = await make_workspace(session)
    camp = await make_campaign(session, ws)
    contact = await make_contact(session, ws)
    session.add(_message(ws.id, camp.id, contact.id, step_no=0))
    session.add(_message(ws.id, camp.id, contact.id, step_no=0, kind=MessageKind.REPLY))
    session.add(_message(ws.id, camp.id, contact.id, step_no=0, kind=MessageKind.REPLY))
    await session.flush()


async def test_idempotency_key_is_unique_per_workspace(session: AsyncSession) -> None:
    ws = await make_workspace(session)
    camp = await make_campaign(session, ws)
    c1, c2 = await make_contact(session, ws), await make_contact(session, ws)
    session.add(_message(ws.id, camp.id, c1.id, idempotency_key="same"))
    await session.flush()
    session.add(_message(ws.id, camp.id, c2.id, idempotency_key="same"))
    with pytest.raises(IntegrityError):
        await session.flush()


# ───────────────────────────── audit log ─────────────────────────────


async def test_audit_log_is_append_only(session: AsyncSession, engine: AsyncEngine) -> None:
    ws = await make_workspace(session)
    entry = AuditLog(workspace_id=ws.id, actor_type=ActorType.SYSTEM, action="test.created")
    session.add(entry)
    await session.commit()

    async with engine.connect() as conn:
        with pytest.raises(DBAPIError, match="append-only"):
            await conn.execute(sa.text("UPDATE audit_logs SET action = 'tampered'"))
        await conn.rollback()
        with pytest.raises(DBAPIError, match="append-only"):
            await conn.execute(sa.text("DELETE FROM audit_logs"))
        await conn.rollback()

    # A retention job may opt in explicitly, inside its own transaction.
    async with engine.begin() as conn:
        await conn.execute(sa.text("SET LOCAL aoi.allow_audit_delete = 'on'"))
        result = await conn.execute(sa.text("DELETE FROM audit_logs"))
        assert result.rowcount == 1


# ───────────────────────────── full-text search ─────────────────────────────


async def test_company_and_contact_full_text_search(session: AsyncSession) -> None:
    ws = await make_workspace(session)
    company = await make_company(session, ws, name="Bright Smile Dental Clinic")
    await make_company(session, ws, name="Iron Gym", industry="fitness")
    contact = await make_contact(session, ws, company, email="owner@brightsmile.example")
    contact.position = "Practice Manager"
    await session.flush()

    hit = await session.execute(
        sa.select(Company.name).where(
            Company.search_tsv.op("@@")(sa.func.plainto_tsquery("simple", "dental")),
            Company.workspace_id == ws.id,
        )
    )
    assert [r[0] for r in hit] == ["Bright Smile Dental Clinic"]

    hit_contact = await session.execute(
        sa.select(Contact.email).where(
            Contact.search_tsv.op("@@")(sa.func.plainto_tsquery("simple", "manager")),
            Contact.workspace_id == ws.id,
        )
    )
    assert [r[0] for r in hit_contact] == ["owner@brightsmile.example"]


async def test_timestamps_are_timezone_aware(session: AsyncSession) -> None:
    ws = await make_workspace(session)
    await session.refresh(ws)
    assert ws.created_at.tzinfo is not None
    assert abs((datetime.now(UTC) - ws.created_at).total_seconds()) < 60


async def test_deleting_a_lead_keeps_message_history(session: AsyncSession) -> None:
    """Messages keep ``to_email`` even if the lead is deleted — so 'already contacted' checks survive."""
    ws = await make_workspace(session)
    camp, company = await make_campaign(session, ws), await make_company(session, ws)
    contact = await make_contact(session, ws, company)
    lead = await make_lead(session, ws, camp, company)
    msg = _message(ws.id, camp.id, contact.id, lead_id=lead.id, to_email="keep@example.com")
    session.add(msg)
    await session.flush()

    await session.delete(lead)
    await session.flush()
    await session.refresh(msg)
    assert msg.lead_id is None
    assert msg.to_email == "keep@example.com"
    assert (await session.get(Lead, lead.id)) is None
