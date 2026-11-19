# Frontend

Next.js 16 (App Router, Cache Components) · TypeScript strict · Tailwind 4 · shadcn/ui (Base UI) · TanStack Query ·
Recharts. See the repository root `CLAUDE.md` and `docs/ARCHITECTURE.md`.

```bash
npm ci
cp .env.example .env.local        # SUPABASE_URL, SUPABASE_ANON_KEY, BACKEND_URL, APP_URL — server-side only
npm run dev                       # http://localhost:3000

npm run lint && npm run typecheck && npm run format:check && npm test   # the unit gate CI runs
npm run build && npm run test:e2e # browser tests: real backend + a fake Supabase Auth (see playwright.config.ts for the backend env)
npm run generate:api              # regenerate TS types after `python scripts/export_openapi.py` in backend/
```

**Contract-first:** `src/lib/api/schema.d.ts` is generated from the backend's OpenAPI schema
(`openapi/openapi.json`); CI fails if either file is out of date. Never edit them by hand.

**Auth/secrets (ADR-014/032/033):** the browser holds **no token and no key**. Sign-in happens in a Server Action; the Supabase
session lives in an `HttpOnly; Secure; SameSite=Lax` `__Host-` cookie that page JavaScript cannot read, and every API call goes
through the same-origin proxy `/bff/*`, which attaches the token server-side. Service keys, OAuth refresh tokens and provider API
keys live in the backend only.
