import { defineConfig, devices } from "@playwright/test";

import { BACKEND_URL, IDP_PORT, IDP_URL, OWNER_STATE } from "./e2e/support/constants";

const baseURL = process.env.E2E_BASE_URL ?? "http://localhost:3000";

/**
 * E2E runs against a REAL backend (API + Postgres) and the REAL dashboard build, with a fake Supabase Auth in front of
 * them (e2e/support/fake-idp.mjs — it signs genuine ES256 tokens, so nothing about login is mocked in the apps).
 *
 *   backend (started first, see .github/workflows/ci.yml) must be configured with:
 *     SUPABASE_URL=http://127.0.0.1:54321  OWNER_EMAILS=owner@example.com,shortlived@example.com  APP_ENV=test
 *   local: `python scripts/with_pg.py --name e2e --keep-data -- python -m uvicorn app.main:app_factory --factory --port 8000`
 *          (with those variables set), then `npm run build && npx playwright test`.
 *
 * Playwright starts the fake identity provider and the Next.js server itself. Projects:
 *   setup      signs in as the owner once and saves the session
 *   owner      the dashboard specs, running as the signed-in owner
 *   anonymous  login, sign-out and API-proxy specs, running with no session
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : 3,
  // Pages do two sequential API calls after hydration; leave headroom for loaded CI/laptop machines.
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: { baseURL, trace: "retain-on-failure" },
  projects: [
    { name: "setup", testMatch: /auth\.setup\.ts/ },
    {
      name: "owner",
      dependencies: ["setup"],
      testMatch: /(system|security|bff|integrations|sending)\.spec\.ts/,
      use: { ...devices["Desktop Chrome"], storageState: OWNER_STATE },
    },
    { name: "anonymous", testMatch: /(auth|login)\.spec\.ts/, use: { ...devices["Desktop Chrome"] } },
  ],
  webServer: [
    {
      command: "node e2e/support/fake-idp.mjs",
      url: `${IDP_URL}/__test__/health`,
      reuseExistingServer: !process.env.CI,
      env: { FAKE_IDP_PORT: IDP_PORT },
    },
    {
      command: "npm run start -- -p 3000",
      url: baseURL,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: {
        SUPABASE_URL: IDP_URL,
        SUPABASE_ANON_KEY: "e2e-publishable-key-not-a-secret",
        BACKEND_URL,
        APP_URL: baseURL,
      },
    },
  ],
});
