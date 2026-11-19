# DECISIONS (ADR log)

Format: **ADR-NNN — title** · status · date. Newest at bottom. Deviations from the owner's spec are marked **[DEVIATION]**.

**ADR-001 — Monorepo, single backend image with roles** · accepted · 2026-11-19
`backend/` + `frontend/` in one repo; the backend image runs as `api`, `worker` or `all` (`APP_ROLE`). Why: one CI, atomic contract changes, cheapest always-on deployment.

**ADR-002 — Postgres is the source of truth for work; Redis is transport only** · accepted · 2026-11-19
Controller derives work from DB state; deterministic ARQ job ids; janitor/reconcilers. Why: restart-safe autonomy, no duplicate sends (REQUIREMENTS_AUTONOMY §A).

**ADR-003 — Supabase Auth as identity provider; backend verifies its JWTs** · accepted · 2026-11-19
Spec lists both Supabase and `JWT_SECRET`. Supabase Auth handles sign-in (email + Google); backend verifies tokens and maps to `users/workspaces`. `JWT_SECRET` signs *our own* short-lived tokens (unsubscribe links, OAuth `state`, internal webhooks). Why: no home-grown password storage.

**ADR-004 — Tenant isolation in code + RLS deny-all** · accepted · 2026-11-19
Backend uses a privileged DB connection, so RLS can't be the primary control; it is enabled deny-all so the public anon key can never read data via PostgREST. Primary control: workspace-scoped repositories + isolation tests.

**ADR-005 — Sending failover never evades limits** · accepted · 2026-11-19
Per REQUIREMENTS_AUTONOMY §D/§I: email-send limit exhaustion waits for the next window; failover only on technical outage/auth error and only to campaign-authorised senders. No account/key rotation anywhere.

**ADR-006 — Local dev without Docker** · accepted · 2026-11-19
Machine has no Docker/psql/Redis. Unit tests are I/O-free; integration tests run in GitHub Actions service containers (Postgres 16 + Redis 7) and locally against a pip-installable Postgres + `fakeredis`. **Confirmed 2026-11-19:** `pgserver` runs a real PostgreSQL 16.2 on this Windows machine; integration tests auto-start it (`backend/tests/conftest.py`), and `backend/scripts/with_pg.py` runs any command (alembic, uvicorn…) against a throw-away cluster. ARQ-against-real-Redis tests run in CI only (`redis:7` service) until a local Redis is available; locally a queue abstraction + `fakeredis` is used.

**ADR-007 — pgvector not used in v1** · accepted · 2026-11-19
Spec says "only if genuinely useful". No semantic retrieval requirement in v1; FTS covers lead search. Revisit if reply-similarity / KB features are added.

**ADR-008 — Always-on hosting is a cost decision for the owner** · open · 2026-11-19
24/7 autonomy needs a process that never sleeps. Render free web services spin down when idle; a paid always-on instance (≈ US$7/mo class) is the reliable route; embedded worker keeps it to one service. To confirm with owner before deploying.

**ADR-009 — Gmail OAuth: scopes and token lifetime** · accepted · 2026-11-19
Need `gmail.send` + read access for replies (`gmail.readonly` or `gmail.modify`, both *restricted* scopes). For a single owner-operated app: OAuth consent screen set to **In production (unverified)** so refresh tokens don't expire after 7 days (they do in *Testing* status); owner is the sole user (<100 user cap). If this is ever offered to third parties, Google's restricted-scope verification/security assessment is required — documented in `docs/GOOGLE_OAUTH.md`.

**ADR-010 — Jurisdiction-aware compliance defaults** · accepted · 2026-11-19
Some target countries are stricter than the spec's generic wording (e.g. Germany's UWG requires prior consent for unsolicited commercial email, including B2B). Policies are data with conservative defaults (Germany B2B cold marketing = blocked unless consent recorded; overridable by the owner with an explicit warning + audit entry). Not legal advice; documented in `docs/COMPLIANCE.md`.

**ADR-011 — Job-hunter CVs are Drive links chosen per role** · accepted · 2026-11-19
Owner keeps one resume per role on Google Drive. `resume_assets` stores role → link; emails include the right link; attachments are optional. No send of CV-requiring mail without a configured link.

**ADR-012 — Single-owner personal tool** · accepted · 2026-11-19 · **[DEVIATION: narrows spec's "multi-user" hints]**
Owner clarified the project is for personal use only. Decisions: (a) Supabase signup disabled / backend enforces `OWNER_EMAILS` allowlist; first login auto-provisions the single workspace; (b) keep `workspace_id` on tenant tables + isolation tests (spec §14 "proper tenant isolation if multi-user"), but no org/member management UI, billing, or plans; (c) Gmail OAuth consent screen stays owner-only (ADR-009), so no restricted-scope verification; (d) UI is tuned for one operator (fewer settings screens, no invites/roles). Compliance, safety and autonomy requirements are **unchanged**.

**ADR-013 — Gmail via direct REST (httpx), not google-api-python-client** · accepted · 2026-11-19
Gmail's REST API is the official API; calling it with `httpx` keeps everything async, trivially mockable with `respx`, and avoids discovery-document fetches and a heavy sync client. OAuth code exchange / refresh and Pub/Sub OIDC verification are implemented with `httpx` + PyJWT (JWKS). Scopes and flows are unchanged.
