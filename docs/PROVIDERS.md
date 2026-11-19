# PROVIDERS — researched free-tier matrix and capacity plan (retrieved 2026-11-19, updated same day)

Required by `REQUIREMENTS_AUTONOMY.md` §B. Free tiers change often, so the `ProviderManager` never trusts this file at runtime: it
tracks real usage (provider-reported where an endpoint exists, otherwise an internal counter marked as an estimate). This file is the
starting point; **re-verify before relying on a number**.

Confidence: ✅ read on the vendor's own page · ⚠️ third-party sources only (confirm at sign-up) · ❌ checked and unusable.

## Usage policy
1. Use **many different providers**, each with **one account/key**, free-first (ADR-005, ADR-022). Failover is between providers.
2. **Never** create extra accounts/keys at the same provider to multiply a quota, rotate identities, or bypass limits (provider terms + `REQUIREMENTS_AUTONOMY.md` §D).
3. Paid fallback stays **off** unless the owner enables it per provider with a budget cap.
4. A contact is trusted only if it is **published on the company's own public page** (origin `discovered`) or **returned by a provider** (`provider`); sending additionally needs verification. Guessed patterns are never "verified".
5. Every provider carries a `may_receive_personal_data` flag; free LLM tiers that train on inputs never receive prospect personal data.

## 1. The sending constraint decides everything (read this first)
| Fact | Source | Consequence |
|---|---|---|
| **Resend** AUP prohibits *"cold outreach, purchased lists, or scraped contact data"*; opt-in recipients only ✅ | [Resend AUP](https://resend.com/legal/acceptable-use) | ❌ **never use for prospecting** (fine for transactional mail to the owner only) |
| **Brevo** bans purchased/scraped lists and non-opted-in recipients; suspension on breach ✅ | [Brevo anti-spam policy](https://www.brevo.com/en/legal/antispampolicy/) | ❌ **never use for prospecting** |
| Render free **blocks outbound SMTP 25/465/587** ✅ | [Render changelog](https://render.com/changelog/free-web-services-will-no-longer-allow-outbound-traffic-to-smtp-ports) | Mail goes through the **Gmail REST API (HTTPS)** only; SMTP-probe email verification is impossible |
| Gmail "Send mail as" a custom domain from a **consumer** Gmail breaks DMARC alignment (no DKIM for your domain) ⚠️ | [Tiger Technologies](https://blog.tigertech.net/posts/modern-gmail-send-as/), [Suped](https://www.suped.com/learn/dmarc/why-does-gmail-say-it-cannot-verify-my-authenticated-email) | NEXORA's `<NEXORA_FOUNDER_ADDRESS>` alias will likely show "via gmail.com" / fail DMARC → test; real fix = a Google Workspace mailbox for the domain (paid) |
| Google/Yahoo/Microsoft bulk-sender rules: complaints **< 0.3 %**, bounces **< 2 %**, SPF+DKIM+DMARC, one-click unsubscribe (strictly enforced at ~5,000/day, but the thresholds are the reputation yardstick) ⚠️ | vendor blogs | We auto-pause at a stricter threshold and auto-ramp only while healthy |
| New mailbox warm-up: start 5–20/day, ramp over 4–6 weeks, ~+20–30 % every 3 days if metrics hold ⚠️ | vendor guidance | Built-in adaptive ramp (ADR-025) |

## 2. Contact / company data
| Provider | Free tier | API on free? | Verdict |
|---|---|---|---|
| **Hunter.io** | 50 credits/mo ✅ (domain search 1/found email; finder 1/found; verify 0.5) | **Yes**, free key; 15 req/s · 500/min ✅; usage via Account endpoint + `X-Credits-Used` ✅ | ✅ use for decision-maker emails on top-scored leads |
| **People Data Labs** | ≈100 records/mo ⚠️ | company enrichment/search ⚠️; contact fields obfuscated ⚠️ | ✅ company qualification only |
| **Snov.io** | 50 credits/mo ✅ | ❌ API excluded on free ✅ | ❌ skip |
| **Apollo.io** | ~10 exports/mo ⚠️ | ❌ no usable API ⚠️ | ❌ skip |
| **Tomba** | 25 searches + 50 verifications/mo ⚠️ | free API but tiny rate limit (5/day) ⚠️ | ➖ marginal |
| **Prospeo** | 100 credits/mo ⚠️ | API on free contested ⚠️ | ⏳ verify at sign-up |
| LeadMagic / Enrow / Findymail / Anymail | one-time 10–100 credits or card-gated ⚠️ | – | ❌ not recurring |
| Skrapp / GetProspect | – | **LinkedIn-sourced** → violates "no LinkedIn" | ❌ excluded |
| Vibe Prospecting (Explorium) | unknown | – | ⏳ Claude connector in the owner's session, **not callable by the deployed backend** |

## 3. Email verification (own checks first: syntax, MX, disposable, role)
| Provider | Free tier | Verdict |
|---|---|---|
| **Verifalia** | **25 free credits/day** (recurring, resets midnight GMT, no accumulation) ✅ [help](https://verifalia.com/help/billing-pricing/how-do-daily-free-credits-work) | ✅ **best recurring** ≈ 750/mo |
| **ZeroBounce** | 100 credits/mo + 10 finder queries ✅ (business-domain sign-up email) | ✅ |
| **Abstract API** | 100 requests/mo ⚠️ | ✅ |
| MailboxValidator | 300 API queries/mo ⚠️ | ✅ verify at sign-up |
| Reoon | 600/mo *or* 100 trial — conflicting ⚠️ | ⏳ |
| Emailable / MillionVerifier / Bouncer | 250 / 100 / small — **one-time** ⚠️ | ➖ bootstrap |
| NeverBounce | no free tier ⚠️ | ❌ |
| Hunter verifier | inside the 50 credits (0.5 each) | ✅ shared budget |
→ ≈ **1,200–1,500 provider verifications/month** ≈ 40–50/day, spent on contacts about to be sent. Everything else relies on MX/disposable/role checks and **published-on-own-site** provenance; bounces feed suppression and auto-pause.

## 4. Search / research
| Provider | Free tier / terms | Verdict |
|---|---|---|
| **Tavily** | 1,000 credits/mo ✅ ([docs](https://docs.tavily.com/documentation/api-credits)) | ✅ primary |
| **Brave Search API** | $5 free credit/mo ≈ 1,000 requests ✅ ([page](https://brave.com/search/api/)); **no storage rights** on standard plans | ✅ transient use only |
| **Firecrawl** | 1,000 credits/mo, no card ⚠️ | ✅ optional (own crawler is default) |
| Exa | ≈$10 monthly credit ⚠️ | ➖ optional |
| Serper.dev / SerpApi | 2,500 one-time / 100–250 per mo ⚠️ | ➖ bootstrap |
| Jina Reader/Search | keyed free rate limits; token allowance unclear ⚠️ | ➖ optional |
| Google Custom Search JSON API | 100/day but **closed to new customers, ends 2027-01-01** ⚠️ | ❌ |

## 5. Business discovery — the volume engine (no per-call credits)
| Source | Terms | Notes |
|---|---|---|
| **Overture Maps — Places** | CDLA-Permissive v2 for Places; OSM-derived layers need ODbL attribution | Free S3 parquet (DuckDB). Schema has `websites`, `socials`, `emails`, `phones`, `confidence`, category in `basic_category`/`taxonomy`. **`emails` is usually null** ⚠️ → the website is the handle; crawl it. |
| **OpenStreetMap** (`email`, `website`, `phone` tags) | ODbL; public Overpass: <10,000 queries/day, <1 GB/day, identify User-Agent, pause 30 s on 429, **"commercial use should self-host or pay"** ✅ ([wiki](https://wiki.openstreetmap.org/wiki/Overpass_API)) | Prefer extracts/Overture over hammering public Overpass |
| **Geoapify Places** | **3,000 credits/day free, commercial use OK with attribution, no card** ✅ ([pricing](https://geoapify.com/pricing)); storage terms unclear ⚠️ | ✅ candidate for city/category sweeps — read ToS storage clause first |
| Company websites (About/Contact/Team) | robots.txt + ToS; SSRF-guarded crawler (Phase 3) | main source of *published* emails |
| Google Places API | caching limited to `place_id` | ❌ excluded |
| Foursquare Places | credits/terms unclear ⚠️ | ⏳ low priority |
| GitHub / LinkedIn | GitHub ToS forbids using emails for unsolicited mail; LinkedIn banned | ❌ excluded |

## 6. Job Hunter sources (verified openings + hiring signals)
| Source | Access | Terms / notes |
|---|---|---|
| **Greenhouse public Job Board API** | GET endpoints need no auth ✅ ([docs](https://developers.greenhouse.io/job-board.html)) | `boards-api.greenhouse.io/v1/boards/{token}/jobs` — verified open roles per company |
| **Lever public Postings API** | read-only, no auth ⚠️ (`api.lever.co/v0/postings/{site}`) | ToS bars *competitors* only ⚠️ |
| **Hacker News "Who is hiring?"** | Algolia HN API, no key ⚠️ | many posts include a contact email the poster published |
| **Arbeitnow** (Germany/EU) | no key ⚠️ | attribution |
| **RemoteOK / Remotive** | key-less feeds ⚠️ | attribution; **Remotive: ≤4 fetches/day, 24 h delay, don't repost** |
| **Adzuna** | free key, ~1,000 calls/mo ⚠️ | by country |
| **Jooble** | free key ⚠️ | |
| **UK Companies House API** | free key, 600 req / 5 min ✅ ([guidelines](https://developer.company-information.service.gov.uk/developer-guidelines)) | UK targets: official company data, officers, SIC |
| OpenCorporates | free tier conflicting ⚠️ | ❌ skip |

## 7. LLM inference (per-lead budget ≈ 2–3 calls; ≈ 300–400 calls/day at ~100 sends/day)
| Provider | Free tier | Trains on free-tier data? | Verdict |
|---|---|---|---|
| **Groq** | free tier, limits per account (console); remaining quota in `x-ratelimit-*` headers ✅; ≈1,000 req/day ⚠️ | no ⚠️ | ✅ primary |
| **GitHub Models** | ≈150–1,000 req/day ⚠️ | no ⚠️ | ✅ fallback |
| **NVIDIA NIM** | ≈1,000 req/day ⚠️ | no ⚠️ | ✅ fallback |
| **Cloudflare Workers AI** | ≈10,000 neurons/day ⚠️ | no ⚠️ | ✅ small tasks (classification) |
| **OpenRouter** | `:free` models 20 req/min · 50 req/day ⚠️ | no ⚠️ | ➖ last resort |
| Google Gemini (AI Studio) | 20–1,500 req/day ⚠️, "not guaranteed" ✅ | **yes (outside EEA/UK)** ⚠️ | ⚠️ **no personal data** — non-personal tasks only |
| Mistral "Experiment" | ≈1B tokens/mo ⚠️ | **yes** ⚠️ | ⚠️ no personal data |
| Cohere trial | ~100/day ⚠️ | – | ❌ **non-commercial only** |
| Cerebras | open free tier ended; $5 trial needs a card ⚠️ | – | ❌ default-off |
| Ollama (local) | free, owner's machine only | n/a | ➖ dev only |
Cost control: deterministic scoring does most qualification; the LLM only drafts and classifies. Usage is read from rate-limit headers where available.

## 8. Notifications, hosting and infrastructure terms
| Service | Finding |
|---|---|
| Telegram Bot API | free ✅ (owner's choice) |
| **GitHub Actions** | free quotas unchanged (≈2,000 Linux min/month) per **account**; public repos free ✅ ([changelog](https://github.blog/changelog/2025-12-16-coming-soon-simpler-pricing-and-a-better-experience-for-github-actions/)). Actions are for CI — **not** a place to run discovery crawls (GitHub's terms bar unrelated workloads). |
| **Supabase free** | ≈500 MB Postgres (read-only when exceeded), auto-pauses after ~1 week idle ⚠️ → retention budget below; `/readyz` pings keep it active |
| **Render free** | 750 h/mo; spins down after 15 min idle (≈1 min wake); docs: *"Do not use them for production applications"*; all free services suspend if hours run out ✅ ([docs](https://render.com/docs/free)) — owner accepted (one service, currently idle) |
| **Vercel Hobby** | ToS §4: *"…only … for your personal or non-commercial use"*; may terminate Hobby projects without notice ✅ ([terms](https://vercel.com/legal/terms)) — **owner decision pending** (ADR-023) |
| **UptimeRobot free** | 50 monitors, 5-min interval; "good for personal and commercial projects" ✅ ([pricing](https://uptimerobot.com/pricing/)) |

## 9. Capacity plan — can we reach ~9,000 contacts/month at 300 emails/day?
**Discovery: yes (~9k/month is realistic). Sending 300/day: not safely from free/personal infrastructure — see below.**

Funnel (monthly, free tiers only):
1. **Candidates:** ≈30–40k businesses from Overture/OSM/Geoapify filtered by category + country.
2. **Crawl** ≈20–25k public company sites (~3 polite fetches each ≈ 70k pages ≈ 2–3k/day — easily within a 0.1-CPU instance).
3. **Published emails** on roughly half of small-business sites → **≈9–12k contacts**.
4. **Eligibility gate** (suppression, dedupe, jurisdiction policy — e.g. Germany blocked by default, role/named handling) → **≈7–9k eligible**.
5. **Provider verification** on ≈1,200–1,500 (Verifalia 25/day, ZeroBounce 100, Abstract 100, Hunter, MailboxValidator) — spent on contacts about to be sent.
6. **Lead scoring** ranks everything; only the top slice is contacted — **quality over volume** lifts reply rates and protects sender reputation.

Sending (Gmail REST only — ESPs forbid cold outreach, §1). Adaptive ramp per mailbox (ADR-025):
| Phase | Per-mailbox daily limit |
|---|---|
| Days 1–3 | 10 |
| Days 4–7 | 15 |
| Week 2 | 25 |
| Week 3 | 35 |
| Week 4 | 50 |
| Month 2+ | up to 60–80, raised ≈20 % every 3 days only while bounce < 2 % and complaints ≈ 0; halved on breach |
Ceiling (configurable, with warnings): personal Gmail 150/day, Workspace 400/day per mailbox.
**Two mailboxes (NEXORA + Job Hunter) ⇒ ≈ 100–130/day at steady state ≈ 2,500–3,500 emails/month; month 1 ≈ 1,200–1,600.**
Reaching a true 300/day (≈9k/month) needs authenticated domain mailbox(es) (Google Workspace, paid) plus earned reputation. That is a **spend decision**, not a software limit; the software supports it (higher configured ceilings) but will not push an account past what its health metrics support, and will never spread volume across mailboxes to dodge limits.

Storage budget (Supabase 500 MB): ≈6–10 KB per stored lead ⇒ ≈55–90 MB per 9k leads. Retention job: purge rejected/duplicate candidates after 14 days (keep a dedupe hash), usage events 30 days, activity feed 60 days; suppression and sent-message history are kept.

## 10. Keys the owner will be asked for (values go in `.local/SECRETS.local.md`; the template there lists every one)
Phase 2: Supabase (URL, anon key, service-role key, DB connection string — direct/session mode, JWT secret/JWKS), Groq, Google Cloud OAuth client (Gmail).
Phase 3: Hunter, PDL, Verifalia, ZeroBounce, Abstract (+MailboxValidator), Tavily, Brave, Firecrawl (optional), Geoapify, Companies House, Adzuna, Jooble, GitHub Models / NVIDIA NIM / Cloudflare Workers AI / OpenRouter.
Phase 4–5: Telegram bot token + chat id; DNS access for `nexora-ai.co.in` (SPF/DKIM/DMARC).
Phase 8: Render API key, Vercel token (+ protection-bypass secret).
