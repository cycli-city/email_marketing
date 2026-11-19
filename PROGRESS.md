# PROGRESS — live status & feature-completion report

_Last updated: 2026-11-19. Update this file after every meaningful change._

Legend: ✅ done & tested · 🟡 implemented, awaiting credentials · 🔒 depends on external approval · ⬜ not started · 🚧 in progress

## Phase status
| # | Phase | Status | Notes |
|---|---|---|---|
| 1 | Architecture & scaffold | 🚧 backend ✅ / frontend ⬜ / CI ✅ (unproven until first push) | Backend scaffold, 34-table schema + migration, API shell done. **Next: Next.js scaffold.** |
| 2 | Auth + integrations (Supabase JWT, Gmail OAuth, Groq, Redis/ARQ, provider framework) | ⬜ | |
| 3 | Lead discovery (crawler, extraction, verification, qualification) | ⬜ | |
| 4 | Outreach (templates, generation, campaigns, Gmail send, approvals, limits) | ⬜ | |
| 5 | Reply intelligence (sync, classify, notify, follow-ups) | ⬜ | |
| 6 | Dashboard (all pages + System Operations) | ⬜ | |
| 7 | Testing & hardening | ⬜ | |
| 8 | Deployment (Supabase, Render, Vercel) + docs | ⬜ | |

## Quality gate (local, 2026-11-19)
`ruff check` ✅ · `ruff format --check` ✅ · `mypy --strict` ✅ (38 files) · `pytest` ✅ **76 passed** (59 unit / 17 integration on real PostgreSQL 16) · coverage 97 %.

## Feature-completion report (REQUIREMENTS.md §21)
### Fully implemented and tested
- Typed settings for every spec env var; production guards (secrets present, https public URLs, no mock providers).
- Fernet encryption-at-rest with key rotation; purpose-bound signed tokens (unsubscribe/OAuth-state) incl. expiry + alg-none rejection.
- Injectable clock (`Clock`/`FakeClock`).
- Relational schema (34 tables) + Alembic migration verified: upgrade → `alembic check` (no drift) → downgrade → upgrade. DB-enforced rules: guessed email can never be `verified`; unique contact email per workspace; one message per (campaign, contact, step); idempotency keys; score 0–100; enum CHECKs match Python enums; **RLS enabled on every table**; **append-only audit log** (trigger); full-text search on companies/contacts; message history survives lead deletion.
- FastAPI shell: request-ID + security-headers middleware, uniform error envelope (no value echo, no stack leaks), CORS locked to `FRONTEND_URL`, `/healthz`, `/readyz` (DB + Redis), docs hidden in production.
### Implemented but awaiting credentials
_none yet_
### Dependent on external approval
- Gmail restricted scope (`gmail.readonly`/`modify`): owner-only "In production (unverified)" mode is enough for personal use (ADR-009/012).
### Not yet implemented
Everything in phases 2–8, plus the Next.js frontend scaffold, CodeQL workflow, Dockerfile/render.yaml.

## Next up (in order)
1. Next.js 16 scaffold (Tailwind 4, shadcn/ui, TanStack Query, Recharts, Vitest, Playwright) + CI job + OpenAPI→TS type generation.
2. Push to GitHub (needs owner's GitHub token) and prove CI green.
3. Phase 2: Supabase JWT auth + owner allow-list, provider framework (registry, manager, retry engine, usage tracking) with mock providers, Groq adapter, ARQ worker skeleton + queue abstraction, Gmail OAuth + token store.

## Open questions for the owner (ask only when blocking)
1. Always-on hosting budget (Render paid instance) — ADR-008.
2. Which Gmail address sends NEXORA mails vs Job Hunter mails? Custom domain mailbox for NEXORA?
3. Confirm NEXORA postal address in footer (Amritsar address from the public site) and whether "paying client" may be mentioned in outreach.
4. Google Drive links per role resume (promised "later").
5. Is the owner currently a student or graduated (resume says Apr 2026)? Affects job-mail wording.

## Credentials received
See `.local/SECRETS.local.md` (never listed here). Pending: GitHub, Supabase, Render, Vercel (+bypass secret), Groq, Google OAuth, Redis.
