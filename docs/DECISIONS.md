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

**ADR-008 — Hosting: Render free tier kept awake by UptimeRobot** · accepted · 2026-11-19 (owner decision)
24/7 autonomy needs a process that never sleeps. Owner chose the **free** Render web service + **UptimeRobot** pinging it. Consequences designed for: (a) one service only, `APP_ROLE=all` (API + ARQ worker in-process) because the free tier has no separate background workers; (b) point the UptimeRobot monitor (5-min interval, under Render's 15-min idle spin-down) at **`/readyz`** — it also touches the database, which keeps a free Supabase project from auto-pausing after a week of inactivity; (c) the free instance can still restart/redeploy at any time, so the system is restart-safe by construction (ADR-002: DB is the source of truth, exactly-once send protocol); (d) free instances are small (0.1 CPU / 512 MB) — keep memory lean, set `max_in_flight_requests` conservatively; (e) monthly free instance hours (750) cover one always-on service; (f) risk accepted: Render may change free-tier terms, and a missed ping window means a cold start (~30–60 s) before the next job tick.

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

**ADR-014 — Browser sessions: backend-for-frontend with httpOnly cookies (Phase 2)** · accepted (to implement) · 2026-11-19
Owner asked for maximal protection against session hijacking and XSS token theft. A bearer token kept in `localStorage` is readable by any injected script. Design: Next.js server (BFF) holds the Supabase session in **`HttpOnly; Secure; SameSite=Strict`** cookies (via `@supabase/ssr`), validates it server-side, and forwards API calls to FastAPI with the bearer token attached **on the server** — the browser JavaScript never sees an access or refresh token. State-changing BFF routes additionally verify the `Origin` header (CSRF). Supabase refresh-token rotation + reuse detection stays on. Backend JWT verification pins the algorithm, checks `aud`/`iss`/`exp`/`nbf`, requires a verified email on the `OWNER_EMAILS` allow-list. The current direct-fetch health call is the only browser→API traffic until this lands (it carries no credentials).

**ADR-015 — Strict nonce-based CSP; Cache Components off** · accepted · 2026-11-19 · **[DEVIATION from scaffold defaults]**
A nonce + `strict-dynamic` policy (no `unsafe-inline` for scripts) is the strongest practical XSS defence but needs per-request rendering. The create-next-app defaults (`cacheComponents`, `partialPrefetching`) prerender a static shell that cannot carry a nonce, so they are disabled; every route is dynamic (`force-dynamic`), the policy is built in `src/proxy.ts`, and the nonce is forwarded to `next-themes` and Base UI's `CSPProvider`. Verified in a real browser: zero violations on the app and 404 page; injected inline scripts, remote scripts, event handlers, `javascript:` links and cross-origin exfiltration are all blocked. `style-src-attr 'unsafe-inline'` (attributes only, cannot run script) is the one deliberate allowance. Trusted Types (`require-trusted-types-for 'script'`) is a candidate future hardening once the dependency tree is confirmed compatible.

**ADR-016 — Repository stays private** · accepted · 2026-11-19
Owner asked whether to go public for unlimited Actions minutes. Decision: **stay private**. Reasons: the repo holds personal context, outreach strategy, prompts and compliance reasoning; a single leaked credential in a public history is compromised within seconds (bots), whereas in a private repo the blast radius is contained; hiding the code from attackers is a free extra layer even though security never depends on it. Cost: private repos get 2,000 free Actions minutes/month (enough with the current ~15–20 min per push; cancel-in-progress is on) and lose free CodeQL/secret-scanning-push-protection/branch-protection — compensated by `scan_secrets.py` (pre-commit, pre-push, CI, bundle scan), gitleaks (pinned, full history), ruff-bandit + mypy + ESLint security bans, hash-verified `pip-audit`, `npm audit`, Dependabot, and SHA-pinned actions. CodeQL activates automatically if the repo is ever made public. Revisit: publishing a sanitised portfolio copy later (after a history rewrite and a fresh secret audit).

**ADR-017 — HTTPS/TLS everywhere, enforced by configuration** · accepted · 2026-11-19
Production refuses to start unless: `FRONTEND_URL`/`BACKEND_URL`/`SUPABASE_URL`/`GOOGLE_REDIRECT_URI` are public `https://`; `DATABASE_URL` enforces TLS (`sslmode=require|verify-*` or a CA file); `REDIS_URL` is `rediss://` (explicit opt-out flag only for a private network). The API redirects plain HTTP with 308 to the *configured* host, sends HSTS (2 years), rejects unknown `Host` headers, and the frontend sends HSTS + `upgrade-insecure-requests`. Optional `DATABASE_SSL_CA_FILE` upgrades Postgres from encrypted to **verified** (anti-MITM) TLS — to be wired with the Supabase CA at deployment. Outbound provider calls are HTTPS-only (adapter requirement).

**ADR-018 — Abuse and DoS controls; honest scope** · accepted · 2026-11-19
Implemented: per-client rate limits (Redis sliding-window counter with in-memory fallback so protection survives a Redis outage; stricter buckets for auth/unsubscribe), proxy-aware client IP that cannot be spoofed via `X-Forwarded-For` (N-th entry from the right, `TRUSTED_PROXY_COUNT`), global in-flight cap (fast 503 instead of memory exhaustion; liveness exempt), request-body size limit (declared and streamed, correct 413), DB statement/idle-transaction timeouts, bounded in-memory limiter. **Honest limit:** application code cannot absorb a volumetric network flood; that is the job of the platform edge (Vercel and Render sit behind their own DDoS mitigation). Our responsibility is to stay cheap and bounded behind it. Free-tier note: an attacker who exhausts the free instance can take the service down — acceptable for a personal tool; Cloudflare in front is the upgrade path.

**ADR-019 — Supply-chain hardening** · accepted · 2026-11-19
Third-party GitHub Actions are pinned to full commit SHAs (Dependabot keeps them current); Python deps are locked (`uv.lock`) and audited from a hash-verified export; npm uses `package-lock.json` + `npm audit` on the production tree; the gitleaks binary is pinned with a verified SHA-256; checkouts use `persist-credentials: false`; build-time-only tooling (`shadcn`) lives in devDependencies so it is not in the shipped tree. `redis` is capped `<6` because `arq` requires it (found via Dependabot); `requires-python` is bounded `<3.14`.

**ADR-020 — No sonner; toasts via Base UI later** · accepted · 2026-11-19
`sonner` injects CSS at runtime with no nonce support, which a strict CSP rightly blocks. Removed (unused). Notifications will use Base UI Toast with `CSPProvider`.
