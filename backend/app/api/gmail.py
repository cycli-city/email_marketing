"""Gmail connection API (all routes require the signed-in owner). No response ever contains a token."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from app.auth.deps import CurrentOwner
from app.core.errors import IntegrationNotConfiguredError
from app.domain.enums import AccountStatus, AccountType, CampaignMode
from app.integrations.gmail.service import AccountSummary, GmailService

router = APIRouter(prefix="/api/v1/integrations/gmail", tags=["integrations"])

_Responses = dict[int | str, dict[str, Any]]
_NOT_CONFIGURED: _Responses = {
    503: {"description": "Google OAuth is not configured on this server"}
}


def get_gmail_service(request: Request) -> GmailService:
    service = getattr(request.app.state, "gmail_service", None)
    if service is None:
        raise IntegrationNotConfiguredError(
            "Gmail is not configured: set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI and ENCRYPTION_KEY"
        )
    return cast(GmailService, service)


Gmail = Annotated[GmailService, Depends(get_gmail_service)]


class ConnectRequest(BaseModel):
    mode: CampaignMode


class ConnectResponse(BaseModel):
    authorization_url: str


class ExchangeRequest(BaseModel):
    # Bounded: these arrive from the browser's address bar.
    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=4096)


class AccountOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    mode: CampaignMode
    status: AccountStatus
    account_type: AccountType
    scopes: list[str]
    connected_at: datetime | None
    last_sync_at: datetime | None
    last_error: str | None


class AccountsResponse(BaseModel):
    accounts: list[AccountOut]


class TestResponse(BaseModel):
    ok: bool
    email: str
    messages_total: int | None


def _out(summary: AccountSummary) -> AccountOut:
    return AccountOut(
        id=summary.id,
        email=summary.email,
        display_name=summary.display_name,
        mode=summary.mode,
        status=summary.status,
        account_type=summary.account_type,
        scopes=list(summary.scopes),
        connected_at=summary.connected_at,
        last_sync_at=summary.last_sync_at,
        last_error=summary.last_error,
    )


@router.post(
    "/connect",
    response_model=ConnectResponse,
    summary="Start connecting a Gmail mailbox",
    responses=_NOT_CONFIGURED,
)
async def connect(body: ConnectRequest, owner: CurrentOwner, gmail: Gmail) -> ConnectResponse:
    return ConnectResponse(authorization_url=gmail.begin(owner, body.mode))


@router.post(
    "/exchange",
    response_model=AccountOut,
    summary="Finish connecting: exchange Google's code (the state must belong to the signed-in owner)",
    responses={
        400: {"description": "Invalid/expired state, or Google refused the code"},
        409: {"description": "The mailbox is already connected for the other mode"},
        422: {"description": "A required permission was not granted"},
        **_NOT_CONFIGURED,
    },
)
async def exchange(body: ExchangeRequest, owner: CurrentOwner, gmail: Gmail) -> AccountOut:
    return _out(await gmail.complete(owner, body.code, body.state))


@router.get(
    "/accounts",
    response_model=AccountsResponse,
    summary="Connected Gmail mailboxes",
    responses=_NOT_CONFIGURED,
)
async def accounts(owner: CurrentOwner, gmail: Gmail) -> AccountsResponse:
    return AccountsResponse(
        accounts=[_out(a) for a in await gmail.list_accounts(owner.workspace_id)]
    )


@router.delete(
    "/accounts/{account_id}",
    status_code=204,
    summary="Disconnect a mailbox and revoke Google's grant",
    responses={404: {"description": "No such mailbox"}, **_NOT_CONFIGURED},
)
async def disconnect(account_id: uuid.UUID, owner: CurrentOwner, gmail: Gmail) -> Response:
    await gmail.disconnect(owner, account_id)
    return Response(status_code=204)


@router.post(
    "/accounts/{account_id}/test",
    response_model=TestResponse,
    summary="Read-only connection check (asks Gmail for the mailbox profile; sends nothing)",
    responses={
        404: {"description": "No such mailbox"},
        409: {"description": "Reconnect required"},
        **_NOT_CONFIGURED,
    },
)
async def test_connection(account_id: uuid.UUID, owner: CurrentOwner, gmail: Gmail) -> TestResponse:
    profile = await gmail.test(owner.workspace_id, account_id)
    return TestResponse(ok=True, email=profile.email, messages_total=profile.messages_total)
