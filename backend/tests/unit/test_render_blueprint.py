"""render.yaml must describe a deployment the application will actually start in, and must never carry a secret."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.config import Settings
from tests.helpers import make_production_settings, running

BLUEPRINT = Path(__file__).resolve().parents[3] / "render.yaml"
SECRET_WORDS = (
    "KEY",
    "SECRET",
    "PASSWORD",
    "TOKEN",
    "USERNAME",
    "DATABASE_URL",
    "SUPABASE_URL",
    "OWNER_EMAILS",
    "CLIENT_ID",
    "APP_ID",
)


@pytest.fixture(scope="module")
def blueprint() -> dict[str, Any]:
    return yaml.safe_load(BLUEPRINT.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def service(blueprint: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [s for s in blueprint["services"] if s["name"] == name]
    return found  # type: ignore[no-any-return]


def env(svc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["key"]: item for item in svc["envVars"]}


class TestApi:
    def test_it_is_always_on_because_the_background_worker_lives_in_it(
        self, blueprint: dict[str, Any]
    ) -> None:
        api = service(blueprint, "aoi-api")
        assert (
            api["plan"] != "free"
        )  # a free instance sleeps after 15 minutes and would stop every scheduled job
        assert api["type"] == "web"
        assert api["runtime"] == "python"
        assert api["rootDir"] == "backend"

    def test_migrations_run_before_each_deploy_goes_live(self, blueprint: dict[str, Any]) -> None:
        assert "alembic upgrade head" in service(blueprint, "aoi-api")["preDeployCommand"]

    async def test_the_health_check_is_a_route_the_app_really_serves(
        self, blueprint: dict[str, Any]
    ) -> None:
        path = service(blueprint, "aoi-api")["healthCheckPath"]
        async with running() as (_, client):
            response = await client.get(path)
        assert (
            response.status_code != 404
        )  # 200 when the database is up, 503 when it is not; never "no such route"

    def test_it_starts_the_application_factory_on_the_port_render_provides(
        self, blueprint: dict[str, Any]
    ) -> None:
        command = service(blueprint, "aoi-api")["startCommand"]
        for needed in ("app.main:app_factory", "--factory", "$PORT", "0.0.0.0"):  # noqa: S104 - Render needs all interfaces
            assert needed in command

    def test_it_runs_in_production_mode_without_dotenv(self, blueprint: dict[str, Any]) -> None:
        variables = env(service(blueprint, "aoi-api"))
        assert variables["APP_ENV"]["value"] == "production"
        assert variables["AOI_DISABLE_DOTENV"]["value"] == "1"

    def test_every_variable_is_one_the_application_reads(self, blueprint: dict[str, Any]) -> None:
        known = {name.upper() for name in Settings.model_fields}
        unknown = (
            set(env(service(blueprint, "aoi-api")))
            - known
            - {"PYTHON_VERSION", "AOI_DISABLE_DOTENV"}
        )  # the latter is read straight from the environment
        assert unknown == set()

    def test_everything_production_refuses_to_start_without_is_declared(
        self, blueprint: dict[str, Any]
    ) -> None:
        declared = set(env(service(blueprint, "aoi-api")))
        required = {
            "ENCRYPTION_KEY",
            "JWT_SECRET",
            "SUPABASE_URL",
            "OWNER_EMAILS",
            "BACKEND_URL",
            "FRONTEND_URL",
            "DATABASE_URL",
        }
        assert required <= declared

    def test_every_provider_key_the_settings_know_about_is_declared(
        self, blueprint: dict[str, Any]
    ) -> None:
        provider_fields = {
            "GROQ_API_KEY", "HUNTER_API_KEY", "GEOAPIFY_API_KEY", "FIRECRAWL_API_KEY", "VERIFALIA_USERNAME", "VERIFALIA_PASSWORD",
            "MAILBOXVALIDATOR_API_KEY", "ZEROBOUNCE_API_KEY", "ABSTRACT_EMAIL_API_KEY", "TAVILY_API_KEY", "BRAVE_SEARCH_API_KEY",
            "PDL_API_KEY", "COMPANIES_HOUSE_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "JOOBLE_API_KEY",
        }  # fmt: skip
        assert provider_fields <= set(env(service(blueprint, "aoi-api")))

    def test_production_accepts_a_configuration_with_exactly_these_variables(self) -> None:
        assert make_production_settings().app_env == "production"


class TestDashboard:
    def test_it_needs_exactly_what_the_frontend_validates_at_startup(
        self, blueprint: dict[str, Any]
    ) -> None:
        keys = set(env(service(blueprint, "aoi-dashboard")))
        assert {"SUPABASE_URL", "SUPABASE_ANON_KEY", "BACKEND_URL", "APP_URL"} <= keys

    def test_it_is_always_on_and_checked_on_a_page_that_needs_no_login(
        self, blueprint: dict[str, Any]
    ) -> None:
        dashboard = service(blueprint, "aoi-dashboard")
        assert dashboard["plan"] != "free"
        assert dashboard["healthCheckPath"] == "/login"
        assert dashboard["runtime"] == "node"
        assert dashboard["rootDir"] == "frontend"
        assert "npm run build" in dashboard["buildCommand"]
        assert "$PORT" in dashboard["startCommand"]

    def test_it_never_receives_a_server_side_secret(self, blueprint: dict[str, Any]) -> None:
        forbidden = {
            "DATABASE_URL",
            "ENCRYPTION_KEY",
            "JWT_SECRET",
            "GOOGLE_CLIENT_SECRET",
            "SUPABASE_SERVICE_ROLE_KEY",
        }
        assert forbidden.isdisjoint(env(service(blueprint, "aoi-dashboard")))


class TestNoSecretsInTheFile:
    def test_every_sensitive_variable_is_entered_in_the_dashboard_not_written_here(
        self, blueprint: dict[str, Any]
    ) -> None:
        for svc in blueprint["services"]:
            for key, item in env(svc).items():
                if any(word in key for word in SECRET_WORDS):
                    assert item.get("sync") is False, f"{svc['name']}: {key} must be sync: false"
                    assert "value" not in item, f"{svc['name']}: {key} must not carry a value"

    def test_nothing_in_the_file_looks_like_a_credential(self) -> None:
        text = BLUEPRINT.read_text(encoding="utf-8")
        for needle in (
            "postgresql://",
            "postgres://",
            "sk-",
            "AIza",
            "gsk_",
            "sbp_",
            "eyJ",
            "-----BEGIN",
        ):
            assert needle not in text

    def test_both_services_share_a_region_so_they_talk_over_a_short_link(
        self, blueprint: dict[str, Any]
    ) -> None:
        assert (
            service(blueprint, "aoi-api")["region"] == service(blueprint, "aoi-dashboard")["region"]
        )
