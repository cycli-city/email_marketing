"""The Gmail connection against a real PostgreSQL and a fake Google."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.claims import Owner, Principal
from app.core.clock import FakeClock
from app.core.errors import NotFoundError
from app.core.security import Encryptor
from app.db.models import (
    ActivityEvent,
    AuditLog,
    ConnectedEmailAccount,
    OAuthToken,
    WatchState,
)
from app.domain.enums import AccountStatus, AccountType, CampaignMode
from app.integrations.gmail.errors import (
    AccountNeedsReauthError,
    GoogleUnavailableError,
    InvalidOAuthStateError,
    MailboxAlreadyConnectedError,
    MissingScopesError,
    NoRefreshTokenError,
)
from app.integrations.gmail.oauth import GoogleClient, StateCodec
from app.integrations.gmail.service import GmailService
from tests.gmail_helpers import (
    CONFIG,
    OTHER_PERSONAL_EMAIL,
    PERSONAL_EMAIL,
    WORKSPACE_EMAIL,
    FakeGoogle,
)

Factory = async_sessionmaker[AsyncSession]
NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
SEND_ONLY = "openid email https://www.googleapis.com/auth/gmail.send"


class Stack:
    def __init__(self, factory: Factory, clock: FakeClock, google: FakeGoogle) -> None:
        self.factory, self.clock, self.google = factory, clock, google
        self.encryptor = Encryptor([Fernet.generate_key().decode()])
        self.service = GmailService(
            session_factory=factory,
            clock=clock,
            encryptor=self.encryptor,
            google=GoogleClient(httpx.AsyncClient(transport=google.transport()), CONFIG, clock),
            config=CONFIG,
        )
        self.codec = StateCodec(self.encryptor, clock)

    def owner(self, workspace_id: uuid.UUID, user_id: uuid.UUID | None = None) -> Owner:
        uid = user_id or uuid.UUID(int=77)
        principal = Principal(
            user_id=uid,
            email="owner@example.com",
            aal="aal1",
            session_id=None,
            expires_at=NOW + timedelta(hours=1),
        )
        return Owner(principal=principal, user_id=uid, workspace_id=workspace_id)

    def state_for(
        self, owner: Owner, mode: CampaignMode = CampaignMode.JOB_HUNTER
    ) -> tuple[str, str]:
        """(state, pkce verifier) exactly as the browser would carry them."""
        url = self.service.begin(owner, mode)
        state = parse_qs(urlsplit(url).query)["state"][0]
        return state, self.codec.decode(state).verifier

    async def connect(self, owner: Owner, mode: CampaignMode = CampaignMode.JOB_HUNTER) -> Any:
        state, _ = self.state_for(owner, mode)
        return await self.service.complete(owner, "auth-code-1", state)

    async def all(self, model: Any) -> list[Any]:
        async with self.factory() as s:
            return list((await s.execute(select(model))).scalars())


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def stack(factory: Factory, clock: FakeClock, google: FakeGoogle) -> Stack:
    return Stack(factory, clock, google)


# ───────────────────────────── connecting ─────────────────────────────


class TestConnect:
    async def test_the_flow_stores_the_mailbox_with_encrypted_tokens(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        owner = stack.owner(workspace_id)
        state, verifier = stack.state_for(owner)
        summary = await stack.service.complete(owner, "auth-code-1", state)

        assert (summary.email, summary.mode, summary.status) == (
            PERSONAL_EMAIL,
            CampaignMode.JOB_HUNTER,
            AccountStatus.ACTIVE,
        )
        assert summary.account_type == AccountType.PERSONAL_GMAIL
        assert "https://www.googleapis.com/auth/gmail.send" in summary.scopes
        # Google was given the PKCE verifier that was hidden inside the state.
        assert stack.google.token_calls[0]["code_verifier"] == verifier

        (token,) = await stack.all(OAuthToken)
        access, refresh = stack.google.last_access_token, stack.google.refresh_token
        assert token.access_token_enc
        assert token.refresh_token_enc
        assert access not in token.access_token_enc, "never stored in plain text"
        assert refresh not in token.refresh_token_enc, "never stored in plain text"
        assert stack.encryptor.decrypt(token.access_token_enc) == access
        assert stack.encryptor.decrypt(token.refresh_token_enc) == refresh
        assert token.expires_at == NOW + timedelta(seconds=3599)

        (watch,) = await stack.all(WatchState)
        assert watch.history_id == "98765"

    async def test_a_summary_never_carries_a_token(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        summary = await stack.connect(stack.owner(workspace_id))
        assert not {f for f in dir(summary) if "token" in f.lower()}
        assert stack.google.refresh_token not in repr(summary)

    async def test_connecting_is_audited_and_shown_in_the_activity_feed_without_secrets(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        await stack.connect(stack.owner(workspace_id))
        (audit,) = await stack.all(AuditLog)
        (event,) = await stack.all(ActivityEvent)
        assert (audit.action, audit.details) == (
            "gmail.connected",
            {"mode": "job_hunter", "provider": "gmail"},
        )
        assert event.event_type == "gmail_connected"
        assert PERSONAL_EMAIL in event.message
        for text in (str(audit.details), event.message, str(event.data)):
            assert stack.google.refresh_token not in text
            assert stack.google.last_access_token not in text

    async def test_a_workspace_domain_is_recognised_as_a_workspace_mailbox(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        stack.google.email = WORKSPACE_EMAIL
        summary = await stack.connect(stack.owner(workspace_id), CampaignMode.NEXORA)
        assert (summary.account_type, summary.mode) == (
            AccountType.GOOGLE_WORKSPACE,
            CampaignMode.NEXORA,
        )

    async def test_reconnecting_updates_in_place_and_never_duplicates(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        owner = stack.owner(workspace_id)
        first = await stack.connect(owner)
        stack.google.refresh_token = "1//second-refresh-token-" + "Zz9"
        second = await stack.connect(owner)
        assert first.id == second.id
        assert len(await stack.all(ConnectedEmailAccount)) == 1
        assert len(await stack.all(OAuthToken)) == 1
        (token,) = await stack.all(OAuthToken)
        assert stack.encryptor.decrypt(token.refresh_token_enc) == stack.google.refresh_token
        assert sorted(a.action for a in await stack.all(AuditLog)) == [
            "gmail.connected",
            "gmail.reconnected",
        ]

    async def test_the_same_mailbox_cannot_serve_both_modes(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        owner = stack.owner(workspace_id)
        await stack.connect(owner, CampaignMode.JOB_HUNTER)
        with pytest.raises(MailboxAlreadyConnectedError):
            await stack.connect(owner, CampaignMode.NEXORA)
        (account,) = await stack.all(ConnectedEmailAccount)
        assert account.mode == CampaignMode.JOB_HUNTER

    async def test_several_mailboxes_per_mode_are_allowed(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        owner = stack.owner(workspace_id)
        for address in ("a@example.com", "b@example.com"):
            stack.google.email = address
            await stack.connect(owner, CampaignMode.NEXORA)
        assert len(await stack.service.list_accounts(workspace_id)) == 2


class TestStateBinding:
    async def test_a_state_issued_to_another_user_is_refused_before_google_is_contacted(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        """The attack: trick the owner into finishing a flow someone else started (linking the attacker's mailbox)."""
        attacker_state, _ = stack.state_for(stack.owner(workspace_id, uuid.UUID(int=666)))
        with pytest.raises(InvalidOAuthStateError):
            await stack.service.complete(
                stack.owner(workspace_id), "attackers-code", attacker_state
            )
        assert stack.google.requests == []
        assert await stack.all(ConnectedEmailAccount) == []

    async def test_a_state_from_another_workspace_is_refused(
        self, stack: Stack, workspace_id: uuid.UUID, other_workspace_id: uuid.UUID
    ) -> None:
        state, _ = stack.state_for(stack.owner(other_workspace_id))
        with pytest.raises(InvalidOAuthStateError):
            await stack.service.complete(stack.owner(workspace_id), "code", state)

    @pytest.mark.parametrize("state", ["", "garbage", "gAAAAA" + "A" * 120])
    async def test_forged_states_are_refused(
        self, stack: Stack, workspace_id: uuid.UUID, state: str
    ) -> None:
        with pytest.raises(InvalidOAuthStateError):
            await stack.service.complete(stack.owner(workspace_id), "code", state)
        assert stack.google.requests == []

    async def test_an_old_attempt_cannot_be_completed(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        owner = stack.owner(workspace_id)
        state, _ = stack.state_for(owner)
        stack.clock.advance(minutes=11)
        with pytest.raises(InvalidOAuthStateError, match="expired"):
            await stack.service.complete(owner, "code", state)


class TestGrantProblems:
    async def test_an_unticked_permission_is_refused_and_the_half_grant_revoked(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        stack.google.granted_scopes = SEND_ONLY  # the owner unticked "read"
        with pytest.raises(MissingScopesError) as raised:
            await stack.connect(stack.owner(workspace_id))
        assert raised.value.details["missing"] == ["https://www.googleapis.com/auth/gmail.readonly"]
        assert await stack.all(ConnectedEmailAccount) == []
        assert await stack.all(OAuthToken) == []
        assert stack.google.revoked == [stack.google.last_access_token]

    async def test_a_failed_reconnect_does_not_revoke_a_working_connection(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        owner = stack.owner(workspace_id)
        await stack.connect(owner)
        stack.google.granted_scopes = SEND_ONLY
        with pytest.raises(MissingScopesError):
            await stack.connect(owner)
        assert stack.google.revoked == [], (
            "revoking could have killed the grant the mailbox is using"
        )
        assert (await stack.service.list_accounts(workspace_id))[0].status == AccountStatus.ACTIVE

    async def test_no_refresh_token_means_the_connection_could_not_be_kept_alive(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        stack.google.include_refresh_token = False
        with pytest.raises(NoRefreshTokenError):
            await stack.connect(stack.owner(workspace_id))
        assert await stack.all(OAuthToken) == []


# ───────────────────────────── keeping the token fresh ─────────────────────────────


class TestAccessToken:
    async def test_a_fresh_token_is_used_without_calling_google(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        summary = await stack.connect(stack.owner(workspace_id))
        before = len(stack.google.requests)
        assert (
            await stack.service.access_token(workspace_id, summary.id)
            == stack.google.last_access_token
        )
        assert len(stack.google.requests) == before

    async def test_an_expiring_token_is_refreshed_and_stored_encrypted(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        summary = await stack.connect(stack.owner(workspace_id))
        stack.clock.advance(seconds=3599 - 30)  # inside the 90 s safety margin
        token = await stack.service.access_token(workspace_id, summary.id)
        assert token == stack.google.last_access_token
        assert len(stack.google.refresh_grants) == 1
        assert stack.google.refresh_grants[0]["refresh_token"] == stack.google.refresh_token
        (row,) = await stack.all(OAuthToken)
        assert token not in row.access_token_enc
        assert stack.encryptor.decrypt(row.access_token_enc) == token
        assert row.expires_at == stack.clock.now() + timedelta(seconds=3599)

    async def test_concurrent_callers_share_one_refresh(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        summary = await stack.connect(stack.owner(workspace_id))
        stack.clock.advance(hours=2)
        tokens = await asyncio.gather(
            *(stack.service.access_token(workspace_id, summary.id) for _ in range(8))
        )
        assert len(set(tokens)) == 1
        assert len(stack.google.refresh_grants) == 1

    async def test_a_rotated_refresh_token_replaces_the_stored_one(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        summary = await stack.connect(stack.owner(workspace_id))
        stack.google.rotated_refresh_token = "1//rotated-" + "Qq7"
        stack.clock.advance(hours=2)
        await stack.service.access_token(workspace_id, summary.id)
        (row,) = await stack.all(OAuthToken)
        assert stack.encryptor.decrypt(row.refresh_token_enc) == "1//rotated-Qq7"

    async def test_a_revoked_grant_pauses_only_that_mailbox_and_tells_the_owner(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        summary = await stack.connect(stack.owner(workspace_id))
        stack.google.refresh_error = "invalid_grant"
        stack.clock.advance(hours=2)
        with pytest.raises(AccountNeedsReauthError):
            await stack.service.access_token(workspace_id, summary.id)
        (account,) = await stack.all(ConnectedEmailAccount)
        assert account.status == AccountStatus.NEEDS_REAUTH
        assert account.last_error
        events = [e for e in await stack.all(ActivityEvent) if e.event_type == "gmail_needs_reauth"]
        assert len(events) == 1

        before = len(stack.google.requests)
        with pytest.raises(
            AccountNeedsReauthError
        ):  # no further requests to a provider that said no
            await stack.service.access_token(workspace_id, summary.id)
        assert len(stack.google.requests) == before

    async def test_a_google_outage_leaves_the_mailbox_untouched(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        summary = await stack.connect(stack.owner(workspace_id))
        stack.google.outage = True
        stack.clock.advance(hours=2)
        with pytest.raises(GoogleUnavailableError):
            await stack.service.access_token(workspace_id, summary.id)
        (account,) = await stack.all(ConnectedEmailAccount)
        assert account.status == AccountStatus.ACTIVE
        stack.google.outage = False
        assert await stack.service.access_token(workspace_id, summary.id)  # recovers by itself

    async def test_reconnecting_a_paused_mailbox_makes_it_usable_again(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        owner = stack.owner(workspace_id)
        summary = await stack.connect(owner)
        stack.google.refresh_error = "invalid_grant"
        stack.clock.advance(hours=2)
        with pytest.raises(AccountNeedsReauthError):
            await stack.service.access_token(workspace_id, summary.id)
        stack.google.refresh_error = None
        await stack.connect(owner)
        assert (await stack.service.list_accounts(workspace_id))[0].status == AccountStatus.ACTIVE
        assert await stack.service.access_token(workspace_id, summary.id)

    async def test_another_workspaces_mailbox_is_not_reachable(
        self, stack: Stack, workspace_id: uuid.UUID, other_workspace_id: uuid.UUID
    ) -> None:
        summary = await stack.connect(stack.owner(workspace_id))
        with pytest.raises(NotFoundError):
            await stack.service.access_token(other_workspace_id, summary.id)


class TestTestConnection:
    async def test_reads_the_mailbox_profile_and_sends_nothing(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        summary = await stack.connect(stack.owner(workspace_id))
        profile = await stack.service.test(workspace_id, summary.id)
        assert (profile.email, profile.messages_total) == (PERSONAL_EMAIL, 1234)
        assert not any("/messages/send" in str(r.url) for r in stack.google.requests)

    async def test_a_rejected_token_marks_the_mailbox_for_reconnection(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        summary = await stack.connect(stack.owner(workspace_id))
        stack.google.profile_status = 401
        with pytest.raises(AccountNeedsReauthError):
            await stack.service.test(workspace_id, summary.id)
        assert (await stack.service.list_accounts(workspace_id))[
            0
        ].status == AccountStatus.NEEDS_REAUTH


class TestListAndDisconnect:
    async def test_lists_only_this_workspaces_mailboxes(
        self, stack: Stack, workspace_id: uuid.UUID, other_workspace_id: uuid.UUID
    ) -> None:
        await stack.connect(stack.owner(workspace_id))
        stack.google.email = OTHER_PERSONAL_EMAIL
        await stack.connect(stack.owner(other_workspace_id))
        assert [a.email for a in await stack.service.list_accounts(workspace_id)] == [
            PERSONAL_EMAIL
        ]

    async def test_disconnecting_removes_everything_and_revokes_at_google(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        owner = stack.owner(workspace_id)
        summary = await stack.connect(owner)
        await stack.service.disconnect(owner, summary.id)
        for model in (ConnectedEmailAccount, OAuthToken, WatchState):
            assert await stack.all(model) == []
        assert stack.google.revoked == [stack.google.refresh_token]
        assert "gmail.disconnected" in [a.action for a in await stack.all(AuditLog)]

    async def test_disconnecting_works_even_when_google_cannot_be_reached(
        self, stack: Stack, workspace_id: uuid.UUID
    ) -> None:
        owner = stack.owner(workspace_id)
        summary = await stack.connect(owner)
        stack.google.outage = True
        await stack.service.disconnect(owner, summary.id)
        assert await stack.all(ConnectedEmailAccount) == []

    async def test_another_workspace_cannot_disconnect_your_mailbox(
        self, stack: Stack, workspace_id: uuid.UUID, other_workspace_id: uuid.UUID
    ) -> None:
        summary = await stack.connect(stack.owner(workspace_id))
        with pytest.raises(NotFoundError):
            await stack.service.disconnect(stack.owner(other_workspace_id), summary.id)
        assert len(await stack.all(ConnectedEmailAccount)) == 1
