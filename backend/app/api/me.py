"""``GET /api/v1/me`` — who the current session belongs to. Also the BFF's cheap "is this session still valid?" probe."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from app.auth.deps import CurrentOwner

router = APIRouter(prefix="/api/v1", tags=["auth"])


class MeResponse(BaseModel):
    user_id: uuid.UUID
    email: str
    workspace_id: uuid.UUID
    assurance_level: str  # "aal1" (password) or "aal2" (password + second factor)
    session_expires_at: datetime


@router.get(
    "/me",
    response_model=MeResponse,
    summary="The authenticated owner",
    responses={
        401: {"description": "Missing, invalid or expired session"},
        403: {"description": "Not the owner, or a second factor is required"},
        503: {"description": "Authentication is not available (fails closed)"},
    },
)
async def me(owner: CurrentOwner) -> MeResponse:
    return MeResponse(
        user_id=owner.user_id,
        email=owner.email,
        workspace_id=owner.workspace_id,
        assurance_level=owner.principal.aal,
        session_expires_at=owner.principal.expires_at,
    )
