# Deployment runbook (Render + Supabase)

Two always-on services on Render, one database on Supabase. Everything is described in [`render.yaml`](../render.yaml); no secret is
in this repository. Decisions and reasons: ADR-041 in [`DECISIONS.md`](DECISIONS.md).

| Service | What it is | Plan |
|---|---|---|
| `aoi-api` | FastAPI backend **plus** the background worker and mailbox scheduler (reads replies, settles unknown sends) | Starter, always on |
| `aoi-dashboard` | The Next.js dashboard (holds your session cookie, proxies API calls) | Starter, always on |
| Supabase project | Postgres + sign-in (already created) | Free for now |

A free Render instance sleeps after 15 minutes without traffic, which would stop the background worker. That is why both are paid.

## What it costs

| Item | Cost | Status of the number |
|---|---|---|
| Render `aoi-api` (Starter) | about $7 / month | **not verified** — Render's price table could not be read when this was written; the checkout page shows the real figure before you pay |
| Render `aoi-dashboard` (Starter) | about $7 / month | same |
| Supabase | $0 now; **Pro is $25 / month** | verified on supabase.com/pricing. Free projects are paused after one week of inactivity and have **no backups**; Pro never pauses and keeps 7 days of backups. Worth upgrading before the first real campaign. |
| Vercel | not used | Pro would be $20 / month per seat (verified); its free Hobby plan is for non-commercial use, which NEXORA is not |

Expected total today: about **$14 / month** (two Render instances), **$39** once Supabase Pro is switched on.

## One-time setup (you, about 20 minutes)

1. **Create a Render account** at render.com and add a payment card (Account Settings → Billing).
2. **Generate the two environment files** (I do this; they land in the gitignored `.local/` folder):
   `python backend/scripts/make_render_env.py` → `.local/RENDER_API_ENV.local.env` and `.local/RENDER_DASHBOARD_ENV.local.env`.
   They contain real secrets: open them in your editor, never paste them into a chat, an issue or a commit.
3. **Render → New → Blueprint**, connect `https://github.com/cycli-city/email_marketing`, branch `main`. Render reads `render.yaml`
   and proposes both services. It asks for the values marked `sync: false`.
4. For each service use **Environment → Add from .env** and paste the matching file's contents. (API file → `aoi-api`; dashboard
   file → `aoi-dashboard`.)
5. **Apply** the blueprint. The first build takes a few minutes; the API runs `alembic upgrade head` before it goes live.
6. **Check the two web addresses Render gave you.** If they are not `https://aoi-api.onrender.com` and
   `https://aoi-dashboard.onrender.com` (the name may be taken and get a suffix), tell me the real ones and I regenerate the files
   (`make_render_env.py --api-url … --dashboard-url …`); you paste them again and redeploy.
7. **Google Cloud Console → your OAuth client → Authorised redirect URIs → add**
   `https://<your dashboard address>/integrations/gmail/callback` (keep the localhost one for local work).
8. Open the dashboard address, **sign in**, go to **Settings → Integrations** and **connect both mailboxes again**. (The mailbox
   connections made on your laptop belong to the local database; production has its own encryption key.)

## Checks after every deploy

- `https://<api address>/readyz` answers `{"status":"ready","checks":{"database":"ok"}}`.
- Sign-in works and **Settings → Integrations** lists your mailboxes as Active.
- In the API logs: `queue.worker_started` and, every 90 seconds, `inbox.poll` jobs succeeding.

## Operating it

- **Deploys are manual** (`autoDeploy: false`): push to `main` only after CI is green, then press **Manual Deploy** on each service.
  A failed pre-deploy migration stops the release and leaves the running version untouched.
- **Rollback:** Render → the service → Events → pick the previous deploy → Rollback. Migrations are additive; if one ever must be
  reverted, run `alembic downgrade -1` from a Render shell *before* rolling the code back.
- **Rotating a secret:** change it in Render's Environment tab and redeploy. The production encryption key (`PROD_ENCRYPTION_KEY`)
  encrypts stored mailbox tokens: changing it means reconnecting the mailboxes.
- **Logs** are structured JSON and contain no secrets or message content; Render → Logs.
- **Supabase free tier:** the worker touches the database every 90 seconds, so the project should not look inactive, but there are no
  backups on the free plan — upgrade to Pro before real data accumulates.

## Why the database address matters

Supabase's *direct* connection address is IPv6-only; Render is IPv4-only. The connection string in `DATABASE_URL` must be the
**pooler in session mode (port 5432)**, which the generator takes from your secrets. (Transaction mode, port 6543, breaks
asyncpg's prepared statements and is not used.)
