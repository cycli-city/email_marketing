# ARCHITECTURE — AI Outreach Intelligence

Status: v1 plan (2026-11-19). Implements `REQUIREMENTS.md` + `REQUIREMENTS_AUTONOMY.md`. Deviations are logged in `DECISIONS.md`.

## 1. System context

```
                     ┌───────────────────────── Vercel ─────────────────────────┐
  Owner (browser) ──▶│ Next.js 16 dashboard (App Router, TS strict, shadcn/ui)  │
                     └───────────┬───────────────────────────────────────────────┘
                                 │ HTTPS (BFF cookie session → API; ADR-014)
                     ┌───────────▼─────────── Render (one always-on service) ────┐
   Gmail Pub/Sub ───▶│ FastAPI API   /webhooks/gmail  /u/{token} (public)         │
   (push)            │                                                            │
                     │ In-process workers (asyncio; APP_ROLE=all)                 │
                     │   • controller tick (30 s)   • stage workers               │
                     │   • mailbox sync             • reconcilers · janitor       │
                     └───────────────────────┬────────────────────────────────────┘
                                             │ SQLAlchemy 2 (asyncpg, TLS)
                                     ┌───────▼────────────────────────┐
                                     │ Supabase PostgreSQL            │
                                     │ source of truth + job_queue    │
                                     │ (FOR UPDATE SKIP LOCKED), Auth,│
                                     │ RLS, FTS                       │
                                     └────────────────────────────────┘
     Providers (via ProviderManager): LLM · search · discovery · verification · email send/receive · notify
     No Redis anywhere (ADR-024): queue = Postgres table, rate limiter = in-process.
```

Single backend image, three roles selected by `APP_ROLE`: `api`, `worker`, `all` (api + worker in one process group — cheapest always-on deployment). The frontend is never required for the system to operate.

## 2. Design principles
1. **Postgres is the source of truth for work — and the queue itself (ADR-024).** Every unit of work is a row with a status; the queue is a `job_queue` table claimed with `FOR UPDATE SKIP LOCKED`. If the worker restarts, expired locks are re-queued and the controller re-derives what to do from the DB. (This is what makes "recover unfinished jobs without duplicating sent emails" true.)
2. **Persisted state machines**, not in-memory flows: `Campaign`, `DiscoveryJob`, `Lead/Contact`, `EmailMessage`, `FollowUpSchedule`.
3. **Capability-based provider layer.** Business logic asks for a capability ("verify email"), never for "Hunter".
4. **Deterministic code decides and acts; LLMs only propose** structured data (validated by Pydantic). An LLM can never trigger a send, a suppression change or a notification by itself.
5. **Safety gates are pure functions** (compliance, send-limits, stop-rules) with exhaustive tests.
6. **Everything is auditable**: audit_log + activity feed (the Ops page reads the same events).

## 3. Autonomous pipeline & controller

### 3.1 Stages (per lead/contact; each stage is a worker task that is idempotent)
```
DISCOVERED ─▶ RESEARCHED ─▶ CONTACTS_EXTRACTED ─▶ VERIFIED ─▶ QUALIFIED ─▶ DRAFTED ─▶ (APPROVED) ─▶ QUEUED ─▶ SENT ─▶ [REPLIED | BOUNCED | UNSUBSCRIBED | FOLLOWUP_n | EXHAUSTED]
      └──────── DISQUALIFIED / SUPPRESSED / DUPLICATE / NEEDS_REVIEW / WAITING_PROVIDER (parked, never lost) ────────┘
```
Discovery and drafting **continue while sending is paused**; only `QUEUED → SENT` is gated by eligibility.

### 3.2 Campaign Controller (`orchestrator/controller.py`)
An in-process periodic task (`controller_tick`, every 30 s, guarded by a Postgres advisory lock so only one tick runs at a time). For each `RUNNING` campaign it:
1. Checks safety preconditions → auto-pauses with reason (sender auth failed, bounce rate over threshold, compliance block, kill switch).
2. Computes **pipeline deficit**: `target_qualified − (qualified + drafted + queued + sent-in-flight)` → if > 0, creates/continues a `discovery_job` with a budget slice (replenishment).
3. For each stage with pending rows and free capacity, enqueues stage jobs in batches using **deterministic job ids** (`stage:{lead_id}:{stage}:{attempt_epoch}`) — re-enqueue is a no-op.
4. Applies the sending window (time-zone aware schedule, business days) and per-account hourly/daily limits → releases `QUEUED` messages to the sender.
5. Schedules follow-ups whose due time has arrived (after re-checking stop-rules).
6. Stops the campaign when target/budget/stop-condition reached; records completion event.
Rows parked as `WAITING_PROVIDER` carry `retry_after`; the controller re-releases them when `ProviderManager.next_available_at(capability) <= now`.

### 3.3 Crash recovery
- Stage rows have `status`, `claimed_by`, `claimed_at`, `attempt`. A **janitor cron** returns rows stuck in `*_IN_PROGRESS` past a lease timeout to the previous state (except sends — see §5).
- Queue rows carry `attempts`/`max_attempts`, `locked_until` leases and `run_at` for backoff; graceful shutdown finishes the current job and releases the rest; cancellation sets a flag the handler checks. Exact semantics (fencing, what consumes an attempt, parking, dead letters, dedupe/outbox): **ADR-030**, code in `backend/app/queue/`.
- Dead-letter: after max attempts a task writes a `dead_letters` row (task, payload hash, error class, last error, attempts) and surfaces on the Ops page with a *retry* / *discard* action.

## 4. Provider layer (`app/providers`)

### 4.1 Capabilities (Protocols)
`LLMProvider` · `SearchProvider` · `CompanyDiscoveryProvider` · `ContactDiscoveryProvider` · `EmailVerificationProvider` · `WebsiteResearchProvider` · `EmailSender` · `InboxReader` · `Notifier`. Spec's `LeadDiscoveryProvider` = Company + Contact discovery + public-info retrieval returning normalised `LeadRecord`s.

Each adapter declares a static `ProviderSpec`: `name`, `capability`, `requires_credentials`, `free_tier` (documented quota, reset rule, source URL, retrieved-at), `rate_limit`, `cost_per_call`, `terms_notes`, `supports_usage_api`, `default_priority`. Adapters normalise responses to internal Pydantic schemas (`CompanyRecord`, `ContactRecord`, `VerificationResult`, `LLMResult`, `SendResult`, …).

### 4.2 ProviderManager
Persistent state per provider (`provider_states` table; hot counters in process memory, flushed to the table): credentials status, connection status, plan, **known remaining credits (with `accuracy`: `provider_reported | internal_counter | unknown`)**, daily/monthly usage, rate-limit window, last success/failure, failure count, cooldown_until, reset_at, estimated cost, priority, enabled flag, `paid_fallback_allowed`. Never invent credits: if the provider exposes no usage endpoint the UI shows "internal counter (estimate)" or "unknown".

**Selection** for a capability call:
```
candidates = enabled ∧ credentials_ok ∧ not needs_attention ∧ not in cooldown
           ∧ quota_available(now) ∧ within_budget ∧ allowed_by_task_policy
order      = (is_free desc, priority asc, health_score desc, cost asc)   # free-first, cost-aware
```
Paid providers are only candidates if `paid_fallback_allowed` and the workspace budget has room.

**Execution** (`ProviderManager.call(capability, request, task_policy)`): picks the first candidate, runs it through the **RetryEngine**, records a `provider_usage_events` row (units, outcome, latency), updates state, and on failure classifies and falls through:

| Outcome | Action |
|---|---|
| network error / timeout / 5xx | retry same provider with exponential backoff + full jitter (bounded attempts/window); then cooldown + next provider |
| 429 | honour `Retry-After` (else backoff+jitter); set `rate_limited_until`; do not hit the provider again meanwhile; try next provider |
| 401 | mark `needs_attention` (auth), stop using it until the owner re-saves credentials; notify; next provider |
| 403 | classify: quota exhausted → mark exhausted until `reset_at`; permission/account/policy → `needs_attention`; fall through only if the task is permitted via another provider |
| invalid/malformed response | schema-validate; log; **no retry of permanent failures**; try next provider once |
| all candidates unavailable | raise `NoProviderAvailable(retry_after)` → the *task* is parked `WAITING_PROVIDER` (data preserved), nothing else pauses; controller resumes it |

Circuit breaker per provider (closed → open → half-open probe). Provider health monitor cron pings cheap health endpoints (never spending paid credits) and refreshes usage from provider usage APIs where they exist.

**Send-path exception (anti-evasion):** for `EmailSender` the limit-exhaustion path is **not** a failover trigger. Gmail daily/hourly caps → the message waits for the next window. Failover to Brevo/Resend happens only for *technical* outages/auth errors, only to a sender explicitly authorised on that campaign (same brand identity, domain auth verified), and never to bypass quota or spam blocks. No account/key rotation anywhere.

### 4.3 Dedupe across providers
Companies keyed by normalised registrable domain (+ name/location fuzzy for site-less); contacts by normalised email (lowercase, plus-tag/gmail-dot rules *only for matching*, not for sending) and `(company_id, normalised_name)`. Upserts merge provenance (`lead_sources` rows keep every provider + URL + timestamp) so switching providers never loses data and never duplicates.

## 5. Exactly-once email sending (`services/outreach/sender.py`)
1. Draft approved → `email_messages` row `status=QUEUED`, unique `(campaign_id, contact_id, step_no)` and `idempotency_key`.
2. **Claim** atomically: `UPDATE … SET status='SENDING', claimed_at=now(), claim_token=:t WHERE id=:id AND status='QUEUED' RETURNING *` — zero rows ⇒ someone else has it.
3. Re-run the **compliance gate + stop-rules + limits** *after* claiming (state may have changed since queueing).
4. Build MIME with our own `Message-ID` (`<{uuid}@{sender-domain}>`) and header `X-AOI-Message: {uuid}`; add `List-Unsubscribe` (+ `List-Unsubscribe-Post: List-Unsubscribe=One-Click`), sender identification, postal address footer where required.
5. Call Gmail `users.messages.send` (threading: `threadId` + `In-Reply-To`/`References` for follow-ups).
6. On **success** → `SENT`, store `gmail_message_id`, `thread_id`, timestamps. On **definite failure** (4xx validation) → `FAILED` with reason. On **ambiguous failure** (timeout/connection reset/5xx after the request may have been accepted) → `SEND_UNCERTAIN`, **never retried blindly**.
7. A **reconciler** resolves `SEND_UNCERTAIN` (and any `SENDING` past lease) by searching the mailbox's Sent folder for mail to the same recipient since shortly before the claim and keeping the messages that carry **our own `X-AOI-Message` token** (found ⇒ mark `SENT`, recording the Message-ID Gmail assigned and Gmail's own timestamp; not found after a safe settle window ⇒ back to `QUEUED`, at most one more attempt). *Gmail replaces the `Message-ID` header of every message sent through its API (observed on a real mailbox, ADR-035 correction), so a search for the id we generated can never succeed — the custom header is what survives.*
Tests simulate crash after claim, crash after provider accept, and timeouts, asserting exactly one send.

## 6. Reply intelligence
- **Inbound:** Gmail `users.watch` → Pub/Sub push → `/webhooks/gmail/pubsub` (OIDC-verified, dedup on `messageId`) → enqueue `gmail_sync(account)`. **Fallback:** cron `history.list` from stored `historyId` every 60–120 s; watch renewal cron (watch expires ≤7 d) and recovery on expired/invalid historyId (full resync of recent threads).
- **Matching:** `threadId` → `email_threads` → campaign/contact; fallback `In-Reply-To`/`References` → our Message-ID; fallback sender+subject heuristics (flagged low-confidence). Processed-set keyed by Gmail message id (unique) ⇒ no double processing.
- **Pre-classification (deterministic, before any LLM):** DSN/bounce parsing (`multipart/report`, `Auto-Submitted`, `X-Failed-Recipients`, 5.x.x codes ⇒ hard/soft bounce), auto-reply headers (`Auto-Submitted: auto-replied`, `X-Autoreply`, `Precedence: auto_reply`), unsubscribe keyword intent. These short-circuit to BOUNCE / OUT_OF_OFFICE / UNSUBSCRIBE without trusting the model.
- **LLM classification:** sanitised, length-capped text (quoted history stripped, HTML → text) placed in a data block; JSON schema output `{category, confidence, explanation, extracted:{…}, suggested_action, needs_human}` validated by Pydantic; one repair retry; low confidence or schema failure ⇒ `NEEDS_REVIEW`. Polite-language-only ⇒ cannot be INTERESTED (explicit rule + eval cases).
- **Actions (deterministic table):** category → lead status, stop follow-ups (any human reply, unsubscribe, hard bounce, negative), suppression entries (unsubscribe, hard bounce), notification (only INTERESTED/HIGH_INTENT/JOB_OPPORTUNITY/REFERRAL/actionable UNSUBSCRIBE/errors). No auto-reply to prospects, ever, without approval; drafts may be *suggested*.

## 7. Compliance gate (`domain/compliance.py`)
Pure `evaluate(contact, campaign, jurisdiction_policy, suppression, now) -> Decision(allowed, reasons[])`. Checks: suppression/unsubscribe/bounce; email status must be `verified` (or policy-allowed `unverified` with source evidence — **never `guessed`**); role-based vs named personal address; lawful-basis metadata + source URL present; jurisdiction policy (e.g. Germany B2B cold marketing mail defaults to *requires consent / blocked*; UK PECR corporate vs individual subscriber; CASL conspicuous-publication implied consent + relevance; CAN-SPAM identification + postal address + opt-out; India DPDP/commercial-comm notes); sender identity + postal address configured; unsubscribe mechanism present; duplicates (already contacted in any campaign within cooldown). Policies are data (table + defaults), editable, versioned, and documented as **not legal advice**. Job-application emails (non-marketing) use a lighter policy but still honour opt-outs.

## 8. LLM safety & grounding
- Untrusted text is wrapped in explicit delimiters and system prompts state it is data; no tools exposed to the model; outputs strictly schema-validated.
- **Grounding:** research produces a `facts` list `{id, text, source_url, quote}` extracted *by code + LLM with quote verification (quote must appear verbatim in the fetched page)*. The email generator may only reference fact ids; a verifier rejects drafts containing numbers, names, URLs or claims absent from facts/profile/product-facts; failures regenerate once, then `NEEDS_REVIEW`.
- Prompt files are versioned (`prompts/{name}/v{N}.md` + `schema.json`), the version is stored with every generation for audit/eval.
- Offline **eval harness** (golden sets: reply classification, grounding violations, injection attempts) runs in CI against the mock LLM and optionally live.

## 9. Data model (groups; full DDL in Alembic migrations)
- **Tenancy/auth:** `workspaces`, `users`, `workspace_members`.
- **Accounts/integrations:** `connected_email_accounts` (+ `oauth_tokens` Fernet-encrypted), `integration_settings` (encrypted creds), `provider_states`, `provider_usage_events`, `resume_assets`, `sender_profiles`.
- **Campaigns:** `campaigns` (mode `NEXORA|JOB_HUNTER`, status, target, authorisation, kill switch), `campaign_settings` (schedule, tz, limits, follow-up cfg, approval mode), `email_templates` (+ versions).
- **Leads:** `companies`, `contacts`, `leads` (campaign membership + score + explanation + evidence), `lead_sources`, `email_verifications`, `facts`.
- **Discovery:** `discovery_jobs`, `discovery_job_results`.
- **Messaging:** `email_messages` (state machine), `email_threads`, `incoming_replies`, `reply_classifications`, `follow_up_schedules`.
- **Safety:** `suppression_list`, `jurisdiction_policies`, `consent_records`.
- **Ops:** `activity_events` (Ops feed), `audit_logs`, `dead_letters`, `notification_preferences`, `notification_logs`, `watch_states` (Gmail historyId/watch expiry).
Conventions: UUID PKs, `workspace_id` on every tenant table (+ composite indexes), `created_at/updated_at`, enums as native PG enums or checked text, FTS `tsvector` generated columns + GIN on leads/companies, pgvector **not used in v1** (no real semantic-search need; revisit for reply/KB similarity).
RLS: enabled deny-all on every table; the backend authorises in code (workspace-scoped repository layer + tests that a user from workspace B can never read workspace A).

## 10. Security
Supabase Auth issues JWTs; backend verifies signature/`aud`/`exp` and maps `sub` → user/workspace. Fernet (AES-128-CBC+HMAC; key rotation via `MultiFernet`) for tokens/keys at rest; secrets never serialised to API responses (schemas exclude them, tests assert). Rate limiting (in-process sliding window, ADR-024) on API + webhook endpoints; CORS locked to `FRONTEND_URL`; security headers + CSP on the frontend; signed (HMAC/JWT) short-lived tokens for unsubscribe/OAuth state; Pub/Sub push verified by OIDC token; HTML emails sanitised (nh3/bleach) before render; audit log for every sensitive action; generic error bodies with request ids.

## 11. Repo layout
```
email_marketing/
├─ CLAUDE.md  REQUIREMENTS*.md  CONTEXT.md  PROGRESS.md  README.md
├─ backend/  pyproject.toml  alembic.ini  migrations/  app/{core,db,domain,schemas,api,services,providers,orchestrator,workers,research,prompts}  tests/{unit,integration,e2e}
├─ frontend/ (Next.js 16)  src/{app,components,lib,hooks}  tests/
├─ infra/    Dockerfile(s), render.yaml, docker-compose.yml (for people who have Docker)
├─ docs/     ARCHITECTURE  DECISIONS  PROVIDERS  DEPLOYMENT  GOOGLE_OAUTH  COMPLIANCE  RUNBOOK  profile/
├─ scripts/  dev helpers, openapi export, secret-scan
└─ .github/workflows/  ci.yml  codeql.yml
```

## 12. Testing strategy
- **Unit (fast, no I/O):** domain rules, state machines, scoring, compliance, retry/backoff math, provider selection (fake clock), MIME building, DSN parsing, extractors (hypothesis property tests), grounding verifier.
- **Integration (real Postgres):** repositories, migrations up/down, queue workers end-to-end with mock providers (incl. concurrent-claim and crash-recovery tests), controller tick, exactly-once send under injected crashes, reply pipeline with fixture mailboxes, tenant isolation. CI uses a Postgres service container; locally a pip-installed Postgres (see DECISIONS ADR-006).
- **Adapter contract tests:** `respx`-mocked HTTP per provider with recorded sample payloads (from official docs); a shared contract suite every adapter of a capability must pass. Gmail via googleapiclient `HttpMock`.
- **API tests:** httpx `ASGITransport`; auth, validation, authorisation, rate limits.
- **Frontend:** Vitest + Testing Library (components/hooks), Playwright E2E against the real backend running mock providers; axe-core a11y checks.
- **Gates in CI:** ruff, mypy --strict, pytest (+coverage floor), alembic upgrade/check, OpenAPI drift, ESLint, tsc, vitest, Playwright, build, gitleaks, pip-audit/npm audit, CodeQL.

## 13. Deployment topology
Vercel (frontend) · Render (backend: one always-on service `APP_ROLE=all`, or split `api` + `worker`; **free instance kept awake by UptimeRobot pinging `/readyz` — owner decision, ADR-008; SMTP ports are blocked there, so mail uses HTTPS APIs — ADR-026**) · Supabase (Postgres/Auth). Config via env vars; migrations run as a pre-deploy command; health endpoints `/healthz` (liveness) and `/readyz` (database).

## 14. Phase plan
| Phase | Deliverable | Exit criteria |
|---|---|---|
| 1 Architecture | scaffold, config, DB models + initial migration, CI, `.env.example` | CI green, migration applies on clean PG |
| 2 Auth + integrations | Supabase JWT auth, Provider framework (registry/manager/retry/usage), Groq + mock LLM, Gmail OAuth, Postgres job queue + in-process workers, provider research doc | adapters pass contract tests |
| 3 Discovery | discovery jobs, website crawler (robots), extractors, verification (syntax/MX/provider), AI qualification/scoring | e2e discovery with mocks; dedupe tests |
| 4 Outreach | templates, generator + grounding verifier, campaigns, approval workflows, Gmail sender, limits/warm-up | exactly-once send tests |
| 5 Reply intelligence | Gmail watch/history sync, bounce/OOO/unsub detection, LLM classifier, notifications, follow-ups | stop-rule tests |
| 6 Dashboard | all pages + Ops page wired to real APIs | Playwright E2E green |
| 7 Hardening | failure-injection, load/rate tests, security review, evals | all gates green |
| 8 Deployment | Supabase migrations, Render + Vercel live, docs, runbook | live smoke tests |
