"""Generate the two environment files you paste into Render (docs/DEPLOYMENT.md). Nothing here is ever printed or committed.

    python scripts/make_render_env.py [--api-url https://aoi-api.onrender.com] [--dashboard-url https://aoi-dashboard.onrender.com]

Reads ``.local/SECRETS.local.md`` and writes, into the gitignored ``.local/`` folder:

    RENDER_API_ENV.local.env         for the ``aoi-api`` service
    RENDER_DASHBOARD_ENV.local.env   for the ``aoi-dashboard`` service

Production gets its **own** encryption key and token secret (``PROD_ENCRYPTION_KEY`` / ``PROD_JWT_SECRET``, created once and kept
in the secrets file): production data is never encrypted with a key that also lives on a developer laptop. Mailbox connections
therefore have to be made again on the deployed dashboard (the stored tokens belong to the local database anyway).

The API environment is validated with the application's own production rules before it is written, so a missing or insecure
value fails here, not at 3 a.m. on the server.
"""

from __future__ import annotations

import argparse
import os
import secrets as stdlib_secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(BACKEND))
import live_stack  # noqa: E402  (reads .local/SECRETS.local.md; never prints it)

from app.core.config import Settings  # noqa: E402

LOCAL = REPO / ".local"
API_FILE = LOCAL / "RENDER_API_ENV.local.env"
DASHBOARD_FILE = LOCAL / "RENDER_DASHBOARD_ENV.local.env"
OPTIONAL_PROVIDER_KEYS = tuple(k for k in live_stack.PROVIDER_KEYS)


def ensure_production_secrets() -> list[str]:
    """Create PROD_ENCRYPTION_KEY and PROD_JWT_SECRET once, in the same ``- KEY=value`` format the scanner understands."""
    path = live_stack.SECRETS_FILE
    text = path.read_bytes().decode("utf-8")
    present = live_stack.load_secrets()
    created: list[str] = []
    additions: list[str] = []
    if "PROD_ENCRYPTION_KEY" not in present:
        additions.append(f"- PROD_ENCRYPTION_KEY={Fernet.generate_key().decode()}")
        created.append("PROD_ENCRYPTION_KEY")
    if "PROD_JWT_SECRET" not in present:
        additions.append(f"- PROD_JWT_SECRET={stdlib_secrets.token_urlsafe(48)}")
        created.append("PROD_JWT_SECRET")
    if additions:
        newline = "\r\n" if "\r\n" in text else "\n"
        block = (
            newline + "## Production (Render) - generated, used only by the deployed app" + newline
        )
        path.write_bytes(
            (text.rstrip() + newline + block + newline.join(additions) + newline).encode("utf-8")
        )
    return created


def api_environment(secrets: dict[str, str], api_url: str, dashboard_url: str) -> dict[str, str]:
    live_stack.need(
        secrets,
        "DATABASE_URL",
        "SUPABASE_URL",
        "OWNER_LOGIN_EMAIL",
        "PROD_ENCRYPTION_KEY",
        "PROD_JWT_SECRET",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
    )
    env = {
        "BACKEND_URL": api_url,
        "FRONTEND_URL": dashboard_url,
        "DATABASE_URL": secrets["DATABASE_URL"],
        "SUPABASE_URL": secrets["SUPABASE_URL"],
        "OWNER_EMAILS": secrets["OWNER_LOGIN_EMAIL"],
        "ENCRYPTION_KEY": secrets["PROD_ENCRYPTION_KEY"],
        "JWT_SECRET": secrets["PROD_JWT_SECRET"],
        "GOOGLE_CLIENT_ID": secrets["GOOGLE_CLIENT_ID"],
        "GOOGLE_CLIENT_SECRET": secrets["GOOGLE_CLIENT_SECRET"],
        "GOOGLE_REDIRECT_URI": f"{dashboard_url}/integrations/gmail/callback",
    }
    for name in OPTIONAL_PROVIDER_KEYS:
        if name in secrets:
            env[name] = secrets[name]
    return env


def dashboard_environment(
    secrets: dict[str, str], api_url: str, dashboard_url: str
) -> dict[str, str]:
    live_stack.need(secrets, "SUPABASE_URL", "SUPABASE_ANON_KEY")
    return {
        "SUPABASE_URL": secrets["SUPABASE_URL"],
        "SUPABASE_ANON_KEY": secrets["SUPABASE_ANON_KEY"],
        "BACKEND_URL": api_url,
        "APP_URL": dashboard_url,
    }


def check_production(env: dict[str, str]) -> None:
    """Build Settings in production mode from exactly these values; the app's own validators must accept them."""
    saved = dict(os.environ)
    try:
        for key in list(os.environ):
            if key in env or key.upper().startswith(("DATABASE_", "APP_", "FRONTEND_", "BACKEND_")):
                os.environ.pop(key, None)
        os.environ.update({"AOI_DISABLE_DOTENV": "1", "APP_ENV": "production", **env})
        Settings(_env_file=None)  # type: ignore[call-arg]
    finally:
        os.environ.clear()
        os.environ.update(saved)


def write_env(path: Path, env: dict[str, str]) -> None:
    for name, value in env.items():
        if "\n" in value or "\r" in value:
            sys.exit(f"{name} contains a line break; refusing to write it")
    lines = [
        f'{name}="{value}"' if any(c in value for c in " #'") else f"{name}={value}"
        for name, value in env.items()
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def check_url(name: str, value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.path not in ("", "/") or parts.query:
        sys.exit(f"{name} must be a plain https:// address like https://aoi-api.onrender.com")
    return f"https://{parts.netloc}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--api-url", default="https://aoi-api.onrender.com")
    parser.add_argument("--dashboard-url", default="https://aoi-dashboard.onrender.com")
    args = parser.parse_args()
    api_url = check_url("--api-url", args.api_url)
    dashboard_url = check_url("--dashboard-url", args.dashboard_url)

    created = ensure_production_secrets()
    secrets = live_stack.load_secrets()
    api_env = api_environment(secrets, api_url, dashboard_url)
    dashboard_env = dashboard_environment(secrets, api_url, dashboard_url)
    check_production(
        api_env
    )  # raises with the list of problems (names only) if production would refuse to start

    LOCAL.mkdir(exist_ok=True)
    write_env(API_FILE, api_env)
    write_env(DASHBOARD_FILE, dashboard_env)
    if created:
        print("created in .local/SECRETS.local.md:", ", ".join(created))
    print(f"API       -> {API_FILE.relative_to(REPO)}  ({len(api_env)} variables)")
    print(f"Dashboard -> {DASHBOARD_FILE.relative_to(REPO)}  ({len(dashboard_env)} variables)")
    print(
        "Production settings validated with the application's own rules (HTTPS everywhere, TLS to the database)."
    )
    print(f"Addresses used: API {api_url} | dashboard {dashboard_url}")
    print(
        "Providers configured:",
        ", ".join(sorted(k for k in api_env if k in OPTIONAL_PROVIDER_KEYS)) or "none",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
