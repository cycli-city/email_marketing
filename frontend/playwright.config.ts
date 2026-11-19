import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.E2E_BASE_URL ?? "http://localhost:3000";

/**
 * E2E runs against a REAL backend (API + Postgres + Redis). Start it first:
 *   CI:    see .github/workflows/ci.yml (service containers + uvicorn)
 *   local: backend/scripts/dev_redis.py + `python scripts/with_pg.py -- uvicorn app.main:app_factory --factory`
 * The Next.js server is started (or reused) by Playwright; run `npm run build` beforehand.
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
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "npm run start -- -p 3000",
    url: baseURL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
