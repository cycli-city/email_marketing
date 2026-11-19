import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { createApiClient } from "./client";

const BASE = "http://api.test";

describe("createApiClient", () => {
  it("attaches the bearer token when a provider is given", async () => {
    let seen: string | null = null;
    server.use(
      http.get(`${BASE}/healthz`, ({ request }) => {
        seen = request.headers.get("authorization");
        return HttpResponse.json({ status: "ok", version: "1.2.3" });
      }),
    );

    const api = createApiClient(async () => "tok_abc", BASE);
    const { data } = await api.GET("/healthz");

    expect(seen).toBe("Bearer tok_abc");
    expect(data?.version).toBe("1.2.3");
  });

  it("supports synchronous token providers", async () => {
    let seen: string | null = null;
    server.use(
      http.get(`${BASE}/healthz`, ({ request }) => {
        seen = request.headers.get("authorization");
        return HttpResponse.json({ status: "ok", version: "1" });
      }),
    );
    await createApiClient(() => "sync-token", BASE).GET("/healthz");
    expect(seen).toBe("Bearer sync-token");
  });

  it("sends no Authorization header when the provider yields no token", async () => {
    let seen: string | null | undefined;
    server.use(
      http.get(`${BASE}/healthz`, ({ request }) => {
        seen = request.headers.get("authorization");
        return HttpResponse.json({ status: "ok", version: "1" });
      }),
    );
    await createApiClient(async () => null, BASE).GET("/healthz");
    expect(seen).toBeNull();
  });

  it("sends no Authorization header when no provider is configured", async () => {
    let seen: string | null | undefined;
    server.use(
      http.get(`${BASE}/healthz`, ({ request }) => {
        seen = request.headers.get("authorization");
        return HttpResponse.json({ status: "ok", version: "1" });
      }),
    );
    await createApiClient(undefined, BASE).GET("/healthz");
    expect(seen).toBeNull();
  });
});
