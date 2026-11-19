"""Application settings — environment-driven, validated, secrets wrapped in ``SecretStr``.

Every variable from REQUIREMENTS §19 is supported. Optional integrations are genuinely optional:
absence of a key means the integration reports ``unconfigured`` — never a crash, never a fake success.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["development", "test", "production"]
AppRole = Literal["api", "worker", "all"]


def to_async_database_url(url: str) -> str:
    """Normalise any Postgres URL (Supabase gives ``postgresql://``) to the asyncpg driver.

    ``sslmode=`` (libpq style) is rewritten to asyncpg's ``ssl=`` query parameter.
    """
    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in {"postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"}:
        scheme = "postgresql+asyncpg"
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "sslmode":
            query.append(("ssl", value))
        else:
            query.append((key, value))
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # AOI_DISABLE_DOTENV=1 (set by the test suite) guarantees tests never read a developer's real .env.
        env_file=None if os.environ.get("AOI_DISABLE_DOTENV") else (".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── runtime ──────────────────────────────────────────────
    app_env: AppEnv = "development"
    app_role: AppRole = "all"
    log_level: str = "INFO"
    frontend_url: str = "http://localhost:3000"
    backend_url: str = "http://localhost:8000"

    # ── data stores ──────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/aoi"
    database_url_test: str | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 10

    # ── supabase / auth ──────────────────────────────────────
    supabase_url: str | None = None
    supabase_anon_key: SecretStr | None = None
    supabase_service_role_key: SecretStr | None = None
    supabase_jwt_secret: SecretStr | None = None
    owner_emails: list[str] = Field(default_factory=list)

    # ── secrets for our own crypto ───────────────────────────
    # ENCRYPTION_KEY: one or more comma-separated Fernet keys; the first encrypts, all decrypt.
    encryption_key: SecretStr = SecretStr("")
    jwt_secret: SecretStr = SecretStr("")

    # ── AI ───────────────────────────────────────────────────
    groq_api_key: SecretStr | None = None
    groq_model: str = "openai/gpt-oss-120b"  # primary; verified 2026-11-19 (llama-3.3-70b-versatile no longer exists)
    groq_fast_model: str = "openai/gpt-oss-20b"  # cheap classification / extraction
    groq_guard_model: str = (
        "meta-llama/llama-prompt-guard-2-86m"  # prompt-injection screen for untrusted text
    )

    # ── Google / Gmail ───────────────────────────────────────
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_redirect_uri: str | None = None
    google_pubsub_topic: str | None = None
    google_pubsub_audience: str | None = None  # expected `aud` of Pub/Sub push OIDC token

    # ── optional discovery / verification / notification providers ──
    hunter_api_key: SecretStr | None = None
    firecrawl_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # ── behaviour ────────────────────────────────────────────
    # When true, workers use mock providers for every external capability (dev/test/demo wiring).
    use_mock_providers: bool = False
    controller_tick_seconds: int = 30
    gmail_poll_seconds: int = 90

    # ── transport security ───────────────────────────────────
    # Postgres: production requires TLS (`?sslmode=require` in DATABASE_URL). For certificate
    # verification (protects against MITM) point this at the provider's CA bundle (Supabase publishes one).
    database_ssl_ca_file: str | None = None

    # ── abuse / DoS controls (see docs/SECURITY.md) ──────────
    # Number of reverse proxies in front of the app (Render = 1). Decides which X-Forwarded-* entries
    # are trustworthy; unset → 1 in production, 0 elsewhere. A wrong value either lets clients spoof
    # their IP (too low) or makes every client look like the proxy (too high).
    trusted_proxy_count: int | None = Field(default=None, ge=0, le=5)
    rate_limit_per_minute: int = Field(default=300, ge=1)
    rate_limit_sensitive_per_minute: int = Field(default=20, ge=1)
    max_request_bytes: int = Field(default=1_000_000, ge=1_024)
    max_in_flight_requests: int = Field(default=200, ge=1)
    db_statement_timeout_ms: int = Field(default=30_000, ge=1_000)

    @field_validator("owner_emails", mode="before")
    @classmethod
    def _split_owner_emails(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip().lower() for part in value.split(",") if part.strip()]
        return value

    @field_validator("database_url", "database_url_test")
    @classmethod
    def _async_url(cls, value: str | None) -> str | None:
        return to_async_database_url(value) if value else value

    @field_validator("encryption_key")
    @classmethod
    def _valid_fernet_keys(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        for key in (k.strip() for k in raw.split(",") if k.strip()):
            try:
                Fernet(key.encode())
            except (ValueError, TypeError) as exc:  # pragma: no cover - message path
                raise ValueError(
                    "ENCRYPTION_KEY must be Fernet key(s): run `python scripts/gen_secrets.py`"
                ) from exc
        return value

    @model_validator(mode="after")
    def _production_requirements(self) -> Settings:
        """Refuse to start in production with missing secrets or unencrypted transport."""
        if self.app_env != "production":
            return self
        problems = [*self._secret_problems(), *self._transport_problems()]
        if problems:
            raise ValueError("Invalid production configuration: " + ", ".join(problems))
        return self

    def _secret_problems(self) -> list[str]:
        problems: list[str] = []
        if not self.encryption_key.get_secret_value():
            problems.append("ENCRYPTION_KEY")
        if len(self.jwt_secret.get_secret_value()) < 32:
            problems.append("JWT_SECRET (>= 32 chars)")
        if not self.supabase_url:
            problems.append("SUPABASE_URL")
        if not self.owner_emails:
            problems.append("OWNER_EMAILS")
        if self.use_mock_providers:
            problems.append("USE_MOCK_PROVIDERS must be false in production")
        return problems

    def _transport_problems(self) -> list[str]:
        """HTTPS / TLS everywhere: public URLs, identity endpoints and Postgres."""
        problems: list[str] = []
        # Unsubscribe links and OAuth redirects are built from these — a localhost/http value would
        # ship broken opt-outs in real emails (compliance bug), so refuse to start.
        for name, value in (("FRONTEND_URL", self.frontend_url), ("BACKEND_URL", self.backend_url)):
            if not value.startswith("https://") or "localhost" in value or "127.0.0.1" in value:
                problems.append(f"{name} must be a public https:// URL")
        if self.supabase_url and not self.supabase_url.startswith("https://"):
            problems.append("SUPABASE_URL must be https://")
        if self.google_redirect_uri and not self.google_redirect_uri.startswith("https://"):
            problems.append("GOOGLE_REDIRECT_URI must be https://")
        if not self.database_requires_tls:
            problems.append(
                "DATABASE_URL must enforce TLS (append ?sslmode=require, or set DATABASE_SSL_CA_FILE)"
            )
        return problems

    # ── derived helpers ──────────────────────────────────────
    @property
    def cors_origins(self) -> list[str]:
        origins = {self.frontend_url.rstrip("/")}
        if self.app_env != "production":
            origins.update({"http://localhost:3000", "http://127.0.0.1:3000"})
        return sorted(origins)

    @property
    def database_requires_tls(self) -> bool:
        """True when the DB URL forces TLS (``ssl=require|verify-ca|verify-full``) or a CA file is set."""
        if self.database_ssl_ca_file:
            return True
        query = dict(parse_qsl(urlsplit(self.database_url).query))
        return query.get("ssl", "").lower() in {"require", "verify-ca", "verify-full", "true"}

    @property
    def effective_trusted_proxies(self) -> int:
        if self.trusted_proxy_count is not None:
            return self.trusted_proxy_count
        return 1 if self.app_env == "production" else 0

    @property
    def public_host(self) -> str:
        """Host of BACKEND_URL — used for redirects so a forged Host header can never steer them."""
        return urlsplit(self.backend_url).netloc

    @property
    def allowed_hosts(self) -> list[str]:
        hosts = {urlsplit(self.backend_url).hostname or ""}
        if self.app_env != "production":
            hosts.update({"localhost", "127.0.0.1", "test", "testserver"})
        return sorted(h for h in hosts if h)

    @property
    def encryption_keys(self) -> list[str]:
        return [k.strip() for k in self.encryption_key.get_secret_value().split(",") if k.strip()]

    @property
    def google_oauth_configured(self) -> bool:
        return bool(
            self.google_client_id and self.google_client_secret and self.google_redirect_uri
        )

    @property
    def groq_configured(self) -> bool:
        return self.groq_api_key is not None and bool(self.groq_api_key.get_secret_value())

    @property
    def supabase_auth_configured(self) -> bool:
        return bool(self.supabase_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
