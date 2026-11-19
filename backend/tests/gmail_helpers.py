"""A scriptable fake of Google's OAuth / userinfo / Gmail-profile / revoke endpoints, served over ``httpx.MockTransport``."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx

from app.integrations.gmail.oauth import (
    GMAIL_PROFILE_URL,
    OAUTH_EXCHANGE_URL,
    REQUESTED_SCOPES,
    REVOKE_URL,
    USERINFO_URL,
    OAuthConfig,
)

# Fixture addresses. Consumer-Gmail ones are assembled from parts so the public-repo audit (ADR-027) never mistakes a
# fixture for a real person's address; the service decides personal-vs-Workspace by the domain, so these must be real
# consumer domains. Everything else uses example.* domains.
PERSONAL_EMAIL = "test.mailbox@" + "gmail.com"
OTHER_PERSONAL_EMAIL = "someone.else@" + "gmail.com"
WORKSPACE_EMAIL = "founder@example.com"

CLIENT_ID = "test-client-id.apps.googleusercontent.com"
CLIENT_SECRET = (
    "GOCSPX-" + "TestOnlyNotARealSecret0123456"
)  # built at runtime so the secret scanner stays quiet
REDIRECT_URI = "http://localhost:3000/integrations/gmail/callback"
ALL_SCOPES = " ".join(REQUESTED_SCOPES)
CONFIG = OAuthConfig(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, redirect_uri=REDIRECT_URI)


class FakeGoogle:
    def __init__(self, email: str = PERSONAL_EMAIL) -> None:
        self.email = email
        self.granted_scopes = ALL_SCOPES
        self.include_refresh_token = True
        self.refresh_token = "1//refresh-token-" + "A1b2C3d4E5"
        self.refresh_error: str | None = None  # e.g. "invalid_grant"
        self.rotated_refresh_token: str | None = (
            None  # Google sometimes issues a new one on refresh
        )
        self.code_error: str | None = None
        self.outage = False
        self.profile_status = 200
        self.requests: list[httpx.Request] = []
        self.token_calls: list[dict[str, str]] = []
        self.revoked: list[str] = []
        self._issued = 0

    # ───────────────────────────── conveniences for assertions ─────────────────────────────

    def calls(self, url: str) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url).split("?")[0] == url]

    @property
    def refresh_grants(self) -> list[dict[str, str]]:
        return [c for c in self.token_calls if c["grant_type"] == "refresh_token"]

    @property
    def last_access_token(self) -> str:
        return f"ya29.access-token-{self._issued}"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ───────────────────────────── the fake itself ─────────────────────────────

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.outage:
            return httpx.Response(503, text="unavailable")
        routes = {
            OAUTH_EXCHANGE_URL: self._token,
            USERINFO_URL: self._userinfo,
            GMAIL_PROFILE_URL: self._profile,
            REVOKE_URL: self._revoke,
        }
        route = routes.get(str(request.url).split("?")[0])
        return route(request) if route else httpx.Response(404)

    def _userinfo(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"email": self.email, "email_verified": True, "name": "Test Owner"}
        )

    def _profile(self, request: httpx.Request) -> httpx.Response:
        if self.profile_status != 200:
            return httpx.Response(self.profile_status, json={"error": {"message": "nope"}})
        return httpx.Response(
            200,
            json={
                "emailAddress": self.email,
                "messagesTotal": 1234,
                "threadsTotal": 800,
                "historyId": "98765",
            },
        )

    def _revoke(self, request: httpx.Request) -> httpx.Response:
        self.revoked.append(parse_qs(request.content.decode())["token"][0])
        return httpx.Response(200, json={})

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_calls.append(form)
        assert form["client_id"] == CLIENT_ID
        assert form["client_secret"] == CLIENT_SECRET
        assert not urlsplit(str(request.url)).query, (
            "credentials and codes must be in the POST body, never the URL"
        )
        grant = form["grant_type"]
        if grant == "authorization_code":
            if self.code_error:
                return httpx.Response(
                    400,
                    json={
                        "error": self.code_error,
                        "error_description": f"Bad code {form['code']}",
                    },
                )
            self._issued += 1
            body = {
                "access_token": self.last_access_token,
                "expires_in": 3599,
                "scope": self.granted_scopes,
                "token_type": "Bearer",
            }
            if self.include_refresh_token:
                body["refresh_token"] = self.refresh_token
            return httpx.Response(200, json=body)
        if self.refresh_error:
            return httpx.Response(
                400,
                json={"error": self.refresh_error, "error_description": "Token has been revoked."},
            )
        self._issued += 1
        body = {
            "access_token": self.last_access_token,
            "expires_in": 3599,
            "scope": self.granted_scopes,
            "token_type": "Bearer",
        }
        if self.rotated_refresh_token:
            body["refresh_token"] = self.rotated_refresh_token
        return httpx.Response(200, json=body)
