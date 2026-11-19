"""Typed application errors. API layer maps these to HTTP; workers map them to retry/park/dead-letter."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class. ``code`` is a stable machine-readable identifier; ``message`` is safe to show."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str = "", *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details or {}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ValidationFailedError(AppError):
    status_code = 422
    code = "validation_failed"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class MfaRequiredError(ForbiddenError):
    """The session is valid but has not completed multi-factor authentication (AAL2)."""

    code = "mfa_required"


class AuthUnavailableError(AppError):
    """Authentication cannot be performed right now (not configured, or signing keys unreachable). Fails closed."""

    status_code = 503
    code = "auth_unavailable"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str = "", *, retry_after: int = 1) -> None:
        super().__init__(message, details={"retry_after": retry_after})
        self.retry_after = retry_after


class ComplianceBlockedError(AppError):
    """An action was refused by the compliance gate (suppression, jurisdiction, missing opt-out, …)."""

    status_code = 422
    code = "compliance_blocked"


class IntegrationNotConfiguredError(AppError):
    """A capability needs credentials that are not present. Never papered over with fake data."""

    status_code = 503
    code = "integration_not_configured"


class KillSwitchActiveError(AppError):
    status_code = 423
    code = "kill_switch_active"
