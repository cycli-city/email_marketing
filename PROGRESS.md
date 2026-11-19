# PROGRESS — live status & feature-completion report

_Last updated: 2026-11-19 (project kickoff). Update this file after every meaningful change._

Legend: ✅ done & tested · 🟡 implemented, awaiting credentials · 🔒 depends on external approval · ⬜ not started · 🚧 in progress

## Phase status
| # | Phase | Status | Notes |
|---|---|---|---|
| 1 | Architecture & scaffold | 🚧 | Requirements/context/architecture/ADRs saved. Scaffold next. |
| 2 | Auth + integrations (Supabase JWT, Gmail OAuth, Groq, Redis/ARQ, provider framework) | ⬜ | |
| 3 | Lead discovery (crawler, extraction, verification, qualification) | ⬜ | |
| 4 | Outreach (templates, generation, campaigns, Gmail send, approvals, limits) | ⬜ | |
| 5 | Reply intelligence (sync, classify, notify, follow-ups) | ⬜ | |
| 6 | Dashboard (all pages + System Operations) | ⬜ | |
| 7 | Testing & hardening | ⬜ | |
| 8 | Deployment (Supabase, Render, Vercel) + docs | ⬜ | |

## Feature-completion report (REQUIREMENTS.md §21)
### Fully implemented and tested
_none yet_
### Implemented but awaiting credentials
_none yet_
### Dependent on external approval
- Gmail restricted scopes (readonly/modify): fine for owner-only use in "In production (unverified)" mode; Google verification/CASA needed if opened to third parties.
- Meta/WhatsApp not involved in this app.
### Not yet implemented
Everything above (see phase table).

## Open questions for the owner (ask only when blocking)
1. Always-on hosting budget (Render paid instance) — ADR-008.
2. Which Gmail address sends NEXORA mails vs Job Hunter mails? Custom domain mailbox for NEXORA?
3. Confirm NEXORA postal address in footer (Amritsar address from the public site) and whether "paying client" may be mentioned in outreach.
4. Google Drive links per role resume (promised "later").
5. Is the owner currently a student or graduated (resume says Apr 2026)? Affects job-mail wording.

## Credentials received
See `.local/SECRETS.local.md` (never listed here). Pending: GitHub, Supabase, Render, Vercel (+bypass secret), Groq, Google OAuth, Redis.
