"""The OAuth building blocks: encrypted state, PKCE, the authorisation URL and the Google client."""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.core.clock import FakeClock
from app.core.security import Encryptor
from app.domain.enums import CampaignMode
from app.integrations.gmail.errors import (
    AccountNeedsReauthError,
    GoogleUnavailableError,
    InvalidOAuthStateError,
    OAuthExchangeError,
)
from app.integrations.gmail.oauth import (
    REQUESTED_SCOPES,
    FlowState,
    GoogleClient,
    StateCodec,
    build_authorization_url,
    pkce_pair,
)
from tests.gmail_helpers import CLIENT_ID, CLIENT_SECRET, CONFIG, REDIRECT_URI, FakeGoogle

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
USER, WORKSPACE = uuid.UUID(int=1), uuid.UUID(int=2)


def codec(clock: FakeClock | None = None, key: str | None = None) -> StateCodec:
    return StateCodec(Encryptor([key or Encryptor.generate_key()]), clock or FakeClock(NOW))


def flow(verifier: str = "v" * 64) -> FlowState:
    return FlowState(
        user_id=USER, workspace_id=WORKSPACE, mode=CampaignMode.JOB_HUNTER, verifier=verifier
    )


class TestState:
    def test_round_trips_everything_the_flow_needs(self) -> None:
        c = codec()
        assert c.decode(c.encode(flow())) == flow()

    def test_the_browser_only_ever_sees_ciphertext_so_the_pkce_secret_stays_secret(self) -> None:
        verifier = "SECRET-PKCE-VERIFIER-" + "x" * 40
        token = codec().encode(flow(verifier))
        assert verifier not in token
        assert str(USER) not in token
        assert str(WORKSPACE) not in token
        assert "job_hunter" not in token

    def test_every_state_is_unique(self) -> None:
        c = codec()
        assert c.encode(flow()) != c.encode(flow())

    def test_a_tampered_state_is_rejected(self) -> None:
        c = codec()
        token = c.encode(flow())
        flipped = token[:-6] + ("AAAAAA" if not token.endswith("AAAAAA") else "BBBBBB")
        with pytest.raises(InvalidOAuthStateError):
            c.decode(flipped)

    def test_a_state_from_another_deployment_or_key_is_rejected(self) -> None:
        with pytest.raises(InvalidOAuthStateError):
            codec().decode(codec().encode(flow()))

    @pytest.mark.parametrize("garbage", ["", "x", "not-a-token", "a.b.c", "gAAAAA" + "A" * 80])
    def test_garbage_is_rejected(self, garbage: str) -> None:
        with pytest.raises(InvalidOAuthStateError):
            codec().decode(garbage)

    def test_the_state_expires_after_ten_minutes(self) -> None:
        clock = FakeClock(NOW)
        c = codec(clock, Encryptor.generate_key())
        token = c.encode(flow())
        clock.advance(minutes=9, seconds=59)
        assert c.decode(token) == flow()
        clock.advance(seconds=2)
        with pytest.raises(InvalidOAuthStateError, match="expired"):
            c.decode(token)


class TestPkce:
    def test_challenge_is_the_s256_of_the_verifier(self) -> None:
        verifier, challenge = pkce_pair()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert challenge == expected
        assert 43 <= len(verifier) <= 128  # RFC 7636 bounds

    def test_pairs_are_fresh(self) -> None:
        assert pkce_pair()[0] != pkce_pair()[0]


class TestAuthorizationUrl:
    def params(self, **kwargs: str | None) -> dict[str, list[str]]:
        url = build_authorization_url(CONFIG, state="STATE", challenge="CHALLENGE", **kwargs)
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        return parse_qs(urlsplit(url).query)

    def test_asks_for_exactly_the_minimum_permissions_and_a_long_lived_grant(self) -> None:
        q = self.params()
        assert q["scope"] == [" ".join(REQUESTED_SCOPES)]
        assert set(q["scope"][0].split()) == {
            "openid",
            "email",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.readonly",
        }
        assert q["access_type"] == ["offline"]
        assert q["prompt"] == ["consent"]
        assert q["response_type"] == ["code"]

    def test_uses_pkce_and_the_exact_registered_redirect(self) -> None:
        q = self.params()
        assert (q["code_challenge"], q["code_challenge_method"]) == (["CHALLENGE"], ["S256"])
        assert q["redirect_uri"] == [REDIRECT_URI]
        assert q["state"] == ["STATE"]
        assert q["client_id"] == [CLIENT_ID]

    def test_never_puts_a_secret_or_a_verifier_in_the_url(self) -> None:
        url = build_authorization_url(CONFIG, state="STATE", challenge="CHALLENGE")
        assert CLIENT_SECRET not in url
        assert "client_secret" not in url
        assert "code_verifier" not in url

    def test_does_not_request_broader_gmail_access_than_needed(self) -> None:
        scopes = self.params()["scope"][0]
        assert "gmail.modify" not in scopes
        assert "mail.google.com" not in scopes
        assert "gmail.compose" not in scopes


def client(google: FakeGoogle, clock: FakeClock | None = None) -> GoogleClient:
    return GoogleClient(
        httpx.AsyncClient(transport=google.transport()), CONFIG, clock or FakeClock(NOW)
    )


class TestGoogleClient:
    async def test_exchanging_a_code_returns_tokens_scopes_and_an_expiry_from_our_clock(
        self,
    ) -> None:
        google = FakeGoogle()
        tokens = await client(google).exchange_code("the-code", "the-verifier")
        assert tokens.access_token == google.last_access_token
        assert tokens.refresh_token == google.refresh_token
        assert tokens.expires_at == NOW + timedelta(seconds=3599)
        assert "https://www.googleapis.com/auth/gmail.send" in tokens.scopes
        sent = google.token_calls[0]
        assert (sent["code"], sent["code_verifier"], sent["redirect_uri"]) == (
            "the-code",
            "the-verifier",
            REDIRECT_URI,
        )

    async def test_a_refresh_returns_only_a_new_access_token(self) -> None:
        google = FakeGoogle()
        tokens = await client(google).refresh("refresh-token-value")
        assert tokens.refresh_token is None
        assert google.refresh_grants[0]["refresh_token"] == "refresh-token-value"

    async def test_a_revoked_refresh_token_means_the_mailbox_must_be_reconnected(self) -> None:
        google = FakeGoogle()
        google.refresh_error = "invalid_grant"
        with pytest.raises(AccountNeedsReauthError):
            await client(google).refresh("x")

    async def test_a_refused_code_is_reported_without_echoing_secrets_back(self) -> None:
        google = FakeGoogle()
        google.code_error = "invalid_grant"
        with pytest.raises(OAuthExchangeError) as raised:
            await client(google).exchange_code("SECRET-AUTH-CODE-123456", "v")
        assert "invalid_grant" in str(raised.value)
        assert "SECRET-AUTH-CODE-123456" not in str(
            raised.value
        )  # Google echoes the code in its description

    async def test_google_outages_are_distinct_from_rejections(self) -> None:
        google = FakeGoogle()
        google.outage = True
        with pytest.raises(GoogleUnavailableError):
            await client(google).refresh("x")
        with pytest.raises(GoogleUnavailableError):
            await client(google).userinfo("token")

    async def test_a_network_failure_is_an_outage_not_a_rejection(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        broken = GoogleClient(
            httpx.AsyncClient(transport=httpx.MockTransport(refuse)), CONFIG, FakeClock(NOW)
        )
        with pytest.raises(GoogleUnavailableError):
            await broken.exchange_code("c", "v")

    async def test_userinfo_and_profile_identify_the_mailbox(self) -> None:
        google = FakeGoogle("Someone@Example.COM")
        info = await client(google).userinfo("tok")
        profile = await client(google).gmail_profile("tok")
        assert (info.email, info.email_verified, info.name) == (
            "someone@example.com",
            True,
            "Test Owner",
        )
        assert (profile.email, profile.history_id, profile.messages_total) == (
            "someone@example.com",
            "98765",
            1234,
        )
        assert google.requests[0].headers["authorization"] == "Bearer tok"

    async def test_a_rejected_access_token_means_reconnect(self) -> None:
        google = FakeGoogle()
        google.profile_status = 401
        with pytest.raises(AccountNeedsReauthError):
            await client(google).gmail_profile("tok")

    async def test_revocation_never_raises(self) -> None:
        google = FakeGoogle()
        assert await client(google).revoke("refresh-token") is True
        assert google.revoked == ["refresh-token"]

        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        offline = GoogleClient(
            httpx.AsyncClient(transport=httpx.MockTransport(refuse)), CONFIG, FakeClock(NOW)
        )
        assert await offline.revoke("x") is False  # disconnecting locally must still be possible
