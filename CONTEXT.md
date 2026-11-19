# CONTEXT — Owner, accounts, product facts, decisions

> Everything the owner told me in conversation that is **not** in the two requirement files.
> Secrets are NOT here — they live in `.local/SECRETS.local.md` (gitignored). Last updated: 2026-11-19.

## 1. Owner & authorisations

- **Owner:** Divyansh Khanna — software engineer (full-stack / AI systems / automation), Amritsar, India. Final-year B.Tech IT at VIT (graduating Apr 2026 per resume).
- **GitHub:** user `cycli-city`; this repo = `email_marketing` (**private**). Remote: `https://github.com/cycli-city/email_marketing`.
- **Delegation (explicit):** owner said *"you do all the work… u do read and write across Supabase, Render, Vercel and GitHub… take care of all things."* They will hand over: Render API key, Supabase keys, Vercel token + Vercel *deployment protection bypass* secret, GitHub token (if needed to push). Also any other API key I ask for.
- **Latitude:** *"you don't have to exactly do what I say, just do everything best, whatever's better do that, no strict rule to follow… no compromises in quality… take as much time as you need… very professional project like a very senior engineer made it… make sure everything works, no issues, automated tests so nothing breaks whenever pushed."*
- **Scope (owner, 2026-11-19): "it's all my personal project, just for my personal use."** → Single-owner tool, not a multi-customer SaaS: signup is closed (owner email allowlist), no billing/plans/admin-for-others, Gmail OAuth runs in owner-only mode (no Google verification needed), UI optimised for one operator. `workspace_id` is kept in the schema (cheap, keeps isolation tests meaningful, leaves the door open). **Personal use does not relax compliance** — unsubscribe/suppression/jurisdiction rules stay on.
- **Ask for API keys only when actually needed** — one at a time, never in a way that has them paste secrets into committed files. Tell them to put values into `.local/SECRETS.local.md` or paste in chat → I move them into `.local/` and the host's secret manager (Render/Vercel env vars, GitHub Actions secrets).
- **Still requires explicit go-ahead** (outward-facing / irreversible): sending any real email to a third party, activating a real campaign, dropping or truncating production data, force-pushing, deleting cloud resources, spending money on paid plans/credits.

## 2. The two products the platform serves

### Mode A — NEXORA SALES (selling NEXORA AI)
Facts below were read from the public site `https://www.nexora-ai.co.in` on 2026-11-19. **Only these may be asserted in outreach.**

- **What:** AI business-messaging automation — "Your Complete Business Operating System". AI auto-reply on WhatsApp, Instagram and Facebook Messenger, "trained on your business".
- **Features:** AI auto-reply; smart CRM per industry; digital business cards + WhatsApp payment links; Instagram lead generation (DMs/comments → qualified leads); WhatsApp broadcast campaigns; end-to-end customer-journey automation; contact CSV import; automations; analytics history (Growth+).
- **Channels:** WhatsApp (all plans); Instagram + Messenger (Growth and up).
- **Industries it names:** gyms/fitness, dental clinics, salons/spas, restaurants/cafes, real estate, medical clinics, hotels/hospitality, travel/tourism, education/coaching, automobile service.
- **Pricing (INR/month):** Free (1 WhatsApp no., 100 AI replies, 50 contacts — mentioned in FAQ) · Starter ₹4,999 · Growth ₹9,999 (most popular; 3 WA numbers, 5k replies, Instagram + Messenger, broadcasts, automations) · Pro ₹19,999 (10 numbers, unlimited replies/contacts, dedicated manager) · Enterprise custom. Annual = "2 months free" (amounts not shown). Free trial, no card required, length not stated. **Prices are INR — never convert/assume USD figures.**
- **Claims the site makes:** replies "within seconds", "setup in minutes", "24/7". The site is candid that it is **a new product with no customer counts, testimonials or case studies** → outreach must NOT imply traction, results, customers or ROI numbers. Prefer: free trial, founder-led setup call, concrete use-case ("answer WhatsApp enquiries after hours and book appointments").
- **Contact / footer data:** support@nexora-ai.co.in · Lane No. 17, Vrindavan Gardens, Amritsar, Punjab, India (usable as the physical postal address in CAN-SPAM footers — confirm with owner before use) · © NEXORA AI 2026.
- **Not stated anywhere** (never invent): founder/team bios, funding, company size, demo-booking link, trial length, annual prices, integrations beyond WA/IG/Messenger.
- **Owner's technical proof points** (from resume; usable for credibility in NEXORA mails if accurate): live multi-tenant SaaS, Meta App Review approved (14 permissions), first paying client. Treat "paying client" as a fact the owner can confirm before it appears in any email.
- A login for the NEXORA dashboard exists (see `.local/SECRETS.local.md`). Not needed for the build; only use if product detail is missing.

### Mode B — JOB HUNTER (finding roles / recruiters for the owner)
- Target roles: **Software Engineer, AI Engineer, Python Developer, Full-Stack, Backend, Frontend** (+ Automation Engineer, others configurable).
- The owner keeps **a separate resume per role**, each hosted on **Google Drive** — links will be provided later. Design decision: a `resume_assets` table (`role_key`, `label`, `drive_url`, `file_id`, `is_default`); outreach picks the right variant per target role and includes the **link in the email body** (optionally attaches the file via Drive download if the owner wants). Until links arrive, the system must refuse to send job mails that need a CV and say why (no fake links).
- A **general resume** was uploaded: facts distilled into `docs/profile/RESUME_PROFILE.md`. It is the ONLY source of "personal experience" the email generator may cite.
- Separate sender identity, templates, categories and campaign data from NEXORA (hard requirement).

## 3. Sender identities (to be confirmed with owner when needed)
- Open question: which Gmail address(es) send NEXORA mails vs Job Hunter mails (separate identities required). Default plan: **two connected Gmail accounts** (one per mode), each with its own limits/warm-up. Never rotate accounts to dodge limits.
- Open question: custom domain mailbox for NEXORA (e.g. `name@nexora-ai.co.in`)? If yes, SPF/DKIM/DMARC guidance + Google Workspace or Brevo/Resend path applies.

## 4. Dev machine facts (Windows 11, inspected 2026-11-19)
- Present: Node v24.15, npm 11.12, Python 3.11.9 (Microsoft Store build), git 2.54, winget 1.30. 16 GB RAM, ~52 GB free.
- **Absent:** Docker, `gh`, `psql`, Redis, `uv`, `pnpm`, Supabase/Vercel CLIs.
- Consequences: GitHub/Render/Vercel/Supabase are driven through their **REST APIs / `npx` CLIs** with the owner-supplied tokens; integration tests that need Postgres/Redis run in **GitHub Actions service containers** and locally via a pip-installable Postgres (see `docs/DECISIONS.md`) + `fakeredis`.
- git identity configured globally (name + email).

## 5. Credentials checklist (values only in `.local/SECRETS.local.md`)
| Needed for | Item | Status |
|---|---|---|
| Repo push, Actions secrets | GitHub token (fine-grained, repo `cycli-city/email_marketing`: contents, actions, secrets, workflows) | pending from owner |
| Database + auth | Supabase project URL, anon key, service-role key, DB connection string, JWT secret | pending |
| Backend hosting | Render API key | pending |
| Frontend hosting | Vercel token + deployment-protection bypass secret | pending |
| LLM | Groq API key (primary) — Gemini/Cerebras/OpenRouter optional | pending |
| Gmail | Google Cloud OAuth client ID/secret (+ Pub/Sub topic) | pending — I'll write the exact console steps when needed |
| Redis | Upstash/Render Redis URL | pending |
| Optional discovery | Hunter, Brave/Tavily/Exa, Firecrawl, Snov, PDL… | optional; only if owner wants them |
| Notifications | Telegram bot token + chat id | optional |

## 6. Standing method (summary — full rules in `CLAUDE.md`)
Spec → plan → build in phases → test every layer → CI green → push → deploy → verify live → update `PROGRESS.md`. Provider integrations are researched against **current** docs/pricing before being coded (training-data knowledge of free tiers goes stale) and recorded in `docs/PROVIDERS.md`.
