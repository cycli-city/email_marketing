# Frontend

Next.js 16 (App Router, Cache Components) · TypeScript strict · Tailwind 4 · shadcn/ui (Base UI) · TanStack Query ·
Recharts. See the repository root `CLAUDE.md` and `docs/ARCHITECTURE.md`.

```bash
npm ci
cp .env.example .env.local        # NEXT_PUBLIC_API_URL etc. (only public values — never secrets)
npm run dev                       # http://localhost:3000

npm run lint && npm run typecheck && npm run format:check && npm test   # the unit gate CI runs
npm run build && npm run test:e2e # browser tests; needs the backend running (see playwright.config.ts)
npm run generate:api              # regenerate TS types after `python scripts/export_openapi.py` in backend/
```

**Contract-first:** `src/lib/api/schema.d.ts` is generated from the backend's OpenAPI schema
(`openapi/openapi.json`); CI fails if either file is out of date. Never edit them by hand.

**Auth/secrets:** the browser only ever holds the Supabase _anon_ key and the user's session token. Service keys,
OAuth refresh tokens and provider API keys live in the backend only.
