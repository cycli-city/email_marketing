"""Errors of the Gmail connection. Messages are safe to show; none ever contains a token or secret."""

from __future__ import annotations

from app.core.errors import AppError, ConflictError


class InvalidOAuthStateError(AppError):
    """The OAuth ``state`` is forged, expired, already for someone else, or simply unreadable."""

    status_code = 400
    code = "oauth_state_invalid"


class OAuthExchangeError(AppError):
    """Google refused the authorisation code (expired, reused, or the client settings do not match)."""

    status_code = 400
    code = "oauth_exchange_failed"


class MissingScopesError(AppError):
    """The owner unticked a permission on Google's consent screen; sending/reading would not work."""

    status_code = 422
    code = "gmail_scopes_missing"


class NoRefreshTokenError(AppError):
    """Google granted access but returned no refresh token, so the connection could not be kept alive."""

    status_code = 422
    code = "gmail_no_refresh_token"


class MailboxAlreadyConnectedError(ConflictError):
    code = "mailbox_already_connected"


class AccountNeedsReauthError(AppError):
    """Google rejected the stored refresh token (revoked, expired after long disuse, password change…)."""

    status_code = 409
    code = "gmail_needs_reauth"


class GoogleUnavailableError(AppError):
    """Google could not be reached or answered with a server error. Retry later; the connection itself is intact."""

    status_code = 503
    code = "google_unavailable"
