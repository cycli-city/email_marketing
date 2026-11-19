import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

import {
  BACKEND_URL,
  IDP_URL,
  INTRUDER,
  OWNER,
  PASSWORD,
  SESSION_COOKIE_PREFIX,
  SHORT_LIVED,
} from "./support/constants";
import { forgeAccessToken, sessionCookieValue } from "./support/forge";

interface IdpState {
  signIns: number;
  failedSignIns: number;
  refreshes: number;
  logouts: string[];
  lastAccessToken: string | null;
  lastRefreshToken: string | null;
}

const idpState = async (page: Page): Promise<IdpState> =>
  (await page.request.get(`${IDP_URL}/__test__/state`)).json();

const sessionCookies = async (page: Page) =>
  (await page.context().cookies()).filter((c) => c.name.startsWith(SESSION_COOKIE_PREFIX));

async function signIn(page: Page, email: string, password = PASSWORD, next?: string) {
  await page.goto(next ? `/login?next=${encodeURIComponent(next)}` : "/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

const BAD_CREDENTIALS = "Invalid email or password.";

// Next.js keeps its own hidden role="alert" route announcer on every page; only the form's alert is ours.
const formAlert = (page: Page) => page.locator("form").getByRole("alert");

/**
 * Plants the session cookie the way a server would — through a real `Set-Cookie` header. (Browsers refuse to let tools
 * inject a `__Host-` cookie any other way, which is exactly the protection that prefix is for.)
 */
async function plantSessionCookie(page: Page, value: string) {
  await page.route("**/login", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/plain",
      headers: { "set-cookie": `${SESSION_COOKIE_PREFIX}=${value}; Path=/; Secure; HttpOnly; SameSite=Lax` },
      body: "planted",
    }),
  );
  await page.goto("/login");
  await page.unroute("**/login");
}

test.describe("the login gate", () => {
  test("an anonymous visitor to a dashboard page is sent to the login page and brought back after signing in", async ({
    page,
  }) => {
    await page.goto("/system");
    await expect(page).toHaveURL(/\/login\?next=%2Fsystem$/);
    await expect(page.getByRole("heading", { name: "AI Outreach Intelligence" })).toBeVisible();

    await page.getByLabel("Email").fill(OWNER.email);
    await page.getByLabel("Password").fill(PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/system$/);
    await expect(page.getByRole("heading", { name: "System status" })).toBeVisible();
    await expect(page.getByTestId("signed-in-as")).toHaveText(OWNER.email);
  });

  test("the login page has no accessibility violations (WCAG A/AA)", async ({ page }) => {
    await page.goto("/login");
    await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
    expect(results.violations).toEqual([]);
  });

  test("the login page ships the hardening headers and is never cached", async ({ request }) => {
    const response = await request.get("/login");
    const h = response.headers();
    expect(response.status()).toBe(200);
    expect(h["content-security-policy"]).toMatch(/script-src 'self' 'nonce-[^']+' 'strict-dynamic'/);
    expect(h["content-security-policy"]).toMatch(/connect-src 'self'(;|$)/);
    expect(h["content-security-policy"]).toContain("form-action 'self'");
    expect(h["x-frame-options"]).toBe("DENY");
    expect(h["cache-control"]).toContain("no-store");
  });

  test("wrong password and unknown email get the identical message and leave no session behind", async ({
    page,
  }) => {
    await signIn(page, OWNER.email, "definitely-not-the-password");
    await expect(formAlert(page)).toHaveText(BAD_CREDENTIALS);
    expect(await sessionCookies(page)).toHaveLength(0);

    await signIn(page, "nobody@example.com");
    await expect(formAlert(page)).toHaveText(BAD_CREDENTIALS); // no way to tell which addresses exist
    expect(await sessionCookies(page)).toHaveLength(0);
    await expect(page).toHaveURL(/\/login/);
  });

  test("a failed attempt keeps the typed email (so a typo costs one retype, not two) and never echoes the password", async ({
    page,
  }) => {
    await signIn(page, OWNER.email, "definitely-not-the-password");
    await expect(formAlert(page)).toHaveText(BAD_CREDENTIALS);
    await expect(page.getByLabel("Email")).toHaveValue(OWNER.email);
    await expect(page.getByLabel("Password")).toHaveValue("");
  });

  test("a valid account that is not the owner is refused and its session is discarded", async ({ page }) => {
    const before = await idpState(page);
    await signIn(page, INTRUDER.email);
    await expect(formAlert(page)).toHaveText("This account is not authorised to use this dashboard.");
    expect(await sessionCookies(page)).toHaveLength(0);
    await expect(page).toHaveURL(/\/login/);
    const after = await idpState(page);
    expect(after.logouts.length).toBeGreaterThan(before.logouts.length); // the unwanted session was revoked
  });

  test("the login page only accepts same-site return addresses (no open redirect)", async ({
    page,
    baseURL,
  }) => {
    for (const hostile of ["https://evil.example/phish", "//evil.example", "/\\evil.example"]) {
      await signIn(page, OWNER.email, PASSWORD, hostile);
      await expect(page).toHaveURL(/\/system$/);
      expect(new URL(page.url()).origin).toBe(new URL(baseURL!).origin);
      await page.context().clearCookies();
    }
  });

  test("a message can only be chosen from a fixed list, never injected through the URL", async ({ page }) => {
    await page.goto(`/login?error=${encodeURIComponent("<img src=x onerror=alert(1)>")}`);
    await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
    await expect(formAlert(page)).toHaveCount(0);
    await page.goto("/login?error=session");
    await expect(formAlert(page)).toHaveText("Your session has ended. Please sign in again.");
  });
});

test.describe("the session cookie", () => {
  test("is locked down, and no token ever reaches page JavaScript, storage, HTML or any response body", async ({
    page,
  }) => {
    const bodies: string[] = [];
    page.on("response", async (response) => {
      if (!response.url().startsWith("http://localhost:3000")) return;
      try {
        bodies.push(await response.text());
      } catch {
        // redirects and aborted requests have no body
      }
    });

    await signIn(page, OWNER.email);
    await expect(page).toHaveURL(/\/system$/);
    await expect(page.getByText("All systems operational")).toBeVisible();

    const cookies = await sessionCookies(page);
    expect(cookies.length).toBeGreaterThan(0);
    const inAWeek = Date.now() / 1000 + 7 * 24 * 3600 + 120;
    for (const cookie of cookies) {
      expect(cookie.httpOnly).toBe(true); // JavaScript cannot read it
      expect(cookie.secure).toBe(true);
      expect(cookie.sameSite).toBe("Lax");
      expect(cookie.path).toBe("/");
      expect(cookie.domain).toBe("localhost"); // host-only (no leading dot): not shared with sub-domains
      expect(cookie.expires).toBeGreaterThan(Date.now() / 1000);
      expect(cookie.expires).toBeLessThan(inAWeek); // a week, not the library's default 400 days
    }

    const { lastAccessToken, lastRefreshToken } = await idpState(page);
    expect(lastAccessToken).toBeTruthy();
    const pageView = await page.evaluate(() => ({
      cookie: document.cookie,
      local: JSON.stringify({ ...localStorage }),
      session: JSON.stringify({ ...sessionStorage }),
      html: document.documentElement.outerHTML,
    }));
    for (const [where, text] of Object.entries({ ...pageView, network: bodies.join("\n") })) {
      expect(text, `access token leaked into ${where}`).not.toContain(lastAccessToken!);
      expect(text, `refresh token leaked into ${where}`).not.toContain(lastRefreshToken!);
    }
    expect(pageView.cookie).not.toContain("aoi-session");
  });

  test("is refreshed automatically while the owner keeps using the dashboard", async ({ page }) => {
    await signIn(page, SHORT_LIVED.email); // this account's access tokens live 95 s: due for refresh after ~5 s
    await expect(page).toHaveURL(/\/system$/);
    const before = await idpState(page);
    const valueBefore = (await sessionCookies(page)).map((c) => c.value).join("|");

    await page.waitForTimeout(6_500); // the token is now inside the client's 90 s refresh window
    await page.goto("/system");

    await expect(page).toHaveURL(/\/system$/); // still signed in: the proxy refreshed it behind the scenes
    await expect(page.getByTestId("signed-in-as")).toHaveText(SHORT_LIVED.email);
    expect((await idpState(page)).refreshes).toBeGreaterThan(before.refreshes);
    expect((await sessionCookies(page)).map((c) => c.value).join("|")).not.toBe(valueBefore);
  });

  test("a session cookie is only trusted if its token is genuinely signed", async ({ page, context }) => {
    // Control: the same cookie shape carrying a token the identity provider really issued is accepted…
    const real = await page.request.post(`${IDP_URL}/__test__/mint`, { data: { email: OWNER.email } });
    const { access_token: genuine } = await real.json();
    await plantSessionCookie(page, sessionCookieValue(genuine, OWNER.email));
    await page.goto("/system");
    await expect(page.getByTestId("signed-in-as")).toHaveText(OWNER.email);

    // …while one signed with any other key — even claiming the owner's identity — is not.
    await context.clearCookies();
    await plantSessionCookie(page, sessionCookieValue(forgeAccessToken(OWNER.email), OWNER.email));
    await page.goto("/system");
    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByTestId("signed-in-as")).toHaveCount(0);
  });

  test("signing out ends the session everywhere and removes the cookie", async ({ page }) => {
    await signIn(page, OWNER.email);
    await expect(page).toHaveURL(/\/system$/);
    const before = await idpState(page);

    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/login$/);
    expect(await sessionCookies(page)).toHaveLength(0);
    const after = await idpState(page);
    expect(after.logouts.filter((s) => s === "global").length).toBeGreaterThan(
      before.logouts.filter((s) => s === "global").length,
    ); // the refresh token is revoked at the provider, so a copied cookie cannot be renewed

    await page.goto("/system");
    await expect(page).toHaveURL(/\/login\?next=%2Fsystem$/);
  });
});

test.describe("the API proxy for an anonymous caller", () => {
  test("answers 401 as JSON (not an HTML redirect) and is never cacheable", async ({ request }) => {
    const response = await request.get("/bff/api/v1/me");
    expect(response.status()).toBe(401);
    expect(response.headers()["cache-control"]).toContain("no-store");
    expect((await response.json()).error.code).toBe("unauthorized");
  });

  test("a valid bearer token in the request is ignored — only the server-side session counts", async ({
    request,
  }) => {
    const minted = await request.post(`${IDP_URL}/__test__/mint`, { data: { email: OWNER.email } });
    const { access_token: token } = await minted.json();

    // The token is genuine: the backend itself accepts it…
    const direct = await request.get(`${BACKEND_URL}/api/v1/me`, {
      headers: { authorization: `Bearer ${token}` },
    });
    expect(direct.status()).toBe(200);
    // …but the proxy will not let the browser supply credentials.
    const viaProxy = await request.get("/bff/api/v1/me", { headers: { authorization: `Bearer ${token}` } });
    expect(viaProxy.status()).toBe(401);
  });

  test("state-changing calls from another origin, or with no Origin, are refused before anything else happens", async ({
    request,
    baseURL,
  }) => {
    const body = { headers: { "content-type": "application/json" }, data: {} };
    const foreign = await request.post("/bff/api/v1/anything", {
      ...body,
      headers: { ...body.headers, origin: "https://evil.example" },
    });
    const missing = await request.post("/bff/api/v1/anything", body);
    expect([foreign.status(), missing.status()]).toEqual([403, 403]);
    expect((await foreign.json()).error.code).toBe("cross_origin_blocked");

    // Even the right origin gets no further than authentication when there is no session.
    const ours = await request.post("/bff/api/v1/anything", {
      ...body,
      headers: { ...body.headers, origin: baseURL! },
    });
    expect(ours.status()).toBe(401);
  });

  test("only the dashboard's API and the health probes are reachable through it", async ({ request }) => {
    expect((await request.get("/bff/healthz")).status()).toBe(200); // public probe, no session needed
    for (const path of [
      "/bff/docs",
      "/bff/openapi.json",
      "/bff/api/v1/%2e%2e/docs",
      "/bff/webhooks/gmail/pubsub",
      "/bff",
    ]) {
      expect((await request.get(path)).status(), path).toBe(404);
    }
  });
});
