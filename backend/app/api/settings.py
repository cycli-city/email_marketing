"""Settings the compliance gate depends on: who the mail is from, and each country's sending policy (ADR-043).

Every route needs the signed-in owner. Loosening a policy is a deliberate act: the request must say it understands that
(``acknowledge_loosening``), and every applied change is written to the audit log with before and after.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.deps import CurrentOwner
from app.core.clock import Clock
from app.core.errors import NotFoundError, ValidationFailedError
from app.db.models import JurisdictionPolicy, SenderProfile
from app.domain.enums import CampaignMode, ConsentPolicy
from app.services.outreach.policies import (
    LooseningNotAcknowledgedError,
    PolicyChange,
    list_policies,
    update_policy,
)
from app.services.outreach.profile import ProfileError, ProfileInput, SenderProfiles

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


def _factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast("async_sessionmaker[AsyncSession]", request.app.state.session_factory)


def _clock(request: Request) -> Clock:
    return cast(Clock, request.app.state.clock)


# ───────────────────────────── sender profiles ─────────────────────────────


class SenderProfileIn(BaseModel):
    """Bounded on the way in; the service cleans and validates further."""

    display_name: str = Field(max_length=400)
    postal_address: str | None = Field(default=None, max_length=1500)
    reply_to: str | None = Field(default=None, max_length=400)
    signature_text: str | None = Field(default=None, max_length=6000)
    website: str | None = Field(default=None, max_length=600)


class SenderProfileOut(BaseModel):
    display_name: str
    postal_address: str | None
    reply_to: str | None
    signature_text: str | None
    website: str | None
    updated_at: datetime


class ProfileSlot(BaseModel):
    mode: CampaignMode
    profile: SenderProfileOut | None
    #: What still has to be filled in before this mode may send (empty = ready).
    missing: list[str]


class SenderProfilesResponse(BaseModel):
    profiles: list[ProfileSlot]


def _profile_out(profile: SenderProfile | None) -> SenderProfileOut | None:
    if profile is None:
        return None
    return SenderProfileOut(
        display_name=profile.display_name,
        postal_address=profile.postal_address,
        reply_to=profile.reply_to,
        signature_text=profile.signature_text,
        website=profile.website,
        updated_at=profile.updated_at,
    )


async def _slot(
    service: SenderProfiles, workspace_id: uuid.UUID, mode: CampaignMode
) -> ProfileSlot:
    return ProfileSlot(
        mode=mode,
        profile=_profile_out(await service.get(workspace_id, mode)),
        missing=await service.missing(workspace_id, mode),
    )


@router.get(
    "/sender-profiles",
    response_model=SenderProfilesResponse,
    summary="Sender identity for each mode",
)
async def get_sender_profiles(owner: CurrentOwner, request: Request) -> SenderProfilesResponse:
    service = SenderProfiles(_factory(request), _clock(request))
    return SenderProfilesResponse(
        profiles=[await _slot(service, owner.workspace_id, mode) for mode in CampaignMode]
    )


@router.put(
    "/sender-profiles/{mode}",
    response_model=ProfileSlot,
    summary="Save the sender identity for a mode (replaces it as a whole)",
    responses={422: {"description": "A field is not acceptable; ``details.field`` says which"}},
)
async def put_sender_profile(
    mode: CampaignMode, body: SenderProfileIn, owner: CurrentOwner, request: Request
) -> ProfileSlot:
    service = SenderProfiles(_factory(request), _clock(request))
    try:
        await service.save(
            owner.workspace_id,
            mode,
            ProfileInput(
                display_name=body.display_name,
                postal_address=body.postal_address,
                reply_to=body.reply_to,
                signature_text=body.signature_text,
                website=body.website,
            ),
        )
    except ProfileError as exc:
        raise ValidationFailedError(str(exc), details={"field": exc.field}) from exc
    return await _slot(service, owner.workspace_id, mode)


# ───────────────────────────── country policies ─────────────────────────────

Country = Annotated[str, Path(min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")]


class PolicyOut(BaseModel):
    country: str
    cold_b2b_marketing: ConsentPolicy
    cold_to_individuals: ConsentPolicy
    requires_unsubscribe: bool
    requires_postal_address: bool
    #: Always true: a message must name its sender whatever a policy says. Shown, not editable.
    requires_sender_identification: bool = True
    notes: str | None
    reviewed_on: date | None
    version: int


class PoliciesResponse(BaseModel):
    policies: list[PolicyOut]
    unreviewed: int


class PolicyPatch(BaseModel):
    cold_b2b_marketing: ConsentPolicy | None = None
    cold_to_individuals: ConsentPolicy | None = None
    requires_unsubscribe: bool | None = None
    requires_postal_address: bool | None = None
    mark_reviewed: bool = True
    #: Required whenever the change makes sending easier (a stricter rule relaxed, or a requirement dropped).
    acknowledge_loosening: bool = False


def _policy_out(row: JurisdictionPolicy) -> PolicyOut:
    return PolicyOut(
        country=row.country,
        cold_b2b_marketing=row.cold_b2b_marketing,
        cold_to_individuals=row.cold_to_individuals,
        requires_unsubscribe=row.requires_unsubscribe,
        requires_postal_address=row.requires_postal_address,
        notes=row.notes,
        reviewed_on=row.reviewed_on,
        version=row.version,
    )


@router.get(
    "/policies",
    response_model=PoliciesResponse,
    summary="Sending policy per country (the conservative defaults are created on first use)",
)
async def get_policies(owner: CurrentOwner, request: Request) -> PoliciesResponse:
    rows = await list_policies(_factory(request), _clock(request), owner.workspace_id)
    return PoliciesResponse(
        policies=[_policy_out(r) for r in rows],
        unreviewed=sum(1 for r in rows if r.reviewed_on is None),
    )


@router.patch(
    "/policies/{country}",
    response_model=PolicyOut,
    summary="Review or change one country's policy",
    responses={
        404: {"description": "No policy for that country"},
        422: {"description": "The change loosens the policy and was not acknowledged"},
    },
)
async def patch_policy(
    country: Country, body: PolicyPatch, owner: CurrentOwner, request: Request
) -> PolicyOut:
    try:
        row = await update_policy(
            _factory(request),
            _clock(request),
            owner.workspace_id,
            actor_id=owner.user_id,
            country=country,
            change=PolicyChange(
                cold_b2b_marketing=body.cold_b2b_marketing,
                cold_to_individuals=body.cold_to_individuals,
                requires_unsubscribe=body.requires_unsubscribe,
                requires_postal_address=body.requires_postal_address,
                mark_reviewed=body.mark_reviewed,
            ),
            acknowledged_loosening=body.acknowledge_loosening,
        )
    except LooseningNotAcknowledgedError as exc:
        raise ValidationFailedError(
            "This change makes sending easier. Confirm that you understand and want it.",
            details={"loosens": exc.loosened},
        ) from exc
    if row is None:
        raise NotFoundError("There is no policy for that country.")
    return _policy_out(row)
