# PROGRESS — live status & feature-completion report

_Last updated: 2026-11-19. Update this file after every meaningful change._

Legend: ✅ done & tested · 🟡 implemented, awaiting credentials · 🔒 depends on external approval · ⬜ not started · 🚧 in progress

## Phase status
| # | Phase | Status | Notes |
|---|---|---|---|
| 1 | Architecture & scaffold | ✅ | Backend, 34-table schema, API shell, Next.js shell, contract pipeline, E2E. **Pushed to GitHub; first CI run found and fixed real issues** (see below). |
| 1b | Security baseline | ✅ | Secret scanner + hooks, nonce CSP, HTTPS/TLS enforcement, rate limiting/DoS controls, supply-chain pinning. `docs/SECURITY.md`. |
| 2 | Auth + integrations (BFF cookie sessions, Supabase JWT, provider framework, Groq, Gmail OAuth, ARQ) | ⬜ **next** | Provider failover framework needs no credentials; Supabase keys needed for auth. |
| 3 | Lead discovery (crawler + SSRF guard, extraction, verification, qualification) | ⬜ | |
| 4 | Outreach (templates, generation, campaigns, Gmail send, approvals, limits) | ⬜ | |
| 5 | Reply intelligence (sync, classify, notify, follow-ups) | ⬜ | |
| 6 | Dashboard (all pages + System Operations) | ⬜ | Shell + System status page exist. |
| 7 | Testing & hardening | ⬜ | |
| 8 | Deployment (Supabase, Render free + UptimeRobot, Vercel) + docs | ⬜ | Hosting decided (ADR-008). |

## Quality gate (local, 2026-11-19)
Backend: `ruff` ✅ · `ruff format` ✅ · `mypy --strict` ✅ (46 files) · `pytest` ✅ **188 passed** (unit + integration on real PostgreSQL 16) · OpenAPI export current ✅ · `pip-audit` (hash-verified) ✅ no known vulnerabilities.
Frontend: `eslint` (incl. XSS-sink bans) ✅ · `tsc` ✅ · `prettier` ✅ · `vitest` ✅ **35 passed** · `next build` ✅ (all routes dynamic) · `npm audit` production tree ✅ **0 vulnerabilities** · **Playwright 22/22 against the real stack** (API + Postgres + Redis): axe WCAG 2.1 A/AA light+dark, mobile, nonce CSP with zero violations, injected inline script / remote script / event handler / `javascript:` link **blocked**, cross-origin exfiltration **blocked**, clickjacking **blocked**.
Secrets: `scan_secrets.py` ✅ clean on tree, full git history and the built browser bundle.

## First GitHub CI run (2026-11-19) — what it found
✅ Frontend, API-contract, backend py3.11 + py3.12 tests (real Postgres + Redis service containers) and E2E (10/10) passed on GitHub.
Fixed after reading the logs: `pip-audit --disable-pip` needs a hashed export (my flag); gitleaks *action* fails on a first push → replaced with a pinned, checksum-verified binary; setup-uv cache-prune post-step crashed in the E2E job → cache disabled there; Dependabot exposed that `arq` caps `redis<6` → capped + `requires-python <3.14`; `pgserver` had never been declared as a dev dependency → declared. All actions now pinned to commit SHAs.
**Corrected CI result (commit `adaf91f`): all 6 jobs green on GitHub** — Backend py3.11, Backend py3.12, Frontend, API contract, E2E (Playwright, real backend), Secret scan (repo scanner + gitleaks over full history). CodeQL is skipped by design while the repo is private. gitleaks' only two findings were deliberate test fixtures (allowlisted by exact fingerprint in `.gitleaksignore`) — it independently confirmed no real secret was ever committed. Dependabot is configured to skip majors, group updates and cap open PRs (CI-minutes budget).

## Feature-completion report (REQUIREMENTS.md §21)
### Fully implemented and tested
- Typed settings for every spec env var; production guards: secrets present, public https URLs, **TLS-enforced Postgres, rediss Redis**, no mock providers.
- Fernet encryption-at-rest with key rotation; purpose-bound signed tokens (unsubscribe/OAuth-state).
- Injectable clock; relational schema (34 tables) + verified migration with DB-enforced safety rules (guessed email never verified; one message per campaign/contact/step; idempotency keys; score 0–100; RLS on every table; append-only audit log).
- FastAPI shell: request-ID, security headers + API CSP, HSTS (prod), HTTP→HTTPS redirect, trusted hosts, rate limiting (Redis + in-memory fallback), spoof-proof client IP, concurrency cap, body-size limit, DB timeouts, uniform error envelope, `/healthz`, `/readyz`.
- Contract-first pipeline (OpenAPI → generated TS types → typed client) with drift checks.
- Next.js 16 dashboard shell: responsive sidebar, light/dark themes, System status page (live backend health), custom 404 + error pages, strict nonce CSP + hardening headers.
- Secret scanning: pre-commit/pre-push hooks, CI (tree + history + bundle), gitleaks; GitHub helper that never exposes the token.
### Implemented but awaiting credentials
_none yet_
### Dependent on external approval
- Gmail restricted scope (`gmail.readonly`/`modify`): owner-only "In production (unverified)" mode is enough for personal use (ADR-009/012).
### Not yet implemented
Phases 2–8 (auth/BFF sessions, provider failover, Gmail, discovery + SSRF guard, generation, campaigns/controller, reply intelligence, dashboard pages, deployment, Dockerfile/render config, runbooks).

## Next up (in order)
1. Commit + push this security work; confirm all CI jobs green on GitHub.
2. Phase 2: provider framework (registry, manager, retry engine, usage/credit tracking, failover) with mock providers → Groq adapter → queue abstraction + ARQ worker skeleton → Supabase auth with BFF cookie sessions (needs Supabase keys) → Gmail OAuth + token store (needs Google OAuth client).

## Open questions for the owner (ask only when blocking)
1. Which mode does `divyanshkhanna1729@gmail.com` send — NEXORA, Job Hunter, or both? (My schema ties one mailbox to one mode; tell me if you want one Gmail for both and I'll adjust.)
2. Confirm NEXORA postal address in footer (Amritsar address from the public site) and whether "paying client" may be mentioned in outreach.
3. Google Drive links per role resume (promised "later").
4. Is the owner currently a student or graduated (resume says Apr 2026)? Affects job-mail wording.

## Credentials received
GitHub token ✅ (in `.local/`, never listed here). Pending: Supabase, Render, Vercel (+bypass secret), Groq, Google OAuth, Redis. Sender Gmail address received.
