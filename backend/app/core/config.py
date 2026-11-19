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
    redis_url: str = "redis://localhost:6379/0"

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
    groq_model: str = "llama-3.3-70b-versatile"

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
        if self.app_env != "production":
            return self
        missing: list[str] = []
        if not self.encryption_key.get_secret_value():
            missing.append("ENCRYPTION_KEY")
        if len(self.jwt_secret.get_secret_value()) < 32:
            missing.append("JWT_SECRET (>= 32 chars)")
        if not self.supabase_url:
            missing.append("SUPABASE_URL")
        if not self.owner_emails:
            missing.append("OWNER_EMAILS")
        if self.use_mock_providers:
            missing.append("USE_MOCK_PROVIDERS must be false in production")
        # Unsubscribe links and OAuth redirects are built from these — a localhost/http value would
        # ship broken opt-outs in real emails (compliance bug), so refuse to start.
        for name, value in (("FRONTEND_URL", self.frontend_url), ("BACKEND_URL", self.backend_url)):
            if not value.startswith("https://") or "localhost" in value or "127.0.0.1" in value:
                missing.append(f"{name} must be a public https:// URL")
        if missing:
            raise ValueError("Invalid production configuration: " + ", ".join(missing))
        return self

    # ── derived helpers ──────────────────────────────────────
    @property
    def cors_origins(self) -> list[str]:
        origins = {self.frontend_url.rstrip("/")}
        if self.app_env != "production":
            origins.update({"http://localhost:3000", "http://127.0.0.1:3000"})
        return sorted(origins)

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
