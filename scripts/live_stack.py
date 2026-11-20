"""Run the whole stack locally against the REAL Supabase Auth and the REAL Google OAuth client.

    python scripts/live_stack.py migrate     # create / upgrade the local database
    python scripts/live_stack.py backend     # the API on http://localhost:8000   (blocks)
    python scripts/live_stack.py frontend    # the dashboard on http://localhost:3000 (blocks; builds first if needed)

Why local: the database is a throw-away-but-persistent PostgreSQL in ``backend/.pgdata/live`` (git-ignored), so
nothing here can touch the production Supabase database. Sign-in and Gmail are real: the owner logs in with the real
Supabase account and connects a real mailbox through Google's consent screen.

Secrets come from ``.local/SECRETS.local.md`` and go into the child processes' environment only — they are never
printed, logged or written anywhere else. Stdlib only.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"
SECRETS_FILE = REPO / ".local" / "SECRETS.local.md"
DASHBOARD_URL = "http://localhost:3000"
API_URL = "http://localhost:8000"
DB_NAME = "live"

PROVIDER_KEYS = (
    "GROQ_API_KEY",
    "HUNTER_API_KEY",
    "GEOAPIFY_API_KEY",
    "VERIFALIA_USERNAME",
    "VERIFALIA_PASSWORD",
    "MAILBOXVALIDATOR_API_KEY",
    "ZEROBOUNCE_API_KEY",
    "ABSTRACT_EMAIL_API_KEY",
    "TAVILY_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "FIRECRAWL_API_KEY",
    "PDL_API_KEY",
    "COMPANIES_HOUSE_API_KEY",
    "ADZUNA_APP_ID",
    "ADZUNA_APP_KEY",
    "JOOBLE_API_KEY",
)
_LINE = re.compile(r"^\s*-\s*([A-Z][A-Z0-9_]+)=(.*)$")


def load_secrets() -> dict[str, str]:
    if not SECRETS_FILE.exists():
        sys.exit(f"missing {SECRETS_FILE.relative_to(REPO)} - the real keys live only there")
    found: dict[str, str] = {}
    for raw in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
        match = _LINE.match(raw)
        if not match:
            continue
        # A line may carry a note after the value ("KEY=value   (where it came from)"): the value is the first token.
        # A lone marker character instead of a value ("KEY=-", "KEY=✔ note") means "nothing stored yet".
        tokens = match.group(2).split()
        if tokens and len(tokens[0]) > 1:
            found[match.group(1)] = tokens[0]
    return found


def need(secrets: dict[str, str], *names: str) -> None:
    missing = [n for n in names if n not in secrets]
    if missing:
        sys.exit("missing in .local/SECRETS.local.md: " + ", ".join(missing))


def backend_env(secrets: dict[str, str]) -> dict[str, str]:
    need(
        secrets,
        "SUPABASE_URL",
        "OWNER_LOGIN_EMAIL",
        "ENCRYPTION_KEY",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REDIRECT_URI",
    )
    env = dict(os.environ)
    env.update(
        AOI_DISABLE_DOTENV="1",  # never read a developer .env on top of this
        APP_ENV="development",
        FRONTEND_URL=DASHBOARD_URL,
        BACKEND_URL=API_URL,
        SUPABASE_URL=secrets["SUPABASE_URL"],
        OWNER_EMAILS=secrets["OWNER_LOGIN_EMAIL"],
        ENCRYPTION_KEY=secrets["ENCRYPTION_KEY"],
        GOOGLE_CLIENT_ID=secrets["GOOGLE_CLIENT_ID"],
        GOOGLE_CLIENT_SECRET=secrets["GOOGLE_CLIENT_SECRET"],
        GOOGLE_REDIRECT_URI=secrets["GOOGLE_REDIRECT_URI"],
    )
    for name in PROVIDER_KEYS:  # optional: a provider without its key is simply "not configured"
        if name in secrets:
            env[name] = secrets[name]
    return env


def frontend_env(secrets: dict[str, str]) -> dict[str, str]:
    need(secrets, "SUPABASE_URL", "SUPABASE_ANON_KEY")
    env = dict(os.environ)
    env.update(
        SUPABASE_URL=secrets["SUPABASE_URL"],
        SUPABASE_ANON_KEY=secrets["SUPABASE_ANON_KEY"],
        BACKEND_URL=API_URL,
        APP_URL=DASHBOARD_URL,
    )
    return env


def python_in_backend() -> str:
    for candidate in (BACKEND / ".venv" / "Scripts" / "python.exe", BACKEND / ".venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def with_pg(env: dict[str, str], *command: str) -> int:
    py = python_in_backend()
    return subprocess.call(
        [py, "scripts/with_pg.py", "--name", DB_NAME, "--keep-data", "--", *command],
        cwd=BACKEND,
        env=env,
    )


def npm() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"migrate", "backend", "frontend"}:
        print(__doc__)
        return 2
    secrets = load_secrets()
    if argv[0] == "migrate":
        return with_pg(backend_env(secrets), "alembic", "upgrade", "head")
    if argv[0] == "backend":
        return with_pg(
            backend_env(secrets),
            "python",
            "-m",
            "uvicorn",
            "app.main:app_factory",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        )
    env = frontend_env(secrets)
    if not (FRONTEND / ".next" / "BUILD_ID").exists() or "--rebuild" in argv:
        code = subprocess.call([npm(), "run", "build"], cwd=FRONTEND, env=env)
        if code != 0:
            return code
    return subprocess.call([npm(), "run", "start", "--", "-p", "3000"], cwd=FRONTEND, env=env)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
