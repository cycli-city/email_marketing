from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.core.config import Settings, to_async_database_url
from tests.helpers import PROD_OVERRIDES

GOOD_KEY = Fernet.generate_key().decode()
GOOD_JWT = "j" * 40


def make(**overrides: object) -> Settings:
    base: dict[str, object] = {"encryption_key": GOOD_KEY, "jwt_secret": GOOD_JWT}
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


class TestDatabaseUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("postgresql://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
            ("postgres://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
            ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
            ("postgresql://u:p@h/db?sslmode=require", "postgresql+asyncpg://u:p@h/db?ssl=require"),
        ],
    )
    def test_normalises_to_asyncpg(self, raw: str, expected: str) -> None:
        assert to_async_database_url(raw) == expected

    def test_settings_apply_normalisation(self) -> None:
        assert make(database_url="postgresql://u:p@h/db").database_url.startswith(
            "postgresql+asyncpg://"
        )


class TestOwnerEmails:
    def test_comma_separated_string_is_split_and_lowercased(self) -> None:
        s = make(owner_emails=" Owner@Example.com , second@example.com ,, ")
        assert s.owner_emails == ["owner@example.com", "second@example.com"]

    def test_default_is_empty(self) -> None:
        assert make().owner_emails == []


class TestEncryptionKeyValidation:
    def test_rejects_garbage_key(self) -> None:
        with pytest.raises(ValidationError):
            make(encryption_key="not-a-fernet-key")

    def test_accepts_multiple_keys_for_rotation(self) -> None:
        second = Fernet.generate_key().decode()
        s = make(encryption_key=f"{GOOD_KEY},{second}")
        assert s.encryption_keys == [GOOD_KEY, second]


class TestProductionGuards:
    PROD: dict[str, object] = PROD_OVERRIDES

    def test_production_requires_secrets(self) -> None:
        # explicit empty values: the test environment itself provides ENCRYPTION_KEY / JWT_SECRET
        with pytest.raises(ValidationError) as exc:
            Settings(_env_file=None, app_env="production", encryption_key="", jwt_secret="")
        message = str(exc.value)
        for expected in ("ENCRYPTION_KEY", "JWT_SECRET", "SUPABASE_URL", "OWNER_EMAILS"):
            assert expected in message

    def test_production_forbids_mock_providers(self) -> None:
        with pytest.raises(ValidationError) as exc:
            make(**{**self.PROD, "use_mock_providers": True})
        assert "USE_MOCK_PROVIDERS" in str(exc.value)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("backend_url", "http://localhost:8000"),
            ("backend_url", "http://api.example.com"),  # not https
            ("frontend_url", "https://localhost:3000"),
            ("frontend_url", "https://127.0.0.1"),
        ],
    )
    def test_production_rejects_non_public_urls(self, field: str, value: str) -> None:
        """Unsubscribe links are built from BACKEND_URL; a localhost/http value would break opt-outs."""
        with pytest.raises(ValidationError) as exc:
            make(**{**self.PROD, field: value})
        assert field.upper() in str(exc.value)

    def test_valid_production_config(self) -> None:
        s = make(**self.PROD)
        assert s.app_env == "production"
        assert s.cors_origins == ["https://app.example.com"]  # dev origins are dev-only

    # ── HTTPS / TLS everywhere ───────────────────────────────
    @pytest.mark.parametrize(
        "database_url",
        [
            "postgresql://u:p@db.example.com:5432/postgres",  # no TLS parameter at all
            "postgresql://u:p@db.example.com:5432/postgres?sslmode=disable",
            "postgresql://u:p@db.example.com:5432/postgres?sslmode=prefer",  # may silently downgrade
        ],
    )
    def test_production_requires_tls_to_postgres(self, database_url: str) -> None:
        with pytest.raises(ValidationError) as exc:
            make(**{**self.PROD, "database_url": database_url})
        assert "DATABASE_URL must enforce TLS" in str(exc.value)

    @pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
    def test_postgres_tls_modes_are_accepted(self, mode: str) -> None:
        url = f"postgresql://u:p@db.example.com:5432/postgres?sslmode={mode}"
        assert make(**{**self.PROD, "database_url": url}).database_requires_tls

    def test_a_ca_file_counts_as_enforced_tls(self) -> None:
        s = make(
            **{
                **self.PROD,
                "database_url": "postgresql://u:p@db.example.com:5432/postgres",
                "database_ssl_ca_file": "/etc/ssl/supabase-ca.crt",
            }
        )
        assert s.database_requires_tls

    def test_no_redis_setting_exists_any_more(self) -> None:
        """ADR-024: the platform runs without Redis; a stale REDIS_URL in the environment is simply ignored."""
        assert not hasattr(make(), "redis_url")

    @pytest.mark.parametrize(
        ("field", "value", "needle"),
        [
            ("supabase_url", "http://x.supabase.co", "SUPABASE_URL must be https"),
            (
                "google_redirect_uri",
                "http://api.example.com/cb",
                "GOOGLE_REDIRECT_URI must be https",
            ),
        ],
    )
    def test_production_rejects_plain_http_identity_endpoints(
        self, field: str, value: str, needle: str
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            make(**{**self.PROD, field: value})
        assert needle in str(exc.value)

    def test_development_does_not_require_tls(self) -> None:
        s = make(
            app_env="development",
            database_url="postgresql://u:p@localhost/db",
        )
        assert s.app_env == "development"


class TestProxyTrustAndHosts:
    def test_trusted_proxies_default_to_one_in_production_and_zero_elsewhere(self) -> None:
        assert make(**TestProductionGuards.PROD).effective_trusted_proxies == 1
        assert make().effective_trusted_proxies == 0

    def test_explicit_trusted_proxy_count_wins(self) -> None:
        assert (
            make(
                **{**TestProductionGuards.PROD, "trusted_proxy_count": 2}
            ).effective_trusted_proxies
            == 2
        )
        assert make(trusted_proxy_count=0).effective_trusted_proxies == 0

    def test_allowed_hosts_are_only_the_public_backend_host_in_production(self) -> None:
        assert make(**TestProductionGuards.PROD).allowed_hosts == ["api.example.com"]

    def test_development_also_allows_local_hosts(self) -> None:
        assert {"localhost", "127.0.0.1"} <= set(make().allowed_hosts)

    def test_public_host_comes_from_configuration(self) -> None:
        assert make(**TestProductionGuards.PROD).public_host == "api.example.com"

    def test_dev_cors_includes_localhost(self) -> None:
        assert "http://localhost:3000" in make().cors_origins


class TestOptionalIntegrations:
    def test_unconfigured_by_default(self) -> None:
        s = make()
        assert not s.groq_configured
        assert not s.google_oauth_configured
        assert not s.supabase_auth_configured

    def test_default_groq_models_are_ones_that_exist_today(self) -> None:
        """Regression: llama-3.3-70b-versatile was removed from Groq (verified live 2026-11-19)."""
        s = make()
        assert s.groq_model == "openai/gpt-oss-120b"
        assert s.groq_fast_model == "openai/gpt-oss-20b"
        assert "prompt-guard" in s.groq_guard_model

    def test_groq_configured_when_key_present(self) -> None:
        assert make(groq_api_key="gsk_example").groq_configured

    def test_google_requires_all_three_values(self) -> None:
        assert not make(google_client_id="id", google_client_secret="s").google_oauth_configured
        assert make(
            google_client_id="id", google_client_secret="s", google_redirect_uri="https://x/cb"
        ).google_oauth_configured
