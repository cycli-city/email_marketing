import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

// Runs as the signed-in owner against the real backend (configured with throw-away Google settings — see ci.yml).
// Google itself cannot be driven from CI, so the navigation to its consent screen is intercepted and inspected.

const GOOGLE_CONSENT = "https://accounts.google.com/**";

test.describe("Integrations page", () => {
  test("shows both modes and is reachable from the navigation", async ({ page }) => {
    await page.goto("/system");
    await page.getByRole("link", { name: "Integrations" }).click();
    await expect(page).toHaveURL(/\/settings\/integrations$/);
    await expect(page.getByRole("heading", { name: "Integrations", level: 1 })).toBeVisible();
    await expect(page.getByLabel("NEXORA Sales", { exact: true })).toBeVisible();
    await expect(page.getByLabel("Job Hunter", { exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "Integrations" })).toHaveAttribute("data-active", "");
  });

  test("has no accessibility violations (WCAG A/AA)", async ({ page }) => {
    await page.goto("/settings/integrations");
    await expect(page.getByLabel("Job Hunter", { exact: true })).toBeVisible();
    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
    expect(results.violations).toEqual([]);
  });

  test("Connect sends the browser to Google's consent screen asking for exactly the minimum, protected by PKCE", async ({
    page,
    baseURL,
  }) => {
    await page.route(GOOGLE_CONSENT, (route) =>
      route.fulfill({ status: 200, contentType: "text/html", body: "google" }),
    );
    await page.goto("/settings/integrations");
    const navigation = page.waitForRequest((r) =>
      r.url().startsWith("https://accounts.google.com/o/oauth2/v2/auth"),
    );
    await page.getByRole("button", { name: /connect a gmail mailbox for nexora sales/i }).click();

    const url = new URL((await navigation).url());
    const q = url.searchParams;
    expect(q.get("response_type")).toBe("code");
    expect(q.get("redirect_uri")).toBe(`${baseURL}/integrations/gmail/callback`);
    expect(q.get("access_type")).toBe("offline");
    expect(q.get("prompt")).toBe("consent");
    expect(q.get("code_challenge_method")).toBe("S256");
    expect(q.get("code_challenge")).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(q.get("scope")?.split(" ").sort()).toEqual([
      "email",
      "https://www.googleapis.com/auth/gmail.readonly",
      "https://www.googleapis.com/auth/gmail.send",
      "openid",
    ]);
    // The state is opaque ciphertext: it reveals neither the mode nor any id, and the secret never travels in the URL.
    const state = q.get("state") ?? "";
    expect(state.length).toBeGreaterThan(100);
    expect(state).not.toMatch(/nexora|job_hunter/i);
    expect(url.search).not.toMatch(/client_secret|code_verifier/i);
  });
});

test.describe("the Google callback", () => {
  test("a forged connection attempt is refused with a clear message and nothing is connected", async ({
    page,
  }) => {
    await page.goto("/integrations/gmail/callback?code=attackers-code&state=forged-state");
    await expect(page).toHaveURL(/\/settings\/integrations\?error=state$/);
    await expect(page.getByRole("status")).toContainText(/expired, or it was not started from this session/i);
    await expect(page.getByText("No mailbox connected yet.")).toHaveCount(2);
  });

  test("declining on Google's screen is reported as such", async ({ page }) => {
    await page.goto("/integrations/gmail/callback?error=access_denied&state=anything");
    await expect(page).toHaveURL(/\/settings\/integrations\?error=denied$/);
    await expect(page.getByRole("status")).toContainText("You chose not to allow access");
  });

  test("an incomplete callback is refused", async ({ page }) => {
    await page.goto("/integrations/gmail/callback");
    await expect(page).toHaveURL(/\/settings\/integrations\?error=invalid$/);
  });

  test("text in the address bar can only select a known message, never appear on the page", async ({
    page,
  }) => {
    const hostile = "<img src=x onerror=window.__pwned=true>";
    await page.goto(
      `/settings/integrations?error=${encodeURIComponent(hostile)}&connected=${encodeURIComponent(hostile)}`,
    );
    await expect(page.getByRole("status")).toHaveText(
      "Something went wrong while connecting. Please try again.",
    );
    await expect(page.locator("img[src='x']")).toHaveCount(0);
    expect(await page.evaluate(() => (window as unknown as { __pwned?: boolean }).__pwned)).toBeUndefined();
  });

  test("the response is never cached and does not leak the one-time code through the Referer", async ({
    request,
  }) => {
    const response = await request.get("/integrations/gmail/callback?code=abc&state=forged", {
      maxRedirects: 0,
    });
    expect(response.status()).toBe(303);
    expect(response.headers()["cache-control"]).toBe("no-store");
    expect(response.headers()["referrer-policy"]).toBe("no-referrer");
    expect(response.headers()["location"]).not.toMatch(/abc|forged/);
  });
});
