"""Tiny async factories for integration tests. Each returns a flushed ORM object."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, Company, Contact, Lead, Workspace
from app.domain.enums import CampaignMode


def _uniq() -> str:
    return uuid.uuid4().hex[:8]


async def make_workspace(session: AsyncSession, *, name: str | None = None) -> Workspace:
    suffix = _uniq()
    ws = Workspace(name=name or f"Workspace {suffix}", slug=f"ws-{suffix}")
    session.add(ws)
    await session.flush()
    return ws


async def make_campaign(
    session: AsyncSession, workspace: Workspace, *, mode: CampaignMode = CampaignMode.NEXORA
) -> Campaign:
    camp = Campaign(workspace_id=workspace.id, name=f"Campaign {_uniq()}", mode=mode)
    session.add(camp)
    await session.flush()
    return camp


async def make_company(
    session: AsyncSession,
    workspace: Workspace,
    *,
    name: str = "Acme Dental",
    domain: str | None = None,
    industry: str = "dental clinic",
) -> Company:
    domain = domain or f"{_uniq()}.example.com"
    company = Company(
        workspace_id=workspace.id,
        name=name,
        domain=domain,
        website=f"https://{domain}",
        dedupe_key=domain,
        industry=industry,
        country="US",
        city="Austin",
    )
    session.add(company)
    await session.flush()
    return company


async def make_contact(
    session: AsyncSession,
    workspace: Workspace,
    company: Company | None = None,
    *,
    email: str | None = None,
    **fields: object,
) -> Contact:
    contact = Contact(
        workspace_id=workspace.id,
        company_id=company.id if company else None,
        full_name="Pat Doe",
        email=email if email is not None else f"pat.{_uniq()}@example.com",
        **fields,
    )
    session.add(contact)
    await session.flush()
    return contact


async def make_lead(
    session: AsyncSession, workspace: Workspace, campaign: Campaign, company: Company
) -> Lead:
    lead = Lead(workspace_id=workspace.id, campaign_id=campaign.id, company_id=company.id)
    session.add(lead)
    await session.flush()
    return lead
