import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { config, proxy } from "./proxy";

const request = (path = "/system") => new NextRequest(`http://localhost:3000${path}`);

function nonceOf(csp: string): string {
  const match = /'nonce-([^']+)'/.exec(csp);
  if (!match) throw new Error("no nonce in CSP");
  return match[1];
}

describe("proxy (CSP nonce)", () => {
  it("sets a Content-Security-Policy with a nonce on every response", () => {
    const csp = proxy(request()).headers.get("content-security-policy");
    expect(csp).toContain("script-src");
    expect(csp).toContain("'strict-dynamic'");
    expect(csp).toMatch(/'nonce-[A-Za-z0-9+/=]+'/);
  });

  it("uses a different nonce for every request (a reusable nonce would defeat CSP)", () => {
    const a = nonceOf(proxy(request()).headers.get("content-security-policy")!);
    const b = nonceOf(proxy(request()).headers.get("content-security-policy")!);
    expect(a).not.toBe(b);
  });

  it("forwards the same nonce to Next.js through the request headers", () => {
    const response = proxy(request());
    const csp = response.headers.get("content-security-policy")!;
    // NextResponse.next({request:{headers}}) exposes overridden request headers as x-middleware-request-*
    expect(response.headers.get("x-middleware-request-x-nonce")).toBe(nonceOf(csp));
    expect(response.headers.get("x-middleware-request-content-security-policy")).toBe(csp);
  });

  it("is not applied to static assets or prefetches", () => {
    const [matcher] = config.matcher;
    expect(matcher.source).toContain("_next/static");
    expect(matcher.source).toContain("_next/image");
    expect(matcher.missing).toEqual(
      expect.arrayContaining([expect.objectContaining({ key: "next-router-prefetch" })]),
    );
  });
});
