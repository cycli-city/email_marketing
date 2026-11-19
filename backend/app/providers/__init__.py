"""Provider framework: adapters behind capability protocols, orchestrated by the ProviderManager (ADR-031)."""

from app.providers.errors import (
    AllProvidersUnavailableError,
    AuthError,
    ForbiddenReason,
    InvalidResponseError,
    PermanentProviderError,
    PermissionDeniedError,
    ProviderError,
    ProviderLimitReachedError,
    QuotaExhaustedError,
    RateLimitedError,
    TransientProviderError,
    UncertainOutcomeError,
)
from app.providers.manager import ProviderManager, ProviderView
from app.providers.registry import Provider, ProviderRegistry
from app.providers.spec import CallOutcome, ProviderResult, ProviderSpec

__all__ = [
    "AllProvidersUnavailableError",
    "AuthError",
    "CallOutcome",
    "ForbiddenReason",
    "InvalidResponseError",
    "PermanentProviderError",
    "PermissionDeniedError",
    "Provider",
    "ProviderError",
    "ProviderLimitReachedError",
    "ProviderManager",
    "ProviderRegistry",
    "ProviderResult",
    "ProviderSpec",
    "ProviderView",
    "QuotaExhaustedError",
    "RateLimitedError",
    "TransientProviderError",
    "UncertainOutcomeError",
]
