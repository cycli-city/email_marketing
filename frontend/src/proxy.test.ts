import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { refreshSession, type SessionRefresh } from "@/lib/auth/proxy-session";

import { config, proxy } from "./proxy";

vi.mock("@/lib/auth/proxy-session", () => ({ refreshSession: vi.fn() }));

const ANONYMOUS: SessionRefresh = { authenticated: false, cookies: [], headers: {} };
const SIGNED_IN: SessionRefresh = { authenticated: true, cookies: [], headers: {} };

const request = (path = "/system") => new NextRequest(`http://localhost:3000${path}`);

function nonceOf(csp: string): string {
  const match = /'nonce-([^']+)'/.exec(csp);
  if (!match) throw new Error("no nonce in CSP");
  return match[1];
}

beforeEach(() => {
  vi.mocked(refreshSession).mockResolvedValue(SIGNED_IN);
});

describe("proxy: Content-Security-Policy nonce", () => {
  it("sets a CSP with a nonce on every response, including the login page", async () => {
    for (const path of ["/system", "/login"]) {
      const csp = (await proxy(request(path))).headers.get("content-security-policy");
      expect(csp).toContain("script-src");
      expect(csp).toContain("'strict-dynamic'");
      expect(csp).toMatch(/'nonce-[A-Za-z0-9+/=]+'/);
    }
  });

  it("uses a different nonce for every request (a reusable nonce would defeat CSP)", async () => {
    const a = nonceOf((await proxy(request())).headers.get("content-security-policy")!);
    const b = nonceOf((await proxy(request())).headers.get("content-security-policy")!);
    expect(a).not.toBe(b);
  });

  it("forwards the same nonce to Next.js through the request headers", async () => {
    const response = await proxy(request());
    const csp = response.headers.get("content-security-policy")!;
    // NextResponse.next({request:{headers}}) exposes overridden request headers as x-middleware-request-*
    expect(response.headers.get("x-middleware-request-x-nonce")).toBe(nonceOf(csp));
    expect(response.headers.get("x-middleware-request-content-security-policy")).toBe(csp);
  });

  it("only allows the browser to talk to its own origin (the API is reached through the proxy)", async () => {
    const csp = (await proxy(request())).headers.get("content-security-policy")!;
    expect(csp).toMatch(/connect-src 'self'(;|$)/);
  });

  it("puts the CSP on redirects too", async () => {
    vi.mocked(refreshSession).mockResolvedValue(ANONYMOUS);
    const response = await proxy(request("/system"));
    expect(response.status).toBe(307);
    expect(response.headers.get("content-security-policy")).toContain("'strict-dynamic'");
  });
});

describe("proxy: session gate", () => {
  it("sends a visitor without a session to the login page and remembers where they were going", async () => {
    vi.mocked(refreshSession).mockResolvedValue(ANONYMOUS);
    const response = await proxy(request("/leads?stage=verified"));
    expect(response.status).toBe(307);
    const location = new URL(response.headers.get("location")!);
    expect(location.pathname).toBe("/login");
    expect(location.searchParams.get("next")).toBe("/leads?stage=verified");
  });

  it("does not add a next= for the dashboard root", async () => {
    vi.mocked(refreshSession).mockResolvedValue(ANONYMOUS);
    const location = new URL((await proxy(request("/"))).headers.get("location")!);
    expect(location.search).toBe("");
  });

  it("never checks the session for the login page itself", async () => {
    vi.mocked(refreshSession).mockResolvedValue(ANONYMOUS);
    const response = await proxy(request("/login"));
    expect(response.status).toBe(200);
    expect(refreshSession).not.toHaveBeenCalled();
  });

  it("lets the API proxy answer for itself (a JSON 401, not an HTML redirect) but still refreshes the session", async () => {
    vi.mocked(refreshSession).mockResolvedValue(ANONYMOUS);
    const response = await proxy(request("/bff/api/v1/me"));
    expect(response.status).toBe(200);
    expect(response.headers.get("location")).toBeNull();
    expect(refreshSession).toHaveBeenCalledOnce();
  });

  it("lets a signed-in visitor through without a redirect", async () => {
    const response = await proxy(request("/system"));
    expect(response.status).toBe(200);
    expect(response.headers.get("location")).toBeNull();
  });
});

describe("proxy: session cookies", () => {
  it("writes refreshed session cookies, with their hardened options and the no-store headers, onto the response", async () => {
    vi.mocked(refreshSession).mockResolvedValue({
      authenticated: true,
      cookies: [
        {
          name: "__Host-aoi-session",
          value: "refreshed-value",
          options: { httpOnly: true, secure: true, sameSite: "lax", path: "/", maxAge: 604800 },
        },
      ],
      headers: { "Cache-Control": "private, no-cache, no-store, must-revalidate, max-age=0" },
    });
    const response = await proxy(request("/system"));
    const cookie = response.cookies.get("__Host-aoi-session");
    expect(cookie?.value).toBe("refreshed-value");
    const header = response.headers.get("set-cookie")!;
    expect(header).toMatch(/HttpOnly/i);
    expect(header).toMatch(/Secure/i);
    expect(header).toMatch(/SameSite=lax/i);
    expect(header).toMatch(/Path=\//);
    expect(response.headers.get("cache-control")).toContain("no-store");
  });

  it("carries cleared cookies onto the login redirect so a dead session is removed", async () => {
    vi.mocked(refreshSession).mockResolvedValue({
      authenticated: false,
      cookies: [
        {
          name: "__Host-aoi-session",
          value: "",
          options: { httpOnly: true, secure: true, sameSite: "lax", path: "/", maxAge: 0 },
        },
      ],
      headers: {},
    });
    const response = await proxy(request("/system"));
    expect(response.status).toBe(307);
    expect(response.headers.get("set-cookie")).toMatch(/__Host-aoi-session=;|Max-Age=0/i);
  });
});

describe("proxy: matcher", () => {
  it("is not applied to static assets or prefetches", () => {
    const [matcher] = config.matcher;
    expect(matcher.source).toContain("_next/static");
    expect(matcher.source).toContain("_next/image");
    expect(matcher.missing).toEqual(
      expect.arrayContaining([expect.objectContaining({ key: "next-router-prefetch" })]),
    );
  });
});
