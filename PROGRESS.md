# PROGRESS — live status & feature-completion report

_Last updated: 2026-11-19. Update this file after every meaningful change._

Legend: ✅ done & tested · 🟡 implemented, awaiting credentials · 🔒 depends on external approval · ⬜ not started · 🚧 in progress

## Phase status
| # | Phase | Status | Notes |
|---|---|---|---|
| 1 | Architecture & scaffold | ✅ | Backend, 34-table schema, API shell, Next.js shell, contract pipeline, E2E. CI green on GitHub. |
| 1b | Security baseline | ✅ | Secret scanner + hooks, nonce CSP, HTTPS/TLS enforcement, rate limiting/DoS controls, supply-chain pinning. `docs/SECURITY.md`. |
| 1c | **No Redis** (ADR-024) | ✅ | Redis/ARQ removed everywhere; queue will be a Postgres table; rate limiter in-process. |
| 1d | Public-readiness tooling (ADR-027) | ✅ | `scripts/prepublic_audit.py` (+ CI step), sanitised docs, personal data moved to `.local/`. |
| 2 | Postgres job queue + provider framework + auth + Gmail | 🚧 **started** — Supabase project + schema live, Groq verified | Queue (`job_queue`, SKIP LOCKED, backoff, janitor) → ProviderManager (failover/credits) → Supabase auth with BFF cookies → Gmail OAuth. Needs Phase-2 keys. |
| 3 | Lead discovery (Overture/OSM/Geoapify + SSRF-guarded crawler, extraction, verification, qualification) | ⬜ | Sources in `docs/PROVIDERS.md`. |
| 4 | Outreach (templates, generation, campaigns, sender adapters, approvals, adaptive ramp) | ⬜ | Sending architecture: ADR-025/026. |
| 5 | Reply intelligence (sync, classify, notify, follow-ups) | ⬜ | |
| 6 | Dashboard (all pages + System Operations) | ⬜ | Shell + System status page exist. |
| 7 | Testing & hardening | ⬜ | |
| 8 | Deployment (Supabase, Render, Vercel) + docs | ⬜ | Hosting terms open (ADR-023). |

## Quality gate (local, 2026-11-19)
Backend: `ruff` ✅ · `ruff format` ✅ · `mypy --strict` ✅ (47 files) · `pytest` ✅ **205 passed** (unit + integration on real PostgreSQL 16) · OpenAPI export current ✅.
Frontend: `eslint` (incl. XSS-sink bans) ✅ · `tsc` ✅ · `prettier` ✅ · `vitest` ✅ **35 passed** · `next build` ✅ · **Playwright 22/22 against the real API + Postgres (no Redis)** incl. axe WCAG A/AA, nonce CSP, blocked injection/exfiltration/clickjacking.
Security/public-readiness: `scan_secrets.py` clean (tree, history, browser bundle) ✅ · `prepublic_audit.py` clean on tree + workflows ✅ · **git history fails the audit by design** (ADR-027) ⇒ publish via a clean-slate repo.

## Feature-completion report (REQUIREMENTS.md §21)
### Fully implemented and tested
- Typed settings for every spec env var; production guards (secrets, public https URLs, TLS-enforced Postgres, no mock providers).
- Fernet encryption-at-rest with key rotation; purpose-bound signed tokens; injectable clock.
- Relational schema (34 tables) + verified migration with DB-enforced safety rules (guessed email never verified; one message per campaign/contact/step; idempotency keys; score 0–100; RLS on every table; append-only audit log).
- FastAPI shell: request-ID, security headers + API CSP, HSTS (prod), HTTP→HTTPS redirect, trusted hosts, in-process rate limiting (bounded memory, spoof-proof client IP), concurrency cap, body-size limit, DB timeouts, uniform error envelope, `/healthz`, `/readyz` (database).
- Contract-first pipeline (OpenAPI → generated TS types → typed client) with drift checks.
- Next.js 16 dashboard shell: responsive sidebar, light/dark, System status page, custom 404/error pages, strict nonce CSP + hardening headers.
- Secret scanning (hooks, CI, bundle), gitleaks, pre-public audit, GitHub helper that never exposes the token.
### Implemented but awaiting credentials
_none yet_
### Dependent on external approval / spend
- Gmail restricted scope: owner-only unverified OAuth is enough for personal use (ADR-009/012).
- Cold-outreach sending quality: Google Workspace mailbox(es) on a dedicated sending domain (owner spend decision, ADR-026).
### Not yet implemented
Phases 2–8.

## Next up (in order)
1. Push this work to the (still private) repo; confirm CI green without Redis.
2. Phase 2: Postgres job queue → ProviderManager → auth (BFF) → Gmail OAuth. **I need the Phase-2 keys listed in `.local/SECRETS.local.md`** (Supabase, Groq, Google OAuth).
3. Public publication (only when the owner provides the new repo + new token — ADR-027).

## Owner action checklist (agreed 2026-11-19)
**Done 2026-11-19:** Supabase project created + migrated; Groq key verified; repo made public (existing repo, ADR-027 amendment). **Still to do by the owner:** revoke the account-level Supabase token (no longer needed); optionally rotate the GitHub token; set the repo security toggles (Settings → Code security: secret scanning + push protection; Actions → Fork PR workflows: require approval for all outside collaborators).
**Long lead time (warm-up takes weeks) — start now:** (4) buy a separate sending domain (not `nexora-ai.co.in`) — **owner will buy later; NEXORA real sending waits for it (Job Hunter can start earlier)**; (5) Google Workspace Starter on it, 2 mailboxes (`divyansh@…`, `founder@…`), use them like a human for ~2 weeks; (6) tell me the domain + DNS host → I generate the exact SPF/DKIM/DMARC records; enable Google Postmaster Tools.
**Soon:** (7) Telegram bot token (@BotFather) + chat id; (8) Google Cloud OAuth client (one External project, "In production") once the mailboxes exist; (9) the free-tier provider keys listed in `.local/SECRETS.local.md` (no rush; asked again at Phase 3).
**Before the first real send (Phase 4):** NEXORA postal address for footers; may "paying client" be mentioned; student vs graduated wording.
**Phase 8:** hosting for the dashboard (Vercel Hobby is non-commercial by contract) — I will research commercial-friendly options then.
## Open questions for the owner (ask only when blocking)
1. **Public repo (ADR-027):** create a NEW empty public repo + a new fine-grained token for it (Contents, Workflows, Metadata rw; Actions rw); revoke the old token. I then publish one sanitised commit.
2. **Sending setup (ADR-026):** approve the quality setup (dedicated cold-outreach domain + 2–3 Google Workspace mailboxes, Reply-To the founder address) or choose another option; supply registrar/DNS host when we get there.
3. **Hosting (ADR-023):** Vercel Hobby is non-commercial by contract — decide before Phase 8.
4. Confirm the NEXORA postal address for email footers and whether "paying client" may be mentioned.
5. Google Drive links per role resume (promised "later").
6. ~~Student or graduate?~~ **Graduate** (confirmed 2026-11-19) — job mails describe him as a recent graduate.

## Credentials received
GitHub token ✅ (in `.local/`, never listed here). Everything else is pending and listed, phase by phase, in `.local/SECRETS.local.md`. Real mailbox addresses/phone/postal address are in `.local/PRIVATE_CONTEXT.local.md`.
