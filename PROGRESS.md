# PROGRESS — live status & feature-completion report

_Last updated: 2026-11-20. Update this file after every meaningful change._

Legend: ✅ done & tested · 🟡 implemented, awaiting credentials · 🔒 depends on external approval · ⬜ not started · 🚧 in progress

## Phase status
| # | Phase | Status | Notes |
|---|---|---|---|
| 1 | Architecture & scaffold | ✅ | Backend, 34-table schema, API shell, Next.js shell, contract pipeline, E2E. CI green on GitHub. |
| 1b | Security baseline | ✅ | Secret scanner + hooks, nonce CSP, HTTPS/TLS enforcement, rate limiting/DoS controls, supply-chain pinning. `docs/SECURITY.md`. |
| 1c | **No Redis** (ADR-024) | ✅ | Redis/ARQ removed everywhere; queue will be a Postgres table; rate limiter in-process. |
| 1d | Public-readiness tooling (ADR-027) | ✅ | `scripts/prepublic_audit.py` (+ CI step), sanitised docs, personal data moved to `.local/`. |
| 2a | **Postgres job queue + worker** (ADR-024/030) | ✅ | `app/queue/`: SKIP LOCKED claims, leases + heartbeats, fenced writes, backoff, park/release, dead letters, dedupe/outbox, cooperative cancel, periodic jobs, graceful shutdown, reaper, retention. 83 new tests on real Postgres. Not yet started by the app (wired in with the first real task handlers). |
| 2b | **ProviderManager + Groq adapter** (ADR-031) | ✅ | Capability routing, atomic quota reservation, failover, cooldown/circuit with single probe, honest provider-reported credits, per-minute pacing, no-evasion send rules, privacy gate. Groq (`groq-smart`/`groq-fast`) **verified live** against the real API. Not yet started by the app (wired with the first task handlers). |
| 2c | **Authentication** (ADR-032/033) | ✅ | Backend: ES256/RS256 token verification pinned to the project's keys, owner allow-list, optional MFA gate, fail-closed. Dashboard: login page, `HttpOnly` `__Host-` session cookie, same-origin API proxy (the browser holds no token), CSRF/Origin checks, sign-out that revokes the session. 43 real-browser tests against a fake Supabase Auth, **plus a live check against the real Supabase project (2026-11-19)**: owner login created (`scripts/setup_owner_login.py`), token lifetime set to 15 min, password min length 12, sign-ups still off; a real session was accepted by the backend verifier, the project's anon/service keys and a tampered signature were refused, and a full real-browser login/sign-out passed. |
| 2d | **Gmail connection** (ADR-034) | ✅ built · ✅ **verified live 2026-11-20** (NEXORA mailbox connected, read-only test reached the real Gmail; Job Hunter mailbox still to connect) | OAuth with PKCE + session-bound encrypted state, encrypted token storage, automatic refresh, reconnect-needed handling, read-only test, disconnect+revoke; Settings → Integrations page. 77 backend + 32 frontend + browser tests against a fake Google. Google app is published and branding verified; the owner still has to click Connect once and approve (the "unverified app" screen is expected). |
| 2e | **Gmail send path** (ADR-035) | ✅ built & tested · ✅ **verified live**: a real test email was sent and a lost-response replay was resolved by the reconciler. The live run found a real design bug (Gmail replaces the Message-ID) — fixed, see the ADR-035 correction | Safe MIME builder (header-injection proof), Gmail REST client that separates "never sent" from "maybe sent", exactly-once `MailboxSender` (row-locked claim with mailbox limits inside it, fenced writes, post-claim gate, `SEND_UNCERTAIN`, send-as verification) and a reconciler that resolves unknown outcomes from the mailbox's Sent folder. 62 integration + 87 unit tests on real Postgres with a fake Gmail that remembers every message; 6 mutation checks on the key guards. Queue handlers + compliance gate arrive with Phase 4. |
| 2f | **Inbox sync** (ADR-036) | ✅ built & tested · ✅ **verified live 2026-11-20**: the cursor was set at connection, four unrelated private messages were ignored unread, and the owner's reply to the test email was found in our Gmail thread, recorded as a human reply linked to the message we sent (quoted text stripped) | History-API polling with a cursor, a scope guard that never reads private mail (allow-listed headers only, body only for conversations we started), deterministic bounce / auto-reply detection with DSN parsing, spoof-resistant hard-bounce suppression, lost-history recovery, idempotent storage. 30 integration + 58 unit tests; 7 mutation checks. Classification, follow-up stopping and notifications come with reply processing. |
| 2g | **Background runtime** (ADR-037) | ✅ built & tested | The queue worker + a per-workspace scheduler now start with the app: replies are read and unknown sends settled automatically (`inbox.poll`, `outreach.reconcile`). The background worker owns a deny-all send gate, so it structurally cannot send until the compliance gate exists. 14 integration tests incl. a real worker picking up a reply; 4 mutation checks. |
| 3a | **SSRF-guarded fetcher** (ADR-038) | ✅ built & tested | The only sanctioned way to fetch a third-party page: address-based checks, pinned connections, re-validated redirects, robots.txt, throttling, size/time caps. 211 tests, 13 mutation checks. Foundation for the website crawler. |
| 3b | **Discovery provider adapters** (ADR-039) | ✅ built & verified live | Geoapify (businesses), Hunter (contacts), Verifalia + MailboxValidator (verification), Tavily + Brave (search) behind vendor-neutral capabilities; written from the **observed** responses of the real services, 6 live tests pass, 91 unit tests, 10 mutation checks. Found on the way: Brave reports an invalid key as HTTP 422. ZeroBounce (0 credits) and Abstract (key rejected) are waiting on the owner. |
| 3c | **Discovery core** (ADR-040) | ✅ built & run live | Canonical identity/dedupe rules, published-only contact extraction (mailto, text, Cloudflare-protected, JSON-LD) with provenance, free MX/role/disposable checks, and a live demo (`backend/scripts/live_discovery_demo.py`). First real run: 6 businesses → 1 published address (many small businesses publish a form only) — so website discovery by search + Hunter are next. 154 tests, 12 mutation checks. |
| 3 | Lead discovery (Overture/OSM/Geoapify + SSRF-guarded crawler, extraction, verification, qualification) | ⬜ | Sources in `docs/PROVIDERS.md`. |
| 4 | Outreach (templates, generation, campaigns, sender adapters, approvals, adaptive ramp) | ⬜ | Sending architecture: ADR-025/026. |
| 5 | Reply intelligence (sync, classify, notify, follow-ups) | ⬜ | |
| 6 | Dashboard (all pages + System Operations) | ⬜ | Shell + System status page exist. |
| 7 | Testing & hardening | ⬜ | |
| 8 | Deployment (Supabase, Render, Vercel) + docs | ⬜ | Hosting terms open (ADR-023). |

## Quality gate (local, 2026-11-19)
Backend: `ruff` ✅ · `ruff format` ✅ · `mypy --strict` ✅ (149 files) · `pytest` ✅ **1,184 passed** (+4 opt-in live tests against the real Groq API, run on demand) (unit + integration on real PostgreSQL 16) · OpenAPI export current ✅.
Frontend: `eslint` (incl. XSS-sink bans) ✅ · `tsc` ✅ · `prettier` ✅ · `vitest` ✅ **179 passed** · `next build` ✅ · **Playwright 52/52 against the real API + Postgres + a fake Supabase Auth** incl. login/logout, cookie flags, no token in JS/storage/HTML/responses, forged-session rejection, session refresh, CSRF checks, axe WCAG A/AA, nonce CSP, blocked injection/exfiltration/clickjacking.
Security/public-readiness: `scan_secrets.py` clean (tree, history, browser bundle) ✅ · `prepublic_audit.py` clean on tree + workflows ✅ · **git history fails the audit by design** (ADR-027) ⇒ publish via a clean-slate repo.

## Feature-completion report (REQUIREMENTS.md §21)
### Fully implemented and tested
- Typed settings for every spec env var; production guards (secrets, public https URLs, TLS-enforced Postgres, no mock providers).
- Fernet encryption-at-rest with key rotation; purpose-bound signed tokens; injectable clock.
- Relational schema (34 tables) + verified migration with DB-enforced safety rules (guessed email never verified; one message per campaign/contact/step; idempotency keys; score 0–100; RLS on every table; append-only audit log).
- FastAPI shell: request-ID, security headers + API CSP, HSTS (prod), HTTP→HTTPS redirect, trusted hosts, in-process rate limiting (bounded memory, spoof-proof client IP), concurrency cap, body-size limit, DB timeouts, uniform error envelope, `/healthz`, `/readyz` (database).
- Durable Postgres job queue + in-process worker (no Redis): exactly-one claimer under concurrency, crash recovery, zombie-write fencing, retry/backoff with `Retry-After`, parking without burning attempts, dead letters with retry/discard, dedupe keys, graceful shutdown — all proven against a real database (ADR-030).
- ProviderManager (ADR-031): capability routing with free-first / priority / headroom ordering; atomic quota reservation (30-way race on a quota of 10 never overspends); failover on quota / 429 / 5xx / 401 / 403 with the correct per-error behaviour; cooldown + circuit with a single recovery probe; provider-reported credits only (never invented); per-minute pacing; sending limits and policy refusals are never routed around; non-idempotent calls never repeated blindly; privacy gate for personal data; settings-page overview data. Groq adapter (two per-model providers on one key) verified live.
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
2. Phase 2: ~~Postgres job queue~~ ✅ → ~~ProviderManager + Groq~~ ✅ → ~~Supabase auth with BFF cookie sessions~~ ✅ (live-verified) → ~~Gmail OAuth + token store~~ ✅ (needs the owner to connect a mailbox) → ~~Gmail send adapter + exactly-once sender~~ ✅ (ADR-035) ~~Inbox sync~~ ✅ (ADR-036) ~~queue + background tasks wired into the app~~ ✅ (ADR-037) → next: **Phase 3 lead discovery** (provider adapters for the stored keys behind the ProviderManager, SSRF-guarded crawler, verification, dedupe), then the compliance gate + campaign pipeline.
   Then wire queue + manager into app startup together with the first real task handlers (Phase 3), and the Groq-based prompt-injection guard (Prompt Guard 2) when reply classification is built.
3. Public publication (only when the owner provides the new repo + new token — ADR-027).

## Owner action checklist (agreed 2026-11-19)
**Done 2026-11-19:** Supabase project created + migrated; Groq key verified; repo made public (existing repo, ADR-027 amendment). **Still to do by the owner:** revoke the account-level Supabase token (no longer needed); optionally rotate the GitHub token; set the repo security toggles (Settings → Code security: secret scanning + push protection; Actions → Fork PR workflows: require approval for all outside collaborators).
**Long lead time (warm-up takes weeks) — start now:** (4) buy a separate sending domain (not `nexora-ai.co.in`) — **owner will buy later; NEXORA real sending waits for it (Job Hunter can start earlier)**; (5) Google Workspace Starter on it, 2 mailboxes (`divyansh@…`, `founder@…`), use them like a human for ~2 weeks; (6) tell me the domain + DNS host → I generate the exact SPF/DKIM/DMARC records; enable Google Postmaster Tools.
**Needs the owner (found by the live key check, 2026-11-20):** ZeroBounce shows **0 credits** (activate the free monthly credits) and the Abstract key is **rejected** (it must be the *Email Validation* API key; each Abstract API has its own).
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
