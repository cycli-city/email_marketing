import { expect, test } from "@playwright/test";

import { OWNER } from "./support/constants";

// These run as the signed-in owner (their session cookie comes from auth.setup.ts).
test.describe("the API proxy for the signed-in owner", () => {
  test("forwards the owner's request with the server-side token and returns the backend's answer", async ({
    request,
  }) => {
    const response = await request.get("/bff/api/v1/me");
    expect(response.status()).toBe(200);
    expect(response.headers()["cache-control"]).toBe("no-store");
    const me = await response.json();
    expect(me.email).toBe(OWNER.email);
    expect(me.assurance_level).toBe("aal1");
    expect(me.user_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(me.workspace_id).toMatch(/^[0-9a-f-]{36}$/);
  });

  test("passes the backend's own errors through (here: a route that does not exist)", async ({
    request,
    baseURL,
  }) => {
    const response = await request.post("/bff/api/v1/no-such-route", {
      headers: { origin: baseURL!, "content-type": "application/json" },
      data: {},
    });
    expect(response.status()).toBe(404);
    expect((await response.json()).error.code).toBe("not_found");
  });

  test("never hands the backend's headers or cookies back to the browser", async ({ request }) => {
    const headers = (await request.get("/bff/api/v1/me")).headers();
    for (const leaked of ["set-cookie", "server", "x-powered-by", "access-control-allow-origin"]) {
      expect(headers[leaked], leaked).toBeUndefined();
    }
  });

  test("the browser-side client works through the proxy (the system page reads live backend health)", async ({
    page,
  }) => {
    const requests: string[] = [];
    page.on("request", (r) => requests.push(new URL(r.url()).origin));
    await page.goto("/system");
    await expect(page.getByText("All systems operational")).toBeVisible();
    // The page talked to its own origin only — it never contacted the backend or the identity provider.
    expect(new Set(requests)).toEqual(new Set(["http://localhost:3000"]));
  });
});
