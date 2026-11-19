# AI Outreach Intelligence

> **Personal project — all rights reserved.** No licence is granted: this is not open source, it is not a service, and it does not accept contributions or support requests.

A personal, autonomous AI outreach platform: it discovers prospects, researches and qualifies them, writes
genuinely personalised emails, sends them through Gmail within safe limits, watches the inbox 24/7, classifies
replies, stops follow-ups when it should, and notifies only when a human is needed.

Two separate modes — **NEXORA Sales** (find customers for NEXORA AI) and **Job Hunter** (find recruiters /
roles) — with separate campaigns, templates, sender identities and data.

> **Working on this repo?** Start with [`CLAUDE.md`](CLAUDE.md) (operating manual) and [`PROGRESS.md`](PROGRESS.md)
> (what's done / next). The full spec is in [`REQUIREMENTS.md`](REQUIREMENTS.md) and
> [`REQUIREMENTS_AUTONOMY.md`](REQUIREMENTS_AUTONOMY.md); design in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Stack

| Layer | Tech |
|---|---|
| Frontend | Next.js (App Router), TypeScript, Tailwind, shadcn/ui, Recharts, Lucide |
| Backend | Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy 2 (async), Alembic |
| Data | Supabase PostgreSQL (+ Auth) — no Redis |
| Jobs | Postgres-backed queue (`FOR UPDATE SKIP LOCKED`) + in-process workers/cron (persistent, restart-safe) |
| AI | Groq (primary) behind a multi-provider failover layer |
| Email | Gmail API (OAuth 2.0, Pub/Sub watch + History polling) |

## Local development (Windows / macOS / Linux — no Docker required)

```powershell
# 1. Backend
cd backend
python -m pip install uv                # one-time
uv sync                                 # creates .venv from uv.lock
cp ..\.env.example .env                 # then fill it in; generate secrets with:
uv run python scripts/gen_secrets.py

# 2. Run the checks (these are exactly what CI runs)
uv run ruff check app tests scripts migrations
uv run ruff format --check app tests scripts
uv run mypy
uv run pytest                           # integration tests start a throw-away local PostgreSQL automatically

# 3. Apply migrations to a real database and start the API
uv run alembic upgrade head
uv run uvicorn app.main:app_factory --factory --reload
```

Integration tests need a real PostgreSQL. Locally they use the pip-installed `pgserver` (a real Postgres 16
binary — no Docker). In CI they use a Postgres service container. See [`docs/DECISIONS.md`](docs/DECISIONS.md) ADR-006.

## Security tooling (set up once per clone)

```powershell
git config core.hooksPath .githooks        # pre-commit: secret scan · pre-push: secret scan + lint/type/unit gates
python scripts/scan_secrets.py --tree      # scan tracked files (also: --history, --staged, --path DIR)
```

Never commit real credentials: put them in `.local/SECRETS.local.md` (git-ignored) and your host's secret store.
Threat model, controls and honest limits: [`docs/SECURITY.md`](docs/SECURITY.md).

## Repository layout

```
backend/    FastAPI app, in-process workers, Alembic migrations, tests
frontend/   Next.js dashboard
docs/       Architecture, decisions, provider research, deployment, runbooks
.github/    CI (lint · types · tests · audit · secret scan)
```

## Safety stance

This is not a spam tool. Unsubscribe + suppression are enforced centrally, guessed emails are never treated as
verified, sending respects account limits and schedules, approval mode is the default, and provider failover is
never used to dodge quotas or anti-spam controls. See `REQUIREMENTS.md` §13 and `docs/COMPLIANCE.md`.
