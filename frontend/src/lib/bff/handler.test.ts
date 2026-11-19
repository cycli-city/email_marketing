import { describe, expect, it, vi, type Mock } from "vitest";

import { handleBffRequest, type BffDeps } from "./handler";

const APP = "https://app.example.com";
const BACKEND = "https://api.internal.example";

interface Harness {
  deps: BffDeps;
  fetchMock: Mock<typeof fetch>;
  token: { value: string | null };
}

function harness(
  upstream: () => Response | Promise<Response> = () => Response.json({ ok: true }),
  token: string | null = "access-token-123",
): Harness {
  const fetchMock = vi.fn<typeof fetch>(async () => upstream());
  const state = { value: token };
  return {
    fetchMock,
    token: state,
    deps: {
      appOrigin: APP,
      backendUrl: BACKEND,
      getAccessToken: async () => state.value,
      fetchImpl: fetchMock,
      requestId: () => "req-1",
    },
  };
}

const call = (h: Harness, segments: string[] | undefined, init: RequestInit & { url?: string } = {}) => {
  const { url = `${APP}/bff/${(segments ?? []).join("/")}`, ...rest } = init;
  return handleBffRequest(new Request(url, rest), segments, h.deps);
};

const post = (body = '{"a":1}', headers: Record<string, string> = {}): RequestInit => ({
  method: "POST",
  body,
  headers: {
    origin: APP,
    "content-type": "application/json",
    "content-length": String(new TextEncoder().encode(body).length),
    ...headers,
  },
});

function upstreamCall(h: Harness) {
  const [url, init = {}] = h.fetchMock.mock.calls[0];
  return { url: String(url), init, headers: new Headers(init.headers) };
}

describe("routing", () => {
  it("forwards an allowed API route to the fixed backend with the session's bearer token", async () => {
    const h = harness();
    const response = await call(h, ["api", "v1", "me"]);
    expect(response.status).toBe(200);
    const { url, headers } = upstreamCall(h);
    expect(url).toBe(`${BACKEND}/api/v1/me`);
    expect(headers.get("authorization")).toBe("Bearer access-token-123");
    expect(headers.get("x-request-id")).toBe("req-1");
  });

  it.each([[["docs"]], [["openapi.json"]], [["api", "v1", ".."]], [["api", "v1", "a%2fb"]], [undefined]])(
    "never forwards the disallowed route %j",
    async (segments) => {
      const h = harness();
      const response = await call(h, segments as string[] | undefined);
      expect(response.status).toBe(404);
      expect(h.fetchMock).not.toHaveBeenCalled();
    },
  );

  // (TRACE/CONNECT cannot even be constructed by the Fetch API, so they can never reach this handler.)
  it.each(["OPTIONS", "PROPFIND", "PURGE"])(
    "rejects the %s method before any upstream call",
    async (method) => {
      const h = harness();
      const response = await handleBffRequest(
        new Request(`${APP}/bff/api/v1/me`, { method }),
        ["api", "v1", "me"],
        h.deps,
      );
      expect(response.status).toBe(405);
      expect(h.fetchMock).not.toHaveBeenCalled();
    },
  );

  it("forwards the query string unchanged but refuses an enormous one", async () => {
    const h = harness();
    await call(h, ["api", "v1", "leads"], { url: `${APP}/bff/api/v1/leads?stage=verified&page=2` });
    expect(upstreamCall(h).url).toBe(`${BACKEND}/api/v1/leads?stage=verified&page=2`);

    const h2 = harness();
    const response = await call(h2, ["api", "v1", "leads"], {
      url: `${APP}/bff/api/v1/leads?q=${"a".repeat(3000)}`,
    });
    expect(response.status).toBe(414);
    expect(h2.fetchMock).not.toHaveBeenCalled();
  });
});

describe("authentication", () => {
  it("answers 401 itself, without calling the backend, when there is no session", async () => {
    const h = harness(undefined, null);
    const response = await call(h, ["api", "v1", "me"]);
    expect(response.status).toBe(401);
    expect(h.fetchMock).not.toHaveBeenCalled();
    expect((await response.json()).error.code).toBe("unauthorized");
  });

  it("serves the public health probes without a session and without a token", async () => {
    const h = harness(undefined, null);
    const response = await call(h, ["healthz"]);
    expect(response.status).toBe(200);
    expect(upstreamCall(h).headers.has("authorization")).toBe(false);
  });

  it("ignores any Authorization or Cookie the browser sends — the token comes only from the server-side session", async () => {
    const h = harness();
    await call(h, ["api", "v1", "me"], {
      headers: {
        authorization: "Bearer attacker-supplied",
        cookie: "__Host-aoi-session=stolen; other=1",
        "x-forwarded-for": "6.6.6.6",
        host: "evil.example",
      },
    });
    const { headers } = upstreamCall(h);
    expect(headers.get("authorization")).toBe("Bearer access-token-123");
    expect(headers.has("cookie")).toBe(false);
    expect(headers.has("x-forwarded-for")).toBe(false);
  });

  it("forwards only harmless request headers", async () => {
    const h = harness();
    await call(h, ["api", "v1", "me"], {
      headers: {
        accept: "application/json",
        "if-none-match": '"abc"',
        "x-secret": "1",
        referer: "https://x",
      },
    });
    const names = [...upstreamCall(h).headers.keys()].sort();
    expect(names).toEqual(["accept", "authorization", "if-none-match", "x-request-id"]);
  });
});

describe("CSRF: state-changing requests", () => {
  it("accepts a same-origin POST and forwards its JSON body", async () => {
    const h = harness();
    const response = await call(h, ["api", "v1", "leads"], post('{"name":"Acme"}'));
    expect(response.status).toBe(200);
    const { init, headers } = upstreamCall(h);
    expect(init.method).toBe("POST");
    expect(new TextDecoder().decode(init.body as ArrayBuffer)).toBe('{"name":"Acme"}');
    expect(headers.get("content-type")).toBe("application/json");
  });

  it.each([
    ["no Origin at all", { origin: "" }],
    ["a foreign Origin", { origin: "https://evil.example" }],
    ["an opaque Origin", { origin: "null" }],
    ["a cross-site fetch", { "sec-fetch-site": "cross-site" }],
  ] satisfies [string, Record<string, string>][])(
    "rejects a POST with %s, before touching the backend",
    async (_label, overrides: Record<string, string>) => {
      const h = harness();
      const init = post();
      const headers = { ...(init.headers as Record<string, string>), ...overrides };
      if (headers.origin === "") delete (headers as Record<string, string | undefined>).origin;
      const response = await call(h, ["api", "v1", "leads"], { ...init, headers });
      expect(response.status).toBe(403);
      expect((await response.json()).error.code).toBe("cross_origin_blocked");
      expect(h.fetchMock).not.toHaveBeenCalled();
    },
  );

  it.each(["PUT", "PATCH", "DELETE"])("applies the same origin rule to %s", async (method) => {
    const h = harness();
    const response = await call(h, ["api", "v1", "leads", "1"], {
      method,
      headers: { origin: "https://evil.example" },
    });
    expect(response.status).toBe(403);
    expect(h.fetchMock).not.toHaveBeenCalled();
  });

  it("does not demand an Origin for reads (a plain GET navigation or fetch has none)", async () => {
    const h = harness();
    expect((await call(h, ["api", "v1", "me"])).status).toBe(200);
  });
});

describe("request bodies", () => {
  it("refuses non-JSON bodies (blocks cross-site form posts that skip CORS preflight)", async () => {
    const h = harness();
    const response = await call(
      h,
      ["api", "v1", "leads"],
      post("a=1", { "content-type": "application/x-www-form-urlencoded" }),
    );
    expect(response.status).toBe(415);
    expect(h.fetchMock).not.toHaveBeenCalled();
  });

  it("refuses oversized and undeclared-length bodies", async () => {
    const h = harness();
    const huge = await call(h, ["api", "v1", "leads"], post("{}", { "content-length": "5000000" }));
    expect(huge.status).toBe(413);
    const garbage = await call(h, ["api", "v1", "leads"], post("{}", { "content-length": "lots" }));
    expect(garbage.status).toBe(413);

    const streamed = await call(h, ["api", "v1", "leads"], {
      method: "POST",
      headers: { origin: APP, "content-type": "application/json" },
      body: new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode("{}"));
          controller.close();
        },
      }),
      // @ts-expect-error duplex is required by Node for streamed request bodies
      duplex: "half",
    });
    expect(streamed.status).toBe(411);
    expect(h.fetchMock).not.toHaveBeenCalled();
  });

  it("allows an empty POST (e.g. an action endpoint) without a content type", async () => {
    const h = harness();
    const response = await call(h, ["api", "v1", "campaigns", "1", "pause"], {
      method: "POST",
      headers: { origin: APP, "content-length": "0" },
    });
    expect(response.status).toBe(200);
    expect(upstreamCall(h).init.body).toBeUndefined();
  });
});

describe("responses", () => {
  it("passes status and body through, with only safe headers and never a cache", async () => {
    const h = harness(
      () =>
        new Response('{"error":{"code":"rate_limited"}}', {
          status: 429,
          headers: {
            "content-type": "application/json",
            "retry-after": "7",
            "set-cookie": "evil=1; Domain=.example.com",
            "x-powered-by": "secret-stack",
            server: "uvicorn",
            "access-control-allow-origin": "*",
            "cache-control": "public, max-age=3600",
          },
        }),
    );
    const response = await call(h, ["api", "v1", "me"]);
    expect(response.status).toBe(429);
    expect(response.headers.get("retry-after")).toBe("7");
    expect(response.headers.get("content-type")).toBe("application/json");
    expect(response.headers.get("cache-control")).toBe("no-store");
    for (const leaked of ["set-cookie", "x-powered-by", "server", "access-control-allow-origin"]) {
      expect(response.headers.has(leaked)).toBe(false);
    }
    expect(await response.json()).toEqual({ error: { code: "rate_limited" } });
  });

  it("never follows or reveals a backend redirect (it could carry the bearer token elsewhere)", async () => {
    const h = harness(
      () => new Response(null, { status: 302, headers: { location: "https://evil.example/x" } }),
    );
    const response = await call(h, ["api", "v1", "me"]);
    expect(response.status).toBe(502);
    expect(response.headers.has("location")).toBe(false);
    expect((h.fetchMock.mock.calls[0][1] as RequestInit).redirect).toBe("manual");
  });

  it("reports an unreachable backend as 502 without leaking the error", async () => {
    const h = harness(() => {
      throw new TypeError("connect ECONNREFUSED 10.0.0.7:8000");
    });
    const response = await call(h, ["api", "v1", "me"]);
    expect(response.status).toBe(502);
    const text = await response.text();
    expect(text).toContain("backend_unreachable");
    expect(text).not.toContain("10.0.0.7");
  });

  it("does not send a body back for HEAD", async () => {
    const h = harness(() => new Response("body", { status: 200 }));
    const response = await call(h, ["api", "v1", "me"], { method: "HEAD" });
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("");
  });

  it("applies a timeout to the backend call", async () => {
    const h = harness();
    await call(h, ["api", "v1", "me"]);
    expect((h.fetchMock.mock.calls[0][1] as RequestInit).signal).toBeInstanceOf(AbortSignal);
  });
});
