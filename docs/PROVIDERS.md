# PROVIDERS — researched free-tier matrix (retrieved 2026-11-19)

Required by `REQUIREMENTS_AUTONOMY.md` §B ("evaluate each provider's current API availability, free-tier eligibility,
restrictions, commercial-use permission, rate limits and terms **before** implementing it"). Free tiers change often — the
`ProviderManager` never trusts this file at runtime: it tracks real usage (provider-reported when an endpoint exists, otherwise an
internal counter marked as an estimate) and this file is only the starting point. **Re-verify before each paid/credit decision.**

Confidence: ✅ read on the vendor's own page · ⚠️ only third-party sources (confirm at sign-up) · ❌ checked and unusable.

## Usage policy (owner-approved intent + project rules)
1. Use **many different providers**, each with its **own single account/key**, free-first (ADR-005, ADR-022). The manager fails over between *providers*.
2. **Never** create extra accounts/keys at the *same* provider to multiply a free quota, rotate identities, or bypass limits. It violates provider terms and the owner's own written rule (`REQUIREMENTS_AUTONOMY.md` §D). (OpenRouter states extra keys don't raise limits anyway.)
3. Paid fallback stays **off** unless the owner enables it per provider, with a budget cap.
4. Contact data is only trusted if it is **published on the company's own public page** (origin `discovered`) or **returned by a provider** (origin `provider`) and, for sending, passes verification. Guessed patterns are never "verified".

## Contact / company data
| Provider | Free tier | API on free? | Notes | Verdict |
|---|---|---|---|---|
| **Hunter.io** | 50 credits/month ✅ (domain search 1 credit per email found; finder 1 per found; verify 0.5) | **Yes — free API key**, 15 req/s · 500/min ✅ | `X-Credits-Used` header + Account endpoint for usage. Paid-only: offset pagination. | ✅ **Use** for decision-maker emails on the best leads (≈50 finds or ≈100 verifications/month) |
| **People Data Labs** | ≈100 records/month ⚠️ (docs say free plan = 100) | Company Enrichment/Search on self-serve ⚠️ | Free tier reportedly **obfuscates contact data** (company firmographics usable) | ✅ **Use for company qualification** (size, industry, location), not for emails |
| **Snov.io** | 50 credits/month ✅ | ❌ **API excluded on the free trial** (official page) | | ❌ skip (UI-only) |
| **Apollo.io** | ~10 export credits/month ⚠️ | ❌ no full API on free ⚠️ | Conflicting sources | ❌ skip unless owner has a paid plan |
| Prospeo / Findymail / Anymail Finder | 10–100 one-time or card-gated ⚠️ | – | Anymail needs a card; Findymail 10 credits | ❌ not worth a slot |
| Skrapp / GetProspect | 50–150/month ⚠️ | – | **LinkedIn-sourced** → violates our "no LinkedIn" rule | ❌ excluded |
| Vibe Prospecting (Explorium) | unknown | – | Available as a Claude connector in the owner's session, **not callable by the deployed backend**; its REST API/credits not evaluated | ⏳ evaluate later |

## Email verification (our own checks come first: syntax, MX, disposable, role)
| Provider | Free tier | Notes | Verdict |
|---|---|---|---|
| **ZeroBounce** | 100 credits/month + 10 finder queries ✅ (needs a business-domain sign-up email) | Usage endpoint available | ✅ use |
| **Abstract API** (Email Validation) | 100 requests/month ⚠️ | | ✅ use |
| **Reoon** | 600/month ⚠️ *or* 100 trial — sources conflict | Confirm at sign-up | ⏳ verify |
| Emailable / MillionVerifier | 250 / 100 **one-time** ⚠️ | not recurring | ➖ optional |
| NeverBounce | no free tier ⚠️ | pay-per-use | ❌ |
| Hunter verifier | counted in the 50 credits (0.5 each) | | ✅ (shares Hunter budget) |

## Search / research
| Provider | Free tier | Terms to respect | Verdict |
|---|---|---|---|
| **Tavily** | 1,000 credits/month ✅ (search 1 credit basic, 2 advanced; extract 1 per 5 URLs) | | ✅ primary search |
| **Brave Search API** | $5 free credit/month ≈ 1,000 requests ✅ (Search plan $5/1k, 50 req/s) | **No storage/caching rights on standard plans** ✅ → treat results as transient; store only what *our own crawl* of the public site finds. Card requirement unclear. | ✅ use (transient only) |
| Exa | ≈$10 monthly credit ⚠️ | | ✅ optional |
| Serper.dev | 2,500 **one-time** ⚠️ | | ➖ bootstrap only |
| SerpApi | 100–250/month ⚠️ | | ➖ optional |
| Firecrawl | small one-time credit ⚠️ | our own crawler (robots-respecting) is the default | ➖ optional |
| Google Places API | free monthly SKU thresholds | **Terms allow caching only `place_id` (lat/lng 30 days)** → cannot build a lead DB | ❌ excluded |

## Open data for business discovery (the real volume source — no per-call credits)
| Source | License / limits | Notes |
|---|---|---|
| **Overture Maps — Places** | Places theme under CDLA-Permissive v2 ✅(per Overture); OSM-derived layers carry ODbL attribution | Free S3 parquet, query by category + country with DuckDB; includes name, category, address, website/phone (and emails where present — verify with a sample query). Best fit; can run as a scheduled GitHub Action to spare the small Render instance. |
| **OpenStreetMap** (`email`, `contact:email`, `website`, `phone` tags) | ODbL: commercial use OK with attribution; share-alike applies to *published* derived databases | Public **Overpass** instance policy ✅: <10,000 queries/day, <1 GB/day, identify via User-Agent, pause 30 s on 429/406 — **and commercial use "should use self-hosted or paid" servers** → prefer Overture/OSM extracts over hammering the public Overpass. |
| Company websites (About/Contact/Team) | robots.txt + ToS; SSRF-guarded crawler (Phase 3) | Main source of *published* business emails; expect roughly half of small-business sites to list one. |

## LLM inference
| Provider | Free tier | Notes | Verdict |
|---|---|---|---|
| **Groq** | Free tier; limits visible per account (console) ✅; remaining quota exposed in `x-ratelimit-*` headers ✅ | Primary (owner's choice) | ✅ primary |
| **Google Gemini API** | Free tier; per-project limits shown in AI Studio ✅, "not guaranteed" ✅ | **Verify free-tier data-use terms before sending any prospect personal data** (training use is typical on free tiers) | ✅ fallback for *non-personal* tasks |
| **OpenRouter** | `:free` models: 20 req/min, 50 req/day (1,000/day after ≥$10 credit) ⚠️ | Extra keys don't raise limits | ➖ last-resort fallback |
| Cerebras | open free tier ended (Jul 2026) → $5 trial credit, **card required** ⚠️ | | ❌ default-off |
| Ollama (local) | free, runs on the owner's machine only | not available on the free Render instance | ➖ dev only |

## Email delivery / inbound
| Provider | Free tier | Notes |
|---|---|---|
| **Gmail API** | Account limits apply (personal ~500/day hard; our policy default 20–50/day with warm-up) | Primary (spec). Alias sending: see ADR-021. |
| **Resend** | 3,000 emails/month, **100/day**, 3 domains, **inbound webhooks**, API ✅ (received emails count toward quota) | Best fallback/primary option if Gmail-alias deliverability is poor: DKIM/SPF can be authenticated for `nexora-ai.co.in`. |
| **Brevo** | 300 emails/day, SMTP + API + webhooks ⚠️ (cap shared with marketing; free-plan branding reported) | Alternative fallback. |
| Telegram Bot API | free | Notifications (owner's choice). |

## Hosting / infrastructure terms that affect the plan (see ADR-023)
| Service | Finding | Impact |
|---|---|---|
| **GitHub Actions** | Free plan quotas "remain the same" (≈2,000 Linux min/month ≈ "33 hours") ✅; allowance is per **account** (shared by all private repos), public repos free ✅ | A secondary source claims a dollar-pool change on 2026-10-28 (≈ same minutes) — unconfirmed. Check *Settings → Billing → Usage*. |
| **Vercel Hobby** | ToS §4 (verbatim): *"You shall only use the Services under a Hobby plan for your personal or non-commercial use."* Vercel *"may shut down and terminate projects or deployments using the Hobby plan without notice."* ✅ | The dashboard operates NEXORA's sales (commercial) → **owner decision needed** (Pro ≈ $20/mo, or a host that allows commercial free use). |
| **Render free** | 750 instance-hours/month per workspace; spins down after 15 min idle (≈1 min wake); docs say *"Do not use them for production applications"*; if hours are exhausted **all** free web services are suspended ✅. Free Key Value is non-persistent (acceptable: Redis is transport only). Free Postgres expires in 30 days (we use Supabase). | One always-on service ≈ 744 h fits; a **second** free service would risk suspending the API → frontend must not also live on Render free. |
| **UptimeRobot free** | 50 monitors, 5-min interval; page says *"Good for personal and commercial projects"* ✅ | Fine. |
| Supabase free | pauses after inactivity (our `/readyz` ping keeps it active) | ADR-008. |

## Realistic monthly capacity on free tiers (sets expectations honestly)
- Paid-data **lookups**: ≈ 50 Hunter finds + ≈ 100 PDL company enrichments + ≈ 100–300 verifications across ZeroBounce/Abstract(/Reoon) — **a few hundred per month in total**, so they are spent on the best-scored leads only.
- **Search**: ≈ 1,000 Tavily + ≈ 1,000 Brave searches/month.
- **Volume** comes from open data (Overture/OSM) + our own crawl of public company sites, verified with our built-in checks first.
- **Sending is the real ceiling**: a safe Gmail/alias warm-up is ~20–50/day ⇒ **~600–1,500 emails/month**, so discovery only needs to stay a little ahead of that.

## Keys the owner will be asked for (only when the phase needs them; values go in `.local/SECRETS.local.md`)
Phase 2: Supabase (URL, anon key, service-role key, DB connection string — direct/session mode, JWT secret/JWKS), Groq key, Google Cloud OAuth client (Gmail), Redis URL (Render Key Value or Upstash).
Phase 3: Hunter, PDL, ZeroBounce, Abstract (+Reoon), Tavily, Brave, (Exa), Gemini, (OpenRouter).
Phase 4–5: Resend (+ DNS access for `nexora-ai.co.in` SPF/DKIM/DMARC), Telegram bot token + chat id.
Phase 8: Render API key, Vercel token (+ protection-bypass secret).
