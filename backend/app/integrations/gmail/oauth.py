"""Google OAuth 2.0 for Gmail: the signed-in owner's browser round-trip, and the server-to-server calls (ADR-009).

Security design
---------------
* **Authorization-code flow with PKCE (S256)** *and* the client secret — the code is useless to anyone who intercepts it.
* **The ``state`` is an encrypted, authenticated blob** (Fernet): it carries the initiating user, the target mode, an expiry
  and the PKCE verifier. The browser (and Google) only ever see ciphertext, so the verifier stays secret, the state cannot be
  forged or edited, and a stolen state is only good for ten minutes and only for the user it was issued to.
* The callback is finished by an **authenticated** API call (the dashboard forwards ``code`` + ``state`` with the owner's
  session). The backend checks that the *current* user is the one the state was issued to — the defence against a
  link-your-mailbox-to-the-attacker's-account CSRF.
* Only the minimum scopes are requested; the granted scopes are checked, because Google lets the user untick any of them.
* ``access_type=offline`` + ``prompt=consent`` so Google always returns a refresh token.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from app.core.clock import Clock
from app.core.redact import scrub_error
from app.core.security import CryptoError, Encryptor
from app.domain.enums import CampaignMode
from app.integrations.gmail.errors import (
    AccountNeedsReauthError,
    GoogleUnavailableError,
    InvalidOAuthStateError,
    OAuthExchangeError,
)

log = structlog.get_logger("gmail.oauth")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_EXCHANGE_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

SCOPE_SEND = "https://www.googleapis.com/auth/gmail.send"
SCOPE_READ = "https://www.googleapis.com/auth/gmail.readonly"
REQUESTED_SCOPES = ("openid", "email", SCOPE_SEND, SCOPE_READ)
REQUIRED_SCOPES = frozenset({SCOPE_SEND, SCOPE_READ})

STATE_TTL = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class OAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str


@dataclass(frozen=True, slots=True)
class FlowState:
    """What the encrypted ``state`` carries across the round-trip to Google and back."""

    user_id: uuid.UUID
    workspace_id: uuid.UUID
    mode: CampaignMode
    verifier: str  # PKCE secret — only ever inside the encrypted state


class StateCodec:
    def __init__(self, encryptor: Encryptor, clock: Clock, *, ttl: timedelta = STATE_TTL) -> None:
        self._encryptor = encryptor
        self._clock = clock
        self._ttl = ttl

    def encode(self, state: FlowState) -> str:
        payload = {
            "u": str(state.user_id),
            "w": str(state.workspace_id),
            "m": state.mode.value,
            "v": state.verifier,
            "x": int((self._clock.now() + self._ttl).timestamp()),
            "n": secrets.token_urlsafe(8),
        }
        return self._encryptor.encrypt(json.dumps(payload, separators=(",", ":")))

    def decode(self, token: str) -> FlowState:
        try:
            payload = json.loads(self._encryptor.decrypt(token))
            if payload["x"] <= self._clock.now().timestamp():
                raise InvalidOAuthStateError(
                    "This connection attempt has expired. Please start again."
                )
            return FlowState(
                user_id=uuid.UUID(payload["u"]),
                workspace_id=uuid.UUID(payload["w"]),
                mode=CampaignMode(payload["m"]),
                verifier=str(payload["v"]),
            )
        except InvalidOAuthStateError:
            raise
        except (CryptoError, ValueError, KeyError, TypeError) as exc:
            # Forged, edited, from another deployment, or not ours at all — all look the same to the caller.
            raise InvalidOAuthStateError(
                "This connection attempt is not valid. Please start again."
            ) from exc


def pkce_pair() -> tuple[str, str]:
    """A fresh PKCE verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def build_authorization_url(
    config: OAuthConfig, *, state: str, challenge: str, login_hint: str | None = None
) -> str:
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "scope": " ".join(REQUESTED_SCOPES),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urlencode(params)}"


@dataclass(frozen=True, slots=True)
class TokenResponse:
    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scopes: frozenset[str]
    token_type: str


@dataclass(frozen=True, slots=True)
class UserInfo:
    email: str
    email_verified: bool
    name: str | None


@dataclass(frozen=True, slots=True)
class GmailProfile:
    email: str
    history_id: str | None
    messages_total: int | None


class GoogleClient:
    """The few Google endpoints we call. HTTPS only (the shared client refuses anything else) and no redirects."""

    def __init__(self, http: httpx.AsyncClient, config: OAuthConfig, clock: Clock) -> None:
        self._http = http
        self._config = config
        self._clock = clock

    # ───────────────────────────── token endpoint ─────────────────────────────

    async def exchange_code(self, code: str, verifier: str) -> TokenResponse:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": self._config.redirect_uri,
            }
        )

    async def refresh(self, refresh_token: str) -> TokenResponse:
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def _token_request(self, form: dict[str, str]) -> TokenResponse:
        body = {
            **form,
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
        }
        try:
            response = await self._http.post(OAUTH_EXCHANGE_URL, data=body)
        except httpx.HTTPError as exc:
            raise GoogleUnavailableError(
                "Could not reach Google. Please try again shortly."
            ) from exc
        if response.status_code >= 500:
            raise GoogleUnavailableError(
                "Google is temporarily unavailable. Please try again shortly."
            )
        data = _json(response)
        if response.status_code != 200:
            error = str(data.get("error", "unknown_error"))
            description = scrub_error(
                str(data.get("error_description", "")), known_secrets=_secrets(body)
            )[:200]
            log.warning("gmail.token_request_rejected", grant=form["grant_type"], error=error)
            if error == "invalid_grant" and form["grant_type"] == "refresh_token":
                raise AccountNeedsReauthError(
                    "Google no longer accepts this connection. Please reconnect the mailbox."
                )
            raise OAuthExchangeError(
                f"Google rejected the request ({error}). {description}".strip()
            )
        try:
            return TokenResponse(
                access_token=str(data["access_token"]),
                refresh_token=str(data["refresh_token"]) if data.get("refresh_token") else None,
                expires_at=self._clock.now()
                + timedelta(seconds=float(data.get("expires_in", 3600))),
                scopes=frozenset(str(data.get("scope", "")).split()),
                token_type=str(data.get("token_type", "Bearer")),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise OAuthExchangeError("Google returned an unusable token response.") from exc

    # ───────────────────────────── identity & profile ─────────────────────────────

    async def userinfo(self, access_token: str) -> UserInfo:
        data = await self._authorised_get(USERINFO_URL, access_token)
        email = data.get("email")
        if not isinstance(email, str) or "@" not in email:
            raise OAuthExchangeError("Google did not report which mailbox this is.")
        return UserInfo(
            email=email.strip().lower(),
            email_verified=bool(data.get("email_verified")),
            name=data.get("name"),
        )

    async def gmail_profile(self, access_token: str) -> GmailProfile:
        data = await self._authorised_get(GMAIL_PROFILE_URL, access_token)
        email = data.get("emailAddress")
        if not isinstance(email, str):
            raise OAuthExchangeError("The Gmail API did not return a mailbox profile.")
        total = data.get("messagesTotal")
        return GmailProfile(
            email=email.strip().lower(),
            history_id=str(data["historyId"]) if data.get("historyId") else None,
            messages_total=total if isinstance(total, int) else None,
        )

    async def _authorised_get(self, url: str, access_token: str) -> dict[str, Any]:
        try:
            response = await self._http.get(
                url, headers={"Authorization": f"Bearer {access_token}"}
            )
        except httpx.HTTPError as exc:
            raise GoogleUnavailableError(
                "Could not reach Google. Please try again shortly."
            ) from exc
        if response.status_code in (401, 403):
            raise AccountNeedsReauthError(
                "Google rejected the access token. Please reconnect the mailbox."
            )
        if response.status_code >= 500:
            raise GoogleUnavailableError(
                "Google is temporarily unavailable. Please try again shortly."
            )
        if response.status_code != 200:
            raise OAuthExchangeError(f"Google answered with HTTP {response.status_code}.")
        return _json(response)

    # ───────────────────────────── revocation ─────────────────────────────

    async def revoke(self, token: str) -> bool:
        """Best effort: tell Google to forget this grant. Never raises (disconnecting must always succeed locally)."""
        try:
            response = await self._http.post(REVOKE_URL, data={"token": token})
        except httpx.HTTPError:
            return False
        return response.status_code == 200


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _secrets(body: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        v
        for k, v in body.items()
        if k in {"client_secret", "refresh_token", "code", "code_verifier"}
    )
