# Owner profile — distilled from the general resume (uploaded 2026-11-19)

> **Purpose:** the *only* source of first-person facts the Job Hunter email generator may use.
> If a fact is not here, the generator must not claim it. Each fact has an ID so generated emails can be
> grounding-checked (`fact_ids` used must exist). Contact details (phone/email) are intentionally omitted — they come from the connected sender account / settings.
> Role-specific resumes live on Google Drive (links pending) — see `CONTEXT.md` §2.

**Name:** Divyansh Khanna · **Location:** Amritsar, India (open to relocate) · **Headline:** Software Engineer — Full-Stack · AI Systems · Automation | Python / TypeScript
**GitHub:** github.com/cycli-city · **LinkedIn:** linkedin.com/in/divyansh-khanna
**Education:** B.Tech Information Technology, VIT Vellore, CGPA 7.76, Sept 2022 – Apr 2026. Certs: CEH (EC-Council, 2026), ML & DS Fundamentals. Leadership: Core Committee, Entrepreneurship Cell VIT; state-level champion in Badminton and Table Tennis.

## Projects (fact IDs)
- **P-NEXORA — NEXORA AI v2** (live; Meta-approved; paying client; nexora-ai.co.in; private repo). Stack: FastAPI, Next.js 14, TypeScript, Supabase (pgvector, RLS), Redis + ARQ, Groq LLaMA, Razorpay, Docker.
  - Full stack end-to-end: FastAPI backend with pgvector RAG and ARQ priority queues; Next.js dashboard with CRM, billing, channel-connection flows, Google OAuth / TOTP 2FA; Docker + GitHub Actions + Sentry in production.
  - AI pipeline: inbound webhook → HMAC verify → pgvector retrieval over the client's knowledge base → Groq LLaMA reply → outbound. Production controls: idempotent webhooks, exactly-once billing, replay-safe broadcasts, RLS tenant isolation.
  - Meta App Review approved (14 permissions); onboarded first paying client; Fernet AES-256 token encryption; Razorpay subscription billing.
- **P-WINDSCOPE — WindScope UK** (live; windscope-uk-sand.vercel.app; GitHub). React, TypeScript, TanStack Query, Recharts, Tailwind, FastAPI, pandas, Docker, Vitest, Playwright. Interactive timeline with zoom/drill-down/URL-persisted state; FastAPI with ETag/304 caching; contract-first OpenAPI with CI-generated TS types that fail on drift. 98 Vitest tests, 48 Playwright tests, 102 live acceptance checks; strict CSP enforced by tests; zero WCAG A/AA violations; mypy --strict + CodeQL in CI.
- **P-LEADAUTO — AI Lead Automation** (production n8n pipeline; GitHub). n8n, VAPI voice agent, Groq/Llama, Airtable CRM, Gmail, Twilio, webhooks. Form → Airtable CRM → Gmail → AI voice-agent call within 60 seconds; 6+ webhook integrations with branching, error handling and retries; live for a real client.
- **P-CHRONOLENS — ChronoLens** (chronolens-ai.netlify.app; GitHub). Python, FastAPI, LangChain, Claude API, Supabase pgvector, ChromaDB, ONNX, React. Dual vector store, metadata-filtered retrieval, ONNX embeddings; offline eval harness F1 = 0.914 vs 0.301 pure-LLM baseline (528 chunks).
- **P-NEXUSHQ — Nexus HQ** (GitHub). Python, Flask, multi-agent. 19 agents from scratch: planning loops, tool calling, delegation, contextual memory, configurable LLM routing (cheap models for classification, expensive for generation).

## Experience (fact IDs)
- **E-ACETIANS** — Web Development Intern, Acetians Technologies Pvt. Ltd. (remote), Jul–Aug 2025: React components and REST integrations to client requirements; shipped on deadline.
- **E-DRDO** — Cybersecurity Intern, DRDO (Delhi), May–Jul 2025: ISO/IEC 15408 security evaluation of a DLP system; wrote Protection Profile documentation for government stakeholders.
- **E-EKAK** — Python Developer Intern, Ekak Innovations (Kanpur), Jun–Jul 2024: Python backend modules and integration test cycles on a production codebase.

## Skills (grouped)
- **Python/Backend:** Python (advanced), FastAPI, async, Pydantic, Node.js, REST, webhooks (idempotency, retry), PostgreSQL/Supabase (RLS, pgvector, Alembic), Redis + ARQ, Docker, GitHub Actions CI/CD, Sentry.
- **TypeScript/Frontend:** TypeScript (strict), React 18, Next.js 14 (App Router), TanStack Query, Tailwind, Vite, Recharts, Radix UI, Vitest, Playwright, responsive design, Google OAuth, TOTP 2FA, Razorpay.
- **AI/LLM:** multi-agent orchestration (tool calling, planning loops, memory, LLM routing), RAG (LangChain, pgvector, ChromaDB), ONNX embeddings, prompt engineering, LLM evals, Claude / OpenAI / Groq / Gemini APIs.
- **Automation/Integration:** n8n (production), VAPI voice agents, WhatsApp/Instagram/Messenger APIs, Airtable, Twilio, webhook pipelines, comment-to-DM lead capture, broadcast campaigns, Meta App Review.
- **Testing/Quality:** Vitest, Playwright E2E, mypy --strict, contract-first OpenAPI + generated TS types, axe-core (WCAG A/AA), CodeQL, CI quality gates.

## Role → best-evidence mapping (used by the Job Hunter generator)
| Target role | Lead with | Supporting |
|---|---|---|
| Python / Backend Developer | P-NEXORA (FastAPI, ARQ, idempotent webhooks, RLS), E-EKAK | P-WINDSCOPE backend (ETag, mypy --strict, contract-first) |
| AI Engineer | P-CHRONOLENS (eval harness F1 0.914 vs 0.301), P-NEXORA RAG, P-NEXUSHQ | LLM routing, prompt engineering |
| Full-Stack | P-NEXORA (Next.js + FastAPI, live SaaS), P-WINDSCOPE | Playwright/Vitest quality bar |
| Frontend | P-WINDSCOPE (React/TS, Recharts, a11y 0 violations, 98+48 tests), E-ACETIANS | Next.js dashboard in P-NEXORA |
| Automation Engineer | P-LEADAUTO (n8n, 60-second pipeline), P-NEXORA integrations | webhooks/retries |
| Software Engineer (general) | P-NEXORA + P-WINDSCOPE | CI/testing discipline |

## Caveats for the generator
- Do not state years of experience, employer-confirmed outcomes, revenue, user counts or "paying client" beyond what is written above.
- Graduation date is Apr 2026 on the resume; today is after that — do not describe the owner as a current student unless the owner confirms. (Open question → owner.)
- Internships are internships; do not inflate titles.
