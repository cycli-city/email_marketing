import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { createApiClient } from "./api/client";
import { fetchBackendHealth } from "./health";

const BASE = "http://api.test";
const api = () => createApiClient({ baseUrl: BASE });
const fixedNow = () => new Date("2026-01-01T00:00:00Z");

const live = http.get(`${BASE}/healthz`, () => HttpResponse.json({ status: "ok", version: "0.1.0" }));

describe("fetchBackendHealth", () => {
  it("reports ready when every dependency is healthy", async () => {
    server.use(
      live,
      http.get(`${BASE}/readyz`, () => HttpResponse.json({ status: "ready", checks: { database: "ok" } })),
    );
    expect(await fetchBackendHealth(api(), fixedNow)).toEqual({
      api: "up",
      version: "0.1.0",
      database: "ok",
      overall: "ready",
      checkedAt: fixedNow(),
    });
  });

  it("treats a 503 readiness response as 'degraded' with per-component detail, not as an exception", async () => {
    server.use(
      live,
      http.get(`${BASE}/readyz`, () =>
        HttpResponse.json({ status: "degraded", checks: { database: "error" } }, { status: 503 }),
      ),
    );
    const health = await fetchBackendHealth(api(), fixedNow);
    expect(health.overall).toBe("degraded");
    expect(health.database).toBe("error");
    expect(health.api).toBe("up");
  });

  it("reports unreachable when the network request itself fails", async () => {
    server.use(http.get(`${BASE}/healthz`, () => HttpResponse.error()));
    const health = await fetchBackendHealth(api(), fixedNow);
    expect(health.overall).toBe("unreachable");
    expect(health.api).toBe("unreachable");
    expect(health.version).toBeNull();
  });

  it("reports unreachable when liveness returns a server error", async () => {
    server.use(http.get(`${BASE}/healthz`, () => HttpResponse.json({ error: {} }, { status: 500 })));
    expect((await fetchBackendHealth(api(), fixedNow)).overall).toBe("unreachable");
  });

  it("reports degraded (API up) when readiness returns an unexpected body", async () => {
    server.use(
      live,
      http.get(`${BASE}/readyz`, () => HttpResponse.json({ error: { code: "x" } }, { status: 500 })),
    );
    const health = await fetchBackendHealth(api(), fixedNow);
    expect(health.api).toBe("up");
    expect(health.overall).toBe("degraded");
  });
});
