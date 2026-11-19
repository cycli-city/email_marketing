# SECURITY — threat model, controls and honest limits

Scope: a **personal, single-owner** system (ADR-012) that holds OAuth tokens for a Gmail account, API keys, and a
database of prospects' contact data. Security goal: nobody but the owner can read or act on any of it, secrets never
leave the backend, and an attacker who finds one bug still can't turn it into a takeover (defence in depth).

**Status legend:** ✅ implemented and covered by an automated test · 🟡 designed, lands in the named phase · ⚠️ residual risk we accept or can't remove.
This document is part of the definition of done — update it when a control changes.

## 1. Threat model → controls

| # | Threat | Controls | Proof (test / CI gate) | Status |
|---|---|---|---|---|
| 1 | **Secrets leaked** via git, CI logs or the browser bundle | `.gitignore` (+ `.local/`); `scripts/scan_secrets.py`: known token formats + **the exact values in `.local/SECRETS.local.md`** + forbidden filenames; pre-commit and pre-push hooks; CI scans full history; **built browser bundle scanned for secrets and server-only variable names**; gitleaks (pinned, checksum-verified); Pydantic `SecretStr` + log redaction; frontend env is `NEXT_PUBLIC_*` only | `test_scan_secrets.py` (39), hook block demo, CI `secret-scan` + `frontend` jobs | ✅ |
| 2 | **XSS** (stored / reflected / DOM) | Strict CSP: per-request **nonce + `strict-dynamic`**, no `unsafe-inline` for scripts, `object-src 'none'`, `base-uri 'self'`, allow-listed `connect-src`; React auto-escaping; ESLint **bans** `dangerouslySetInnerHTML`, `eval`, `new Function`, string `setTimeout`, `javascript:` URLs; API responses carry `default-src 'none'` | `csp.test.ts`, `proxy.test.ts`, `eslint-security.test.ts`, Playwright `security.spec.ts`: injected inline script, remote script, event handler, `javascript:` link all **blocked** | ✅ |
| 2b | XSS via **inbound email HTML** | Backend sanitises with `nh3` and stores text; UI renders text or a sandboxed iframe, never raw HTML | Phase 5/6 tests | 🟡 |
| 3 | **Data exfiltration** by injected code | CSP `connect-src` allow-list (self + API) | Playwright: `fetch` to a foreign origin is blocked | ✅ |
| 4 | **Clickjacking** | `frame-ancestors 'none'` + `X-Frame-Options: DENY` (web and API) | Playwright iframe test | ✅ |
| 5 | **Session hijacking / token theft** | ADR-014: BFF with `HttpOnly; Secure; SameSite=Strict` cookies — tokens never reach JavaScript; refresh-token rotation; pinned-algorithm JWT verification; owner allow-list; short-lived tokens. Until Phase 2 there is **no authentication surface and no browser-held credential** | Phase 2 tests | 🟡 |
| 6 | **CSRF** | API uses bearer tokens (no ambient cookies) and CORS is locked to `FRONTEND_URL`, no credentials; BFF routes will verify `Origin` + `SameSite=Strict` | `test_api_basics.py::TestCors`; Phase 2 | ✅ API / 🟡 BFF |
| 7 | **SQL injection** | SQLAlchemy bound parameters everywhere; no string-built SQL (ruff `S608` on); DB rejects malformed data via CHECK/unique constraints | `test_schema.py`, lint gate | ✅ |
| 8 | **MITM / downgrade** | TLS to every hop (§2); HSTS (2 years); 308 HTTP→HTTPS; production **refuses to start** with plaintext DB/URLs; optional verified-TLS to Postgres via CA file | `test_config.py` (TLS guards), `test_hardening.py::TestHttpsEnforcement` | ✅ |
| 9 | **Host-header attacks / open redirect** | `TrustedHostMiddleware` allow-list; redirects target the *configured* host, never the `Host` header; unsubscribe/OAuth links built from `BACKEND_URL` | `TestTrustedHost`, `test_redirect_cannot_be_steered_by_the_host_header` | ✅ |
| 10 | **Brute force / abuse / DoS** | See §3 | `test_ratelimit.py`, `test_hardening.py` | ✅ (⚠️ volumetric floods: see §3) |
| 11 | **IP spoofing to evade limits** | Client IP = N-th entry from the **right** of `X-Forwarded-For` (N = trusted proxies); short/malformed headers fall back to the unforgeable TCP peer | `test_net.py`, `test_forging_x_forwarded_for_cannot_evade_the_limit` | ✅ |
| 12 | **Credentials at rest** | Fernet (AES-128-CBC + HMAC) for OAuth tokens/API keys with key rotation (`MultiFernet`); never serialised to any API response; RLS enabled deny-all on every table (anon key reads nothing); append-only audit log (trigger) | `test_security.py`, `test_schema.py` | ✅ |
| 13 | **Forged links** (unsubscribe, OAuth `state`) | Purpose-bound HMAC tokens, `alg:none` rejected, expiry enforced | `test_security.py::TestActionTokens` | ✅ |
| 14 | **SSRF** from the website crawler (internal IPs, cloud metadata, DNS rebinding) | Public-IP-only resolution guard, redirect re-validation, DNS pinning, port/scheme limits, no credentials in URLs, response size/time caps | Phase 3 tests | 🟡 |
| 15 | **Prompt injection** (hostile website/email text steering the LLM) | Untrusted text delimited as data; no tools exposed to the model; strict JSON schema validation; **deterministic code** decides every action; grounding verifier rejects claims not in facts | Phase 3–5 tests / evals | 🟡 |
| 16 | **Email header injection / spoofed replies** | CR/LF rejection in every header value; our own `Message-ID`; Pub/Sub OIDC verification; webhook replay protection | Phase 4–5 tests | 🟡 |
| 17 | **Supply chain** | Actions pinned to **commit SHAs**; `uv.lock` + `package-lock.json`; hash-verified `pip-audit`, `npm audit` (prod tree); Dependabot; pinned+checksummed binaries; `persist-credentials: false`; build-only tooling kept out of the shipped tree | CI gates | ✅ |
| 18 | **Stolen GitHub token** | Fine-grained, single-repo scope; stored only in `.local/`; passed to git via environment (never argv or `.git/config`); header never logged; recommend expiry + rotation | `scripts/github.py`, config check | ✅ |
| 19 | **Accidental sends / abuse of the tool itself** | Approval mode default; central suppression; kill switch; compliance gate; exactly-once send protocol | Phases 4–5 | 🟡 |
| 20 | **Over-collection of personal mail** (the connected mailboxes also hold private email; `gmail.readonly` can technically read all of it) | Inbox scope guard (ADR-021): process only messages in threads we started or that reference our `Message-ID`; never forward unrelated mail to an LLM or store it; owner-visible audit of what was read; one-click disconnect revokes the token | Phase 5 tests | 🟡 |
| 21 | **Third-party data/ToS exposure** (free-tier LLMs may train on inputs; search APIs forbid storing results) | Per-provider "may receive personal data" flag in `docs/PROVIDERS.md` / `ProviderSpec`; personal data routed only to providers cleared for it; Brave results treated as transient | Phase 2–3 contract tests | 🟡 |

## 2. Transport security (HTTPS everywhere)

| Hop | Protection | Enforced by |
|---|---|---|
| Browser → Vercel (frontend) | TLS, HSTS 2 yrs, CSP `upgrade-insecure-requests` | Vercel TLS + `next.config.ts` headers + proxy |
| Browser → Render (API) | TLS, HSTS, HTTP→HTTPS 308, CORS allow-list | Render TLS + `HTTPSEnforcementMiddleware` |
| API/worker → Supabase Postgres | TLS required (`sslmode=require`); verified TLS with `DATABASE_SSL_CA_FILE` | production config validator |
| API/worker → Redis | *(none — no Redis; ADR-024)* | — |
| API/worker → Groq / Gmail / providers | HTTPS only (adapter rule); no redirects followed across hosts | adapter contract tests (Phase 2+) |
| Gmail Pub/Sub → webhook | HTTPS + OIDC token | Phase 5 |

Certificates are issued and rotated by Vercel/Render/Supabase — we never handle private keys.

## 3. Abuse and DDoS — what we do, and what no application can do

**Implemented (all tested):** per-IP sliding-window rate limits with stricter buckets for auth/unsubscribe; in-process (exact, single instance — ADR-024) with an
**bounded memory** (LRU) so a flood of unique keys cannot exhaust RAM, and protection never depends on another service being up; unspoofable client-IP resolution; global in-flight cap returning fast
`503`; request-body size limit (declared *and* streamed, correct `413`); bounded limiter memory (LRU); DB `statement_timeout` and
`idle_in_transaction_session_timeout`; liveness probe exempt so platform health checks keep working under load; uvicorn runs with
`--no-server-header`.

**⚠️ Honest limit:** no application code can stop a *volumetric* attack (traffic that saturates the network link before it reaches us).
That is mitigated upstream by Vercel's and Render's edge networks. On Render's **free** instance a determined attacker could still exhaust
CPU/RAM and take the service down for a while; for a personal tool we accept that. If it ever matters, put **Cloudflare** (free plan: DDoS
protection, WAF, bot rules) in front of the API — the app is already proxy-aware (`TRUSTED_PROXY_COUNT`).

## 4. What a reviewer should verify (commands)

```
python scripts/scan_secrets.py --tree --history                 # no secrets in code or history
python scripts/scan_secrets.py --path frontend/.next/static --forbid-names --allow-anon-jwt   # none shipped to browsers
cd backend  && pytest tests/unit/test_hardening.py tests/unit/test_ratelimit.py tests/unit/test_net.py tests/unit/test_config.py
cd frontend && npm test && npx playwright test e2e/security.spec.ts
```

## 5. Operations

- **Where secrets live:** `.local/SECRETS.local.md` (git-ignored) on the dev machine; Render env vars (backend); Vercel env vars (public values only); GitHub Actions secrets. Never in chat after first hand-off, tickets or commit messages.
- **Rotate `ENCRYPTION_KEY`:** prepend the new key (comma-separated, first encrypts) → deploy → run the re-encrypt task (`Encryptor.rotate`) → remove the old key.
- **Rotate the GitHub token:** create a new fine-grained token, replace the line in `.local/SECRETS.local.md`, revoke the old one in GitHub settings.
- **If a secret is ever committed:** treat it as compromised — rotate it first, then remove it from history, then add a scanner rule.
- **UptimeRobot:** HTTP(s) monitor on `https://<api-host>/readyz`, 5-minute interval (keeps the free Render instance awake and the free Supabase project active — ADR-008). Alert contact = owner. It exposes only `ok/error` per dependency.
- **Dependabot PRs:** review weekly; merge only with green CI.
- **Kill switch:** the workspace-level emergency stop (Phase 4) pauses all sending instantly; use it first, investigate second.

## 6. Known gaps / accepted risks (kept current)

1. Authentication and the cookie-session BFF are **not built yet** (Phase 2) — there is intentionally no login surface today.
2. The crawler's SSRF guard, webhook verification, email-header checks and prompt-injection evals arrive with their phases.
3. 4 moderate advisories exist in dev-only tooling (`shadcn` CLI → `ts-morph` → `minimatch` ReDoS); not shipped, not reachable at runtime.
4. `style-src-attr 'unsafe-inline'` (inline `style=""` attributes only) is a deliberate CSP allowance; Trusted Types is a possible future tightening.
5. Free-tier hosting means DDoS resilience relies on the platforms' edge (§3) and cold starts are possible after idle/redeploy (ADR-008).
6. Operational secrets are handled outside the repo (`.local/`, host secret stores); see §5 for rotation procedures.
