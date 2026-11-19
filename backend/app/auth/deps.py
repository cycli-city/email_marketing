"""FastAPI dependencies that protect routes.

``require_owner`` is the one dependency business routes use. It composes:

1. ``get_principal``  — a valid, unexpired Supabase access token in ``Authorization: Bearer …`` (401 otherwise);
2. the **allow-list** — the token's email must be one of ``OWNER_EMAILS`` (403 otherwise);
3. optional **MFA**   — ``AUTH_REQUIRE_MFA`` demands an AAL2 session (403 ``mfa_required``);
4. **provisioning**   — the matching ``users`` / ``workspaces`` rows (created on the owner's first login).

Cookies are never read here: the API is bearer-only, which is why CORS needs no credentials and CSRF has no surface
(the browser never holds the bearer token — the Next.js BFF attaches it server-side, ADR-014).
"""

from __future__ import annotations

from typing import Annotated, cast

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import get_app_settings
from app.auth.claims import Owner, Principal
from app.auth.jwks import JwksUnavailableError
from app.auth.provisioning import OwnerDirectory
from app.auth.verifier import InvalidTokenError, TokenVerifier
from app.core.config import Settings
from app.core.errors import (
    AuthUnavailableError,
    ForbiddenError,
    MfaRequiredError,
    UnauthorizedError,
)

log = structlog.get_logger("auth")

# auto_error=False: we want our uniform error envelope (and WWW-Authenticate), not FastAPI's default body.
_bearer = HTTPBearer(
    auto_error=False,
    description="Supabase access token (attached by the BFF, never by the browser)",
)


def get_token_verifier(request: Request) -> TokenVerifier:
    verifier = getattr(request.app.state, "token_verifier", None)
    if verifier is None:
        raise AuthUnavailableError("Authentication is not configured on this server")
    return cast(TokenVerifier, verifier)


def get_owner_directory(request: Request) -> OwnerDirectory:
    directory = getattr(request.app.state, "owner_directory", None)
    if directory is None:
        raise AuthUnavailableError("Authentication is not configured on this server")
    return cast(OwnerDirectory, directory)


async def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    verifier: Annotated[TokenVerifier, Depends(get_token_verifier)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise UnauthorizedError("Authentication required")
    try:
        return await verifier.verify(credentials.credentials)
    except InvalidTokenError as exc:
        log.info("auth.rejected", reason=exc.reason)  # the reason stays server-side
        raise UnauthorizedError("Invalid or expired session") from exc
    except JwksUnavailableError as exc:
        log.error("auth.keys_unavailable")
        raise AuthUnavailableError("Authentication is temporarily unavailable") from exc


async def require_owner(
    principal: Annotated[Principal, Depends(get_principal)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    directory: Annotated[OwnerDirectory, Depends(get_owner_directory)],
) -> Owner:
    allowed = {email.strip().lower() for email in settings.owner_emails}
    if principal.email not in allowed:
        # A valid Supabase login that is not the owner. Signups are disabled, so this should never happen.
        log.warning("auth.not_allow_listed")
        raise ForbiddenError("This account is not authorised to use this application")
    if settings.auth_require_mfa and principal.aal != "aal2":
        raise MfaRequiredError("Two-factor authentication is required")
    provisioned = await directory.resolve(principal)
    return Owner(
        principal=principal, user_id=provisioned.user_id, workspace_id=provisioned.workspace_id
    )


CurrentOwner = Annotated[Owner, Depends(require_owner)]
