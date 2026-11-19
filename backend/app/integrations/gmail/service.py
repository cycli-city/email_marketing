"""The Gmail connection: connect a mailbox, keep its access token fresh, list it, test it, disconnect it.

Tokens are Fernet-encrypted at rest and never leave this module in plain text (not in API responses, logs, errors or
the activity feed). A refresh token that Google no longer accepts puts the mailbox into ``needs_reauth`` — only that
mailbox pauses, the owner is told, and everything else keeps running (REQUIREMENTS_AUTONOMY §A).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.claims import Owner
from app.core.clock import Clock
from app.core.errors import NotFoundError
from app.core.security import Encryptor
from app.db.models import (
    ActivityEvent,
    AuditLog,
    ConnectedEmailAccount,
    OAuthToken,
    WatchState,
)
from app.db.session import session_scope
from app.domain.enums import (
    AccountProvider,
    AccountStatus,
    AccountType,
    ActivityLevel,
    ActorType,
    CampaignMode,
)
from app.integrations.gmail.errors import (
    AccountNeedsReauthError,
    InvalidOAuthStateError,
    MailboxAlreadyConnectedError,
    MissingScopesError,
    NoRefreshTokenError,
)
from app.integrations.gmail.oauth import (
    REQUIRED_SCOPES,
    FlowState,
    GmailProfile,
    GoogleClient,
    OAuthConfig,
    StateCodec,
    TokenResponse,
    build_authorization_url,
    pkce_pair,
)

log = structlog.get_logger("gmail")

# Refresh a little early so a token never expires between "check" and "use".
REFRESH_MARGIN = timedelta(seconds=90)
_CONSUMER_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


@dataclass(frozen=True, slots=True)
class AccountSummary:
    """Everything the UI may know about a mailbox. Deliberately has no token fields at all."""

    id: uuid.UUID
    email: str
    display_name: str | None
    mode: CampaignMode
    status: AccountStatus
    account_type: AccountType
    scopes: tuple[str, ...]
    connected_at: datetime | None
    last_sync_at: datetime | None
    last_error: str | None


def _summary(account: ConnectedEmailAccount) -> AccountSummary:
    return AccountSummary(
        id=account.id,
        email=account.email_address,
        display_name=account.display_name,
        mode=account.mode,
        status=account.status,
        account_type=account.account_type,
        scopes=tuple(account.scopes or ()),
        connected_at=account.connected_at,
        last_sync_at=account.last_sync_at,
        last_error=account.last_error,
    )


class GmailService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        encryptor: Encryptor,
        google: GoogleClient,
        config: OAuthConfig,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock
        self._encryptor = encryptor
        self._google = google
        self._config = config
        self._codec = StateCodec(encryptor, clock)
        self._refresh_locks: dict[uuid.UUID, asyncio.Lock] = {}

    # ───────────────────────────── connecting ─────────────────────────────

    def begin(self, owner: Owner, mode: CampaignMode) -> str:
        """The Google consent-screen URL for the owner to visit. Nothing is stored: the state carries everything."""
        verifier, challenge = pkce_pair()
        state = self._codec.encode(
            FlowState(
                user_id=owner.user_id, workspace_id=owner.workspace_id, mode=mode, verifier=verifier
            )
        )
        return build_authorization_url(self._config, state=state, challenge=challenge)

    async def complete(self, owner: Owner, code: str, state: str) -> AccountSummary:
        """Finish the flow. ``owner`` is the *currently authenticated* user; the state must have been issued to them."""
        flow = self._codec.decode(state)
        if flow.user_id != owner.user_id or flow.workspace_id != owner.workspace_id:
            raise InvalidOAuthStateError(
                "This connection attempt belongs to a different session. Please start again."
            )

        tokens = await self._google.exchange_code(code, flow.verifier)
        profile = await self._google.gmail_profile(
            tokens.access_token
        )  # also proves the Gmail API is usable
        info = await self._google.userinfo(tokens.access_token)

        problem = self._grant_problem(tokens)
        if problem is not None:
            # Do not leave a half-useful grant behind — unless the mailbox is already connected, in which case
            # revoking could take down the working connection's grant.
            if not await self._is_connected(owner.workspace_id, profile.email):
                await self._google.revoke(tokens.access_token)
            raise problem
        assert tokens.refresh_token is not None  # guaranteed by _grant_problem

        account = await self._store(
            owner,
            flow.mode,
            profile=profile,
            display_name=info.name,
            tokens=tokens,
            refresh_token=tokens.refresh_token,
        )
        log.info("gmail.connected", mode=flow.mode.value, account_id=str(account.id))
        return account

    @staticmethod
    def _grant_problem(tokens: TokenResponse) -> MissingScopesError | NoRefreshTokenError | None:
        missing = REQUIRED_SCOPES - tokens.scopes
        if missing:
            return MissingScopesError(
                "Gmail needs permission to both send and read mail. Please connect again and tick every box.",
                details={"missing": sorted(missing)},
            )
        if not tokens.refresh_token:
            return NoRefreshTokenError(
                "Google did not provide a long-lived connection. Remove this app at myaccount.google.com/permissions "
                "and connect again."
            )
        return None

    async def _is_connected(self, workspace_id: uuid.UUID, email: str) -> bool:
        async with self._sessions() as session:
            found = await session.execute(
                select(ConnectedEmailAccount.id).where(
                    ConnectedEmailAccount.workspace_id == workspace_id,
                    ConnectedEmailAccount.provider == AccountProvider.GMAIL,
                    ConnectedEmailAccount.email_address == email,
                )
            )
            return found.scalar_one_or_none() is not None

    async def _store(
        self,
        owner: Owner,
        mode: CampaignMode,
        *,
        profile: GmailProfile,
        display_name: str | None,
        tokens: TokenResponse,
        refresh_token: str,
    ) -> AccountSummary:
        now = self._clock.now()
        async with session_scope(self._sessions) as session:
            account = (
                await session.execute(
                    select(ConnectedEmailAccount)
                    .where(
                        ConnectedEmailAccount.workspace_id == owner.workspace_id,
                        ConnectedEmailAccount.provider == AccountProvider.GMAIL,
                        ConnectedEmailAccount.email_address == profile.email,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if account is not None and account.mode != mode:
                raise MailboxAlreadyConnectedError(
                    "That mailbox is already connected for the other mode. Disconnect it there first."
                )
            created = account is None
            if account is None:
                account = ConnectedEmailAccount(
                    workspace_id=owner.workspace_id,
                    provider=AccountProvider.GMAIL,
                    mode=mode,
                    email_address=profile.email,
                )
                session.add(account)
            domain = profile.email.rsplit("@", 1)[-1]
            account.display_name = display_name
            account.account_type = (
                AccountType.PERSONAL_GMAIL
                if domain in _CONSUMER_DOMAINS
                else AccountType.GOOGLE_WORKSPACE
            )
            account.status = AccountStatus.ACTIVE
            account.scopes = sorted(tokens.scopes)
            account.connected_at = now
            account.last_error = None
            account.last_error_at = None
            account.consecutive_failures = 0
            await session.flush()

            row = (
                await session.execute(
                    select(OAuthToken).where(OAuthToken.account_id == account.id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                row = OAuthToken(workspace_id=owner.workspace_id, account_id=account.id)
                session.add(row)
            row.access_token_enc = self._encryptor.encrypt(tokens.access_token)
            row.refresh_token_enc = self._encryptor.encrypt(refresh_token)
            row.expires_at = tokens.expires_at
            row.scope = " ".join(sorted(tokens.scopes))
            row.token_type = tokens.token_type
            row.last_refreshed_at = now

            watch = (
                await session.execute(select(WatchState).where(WatchState.account_id == account.id))
            ).scalar_one_or_none()
            if watch is None:
                session.add(
                    WatchState(
                        workspace_id=owner.workspace_id,
                        account_id=account.id,
                        history_id=profile.history_id,
                    )
                )

            session.add(
                AuditLog(
                    workspace_id=owner.workspace_id,
                    created_at=now,
                    actor_type=ActorType.USER,
                    actor_id=str(owner.principal.user_id),
                    action="gmail.connected" if created else "gmail.reconnected",
                    entity_type="connected_email_account",
                    entity_id=str(account.id),
                    details={"mode": mode.value, "provider": "gmail"},
                )
            )
            session.add(
                ActivityEvent(
                    workspace_id=owner.workspace_id,
                    created_at=now,
                    level=ActivityLevel.INFO,
                    category="email",
                    event_type="gmail_connected" if created else "gmail_reconnected",
                    message=f"{'Connected' if created else 'Reconnected'} {profile.email} for {mode.value.replace('_', ' ')}",
                    data={"account_id": str(account.id), "mode": mode.value},
                )
            )
            await session.flush()
            return _summary(account)

    # ───────────────────────────── reading & removing ─────────────────────────────

    async def list_accounts(self, workspace_id: uuid.UUID) -> list[AccountSummary]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(ConnectedEmailAccount)
                .where(
                    ConnectedEmailAccount.workspace_id == workspace_id,
                    ConnectedEmailAccount.provider == AccountProvider.GMAIL,
                )
                .order_by(ConnectedEmailAccount.mode, ConnectedEmailAccount.email_address)
            )
            return [_summary(a) for a in rows.scalars()]

    async def disconnect(self, owner: Owner, account_id: uuid.UUID) -> None:
        """Forget the mailbox here and ask Google to revoke the grant (best effort — local removal always happens)."""
        now = self._clock.now()
        async with session_scope(self._sessions) as session:
            account = await self._account(session, owner.workspace_id, account_id)
            token = (
                await session.execute(select(OAuthToken).where(OAuthToken.account_id == account.id))
            ).scalar_one_or_none()
            plain = None
            if token is not None and token.refresh_token_enc:
                try:
                    plain = self._encryptor.decrypt(token.refresh_token_enc)
                except Exception:  # an unreadable ciphertext must not block disconnecting
                    log.warning(
                        "gmail.token_undecryptable_on_disconnect", account_id=str(account.id)
                    )
            email, mode = account.email_address, account.mode
            await session.delete(account)  # cascades to oauth_tokens and watch_states
            session.add(
                AuditLog(
                    workspace_id=owner.workspace_id,
                    created_at=now,
                    actor_type=ActorType.USER,
                    actor_id=str(owner.principal.user_id),
                    action="gmail.disconnected",
                    entity_type="connected_email_account",
                    entity_id=str(account_id),
                    details={"mode": mode.value},
                )
            )
            session.add(
                ActivityEvent(
                    workspace_id=owner.workspace_id,
                    created_at=now,
                    level=ActivityLevel.INFO,
                    category="email",
                    event_type="gmail_disconnected",
                    message=f"Disconnected {email}",
                    data={"mode": mode.value},
                )
            )
        if plain is not None:
            await self._google.revoke(plain)

    @staticmethod
    async def _account(
        session: AsyncSession, workspace_id: uuid.UUID, account_id: uuid.UUID
    ) -> ConnectedEmailAccount:
        account = (
            await session.execute(
                select(ConnectedEmailAccount).where(
                    ConnectedEmailAccount.id == account_id,
                    ConnectedEmailAccount.workspace_id == workspace_id,
                )
            )
        ).scalar_one_or_none()
        if account is None:
            raise NotFoundError("Mailbox not found")
        return account

    # ───────────────────────────── using the connection ─────────────────────────────

    async def access_token(self, workspace_id: uuid.UUID, account_id: uuid.UUID) -> str:
        """A valid access token for the mailbox, refreshing it first if it is (nearly) expired.

        One refresh at a time per mailbox, so concurrent callers share a single call to Google. Raises
        :class:`AccountNeedsReauthError` if Google no longer accepts the stored credential (the mailbox is then marked
        ``needs_reauth``); a Google outage raises :class:`GoogleUnavailableError` and leaves the mailbox untouched.
        """
        lock = self._refresh_locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            now = self._clock.now()
            async with self._sessions() as session:
                account = await self._account(session, workspace_id, account_id)
                token = (
                    await session.execute(
                        select(OAuthToken).where(OAuthToken.account_id == account_id)
                    )
                ).scalar_one_or_none()
                if (
                    account.status == AccountStatus.NEEDS_REAUTH
                    or token is None
                    or not token.refresh_token_enc
                ):
                    raise AccountNeedsReauthError("This mailbox needs to be reconnected.")
                if (
                    token.access_token_enc
                    and token.expires_at
                    and token.expires_at - now > REFRESH_MARGIN
                ):
                    return self._encryptor.decrypt(token.access_token_enc)
                refresh_plain = self._encryptor.decrypt(token.refresh_token_enc)

            try:
                fresh = await self._google.refresh(refresh_plain)
            except AccountNeedsReauthError:
                await self.mark_needs_reauth(workspace_id, account_id)
                raise
            await self._save_refreshed(workspace_id, account_id, fresh)
            return fresh.access_token

    async def _save_refreshed(
        self, workspace_id: uuid.UUID, account_id: uuid.UUID, fresh: TokenResponse
    ) -> None:
        now = self._clock.now()
        async with session_scope(self._sessions) as session:
            account = await self._account(session, workspace_id, account_id)
            token = (
                await session.execute(
                    select(OAuthToken).where(OAuthToken.account_id == account_id).with_for_update()
                )
            ).scalar_one()
            token.access_token_enc = self._encryptor.encrypt(fresh.access_token)
            token.expires_at = fresh.expires_at
            token.last_refreshed_at = now
            if fresh.refresh_token:  # Google occasionally rotates it
                token.refresh_token_enc = self._encryptor.encrypt(fresh.refresh_token)
            account.consecutive_failures = 0
            if account.status == AccountStatus.ERROR:
                account.status = AccountStatus.ACTIVE
                account.last_error = None

    async def mark_needs_reauth(self, workspace_id: uuid.UUID, account_id: uuid.UUID) -> None:
        """Google refused this mailbox's credentials: stop using it and ask the owner to reconnect. Idempotent."""
        now = self._clock.now()
        async with session_scope(self._sessions) as session:
            account = await self._account(session, workspace_id, account_id)
            if account.status == AccountStatus.NEEDS_REAUTH:
                return
            account.status = AccountStatus.NEEDS_REAUTH
            account.last_error = "Google no longer accepts this connection — reconnect the mailbox."
            account.last_error_at = now
            session.add(
                ActivityEvent(
                    workspace_id=workspace_id,
                    created_at=now,
                    level=ActivityLevel.ERROR,
                    category="email",
                    event_type="gmail_needs_reauth",
                    message=f"{account.email_address} needs to be reconnected (Google rejected its credentials)",
                    data={"account_id": str(account_id)},
                )
            )

    async def test(self, workspace_id: uuid.UUID, account_id: uuid.UUID) -> GmailProfile:
        """Read-only check that the connection really works: asks Gmail for the mailbox profile. Sends nothing."""
        token = await self.access_token(workspace_id, account_id)
        try:
            return await self._google.gmail_profile(token)
        except AccountNeedsReauthError:
            await self.mark_needs_reauth(workspace_id, account_id)
            raise
