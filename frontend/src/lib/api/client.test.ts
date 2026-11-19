import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";

import { createApiClient } from "./client";

const BASE = "http://app.test/bff";

describe("createApiClient", () => {
  it("is typed from the OpenAPI contract and returns parsed data", async () => {
    server.use(http.get(`${BASE}/healthz`, () => HttpResponse.json({ status: "ok", version: "1.2.3" })));
    const { data } = await createApiClient({ baseUrl: BASE }).GET("/healthz");
    expect(data?.version).toBe("1.2.3");
  });

  it("never sends credentials of its own — the session cookie is HttpOnly and the proxy adds the token", async () => {
    let seen: Headers | undefined;
    server.use(
      http.get(`${BASE}/api/v1/me`, ({ request }) => {
        seen = request.headers;
        return HttpResponse.json({
          user_id: "u",
          email: "owner@example.com",
          workspace_id: "w",
          assurance_level: "aal1",
          session_expires_at: "2026-01-01T00:00:00Z",
        });
      }),
    );
    await createApiClient({ baseUrl: BASE }).GET("/api/v1/me");
    expect(seen?.get("authorization")).toBeNull();
  });

  it("reports an expired session once per 401 so the app can return to the login page", async () => {
    server.use(
      http.get(`${BASE}/api/v1/me`, () =>
        HttpResponse.json({ error: { code: "unauthorized" } }, { status: 401 }),
      ),
    );
    const onUnauthorized = vi.fn();
    const { response } = await createApiClient({ baseUrl: BASE, onUnauthorized }).GET("/api/v1/me");
    expect(response.status).toBe(401);
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("does not treat other failures as a signed-out session", async () => {
    server.use(
      http.get(`${BASE}/api/v1/me`, () =>
        HttpResponse.json({ error: { code: "forbidden" } }, { status: 403 }),
      ),
    );
    const onUnauthorized = vi.fn();
    await createApiClient({ baseUrl: BASE, onUnauthorized }).GET("/api/v1/me");
    expect(onUnauthorized).not.toHaveBeenCalled();
  });
});
