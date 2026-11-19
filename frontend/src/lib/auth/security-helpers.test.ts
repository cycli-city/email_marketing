import { describe, expect, it } from "vitest";

import { resolveBackendTarget } from "@/lib/bff/path";

import { SESSION_COOKIE, SESSION_MAX_AGE_SECONDS } from "./constants";
import { hardenCookieOptions } from "./cookies";
import { isTrustedOrigin } from "./origin";
import { safeNextPath } from "./redirects";

describe("hardenCookieOptions", () => {
  it("always emits HttpOnly + Secure + SameSite=Lax + Path=/ and a bounded lifetime", () => {
    expect(hardenCookieOptions({ maxAge: 400 * 24 * 3600 })).toEqual({
      httpOnly: true,
      secure: true,
      sameSite: "lax",
      path: "/",
      maxAge: SESSION_MAX_AGE_SECONDS,
    });
  });

  it("discards everything the library asked for — a weaker request cannot weaken the cookie", () => {
    const result = hardenCookieOptions({
      httpOnly: false,
      secure: false,
      sameSite: "none",
      domain: ".example.com",
      path: "/somewhere",
      maxAge: 10 ** 9,
    });
    expect(result).toMatchObject({ httpOnly: true, secure: true, sameSite: "lax", path: "/" });
    expect(result).not.toHaveProperty("domain");
    expect(result.maxAge).toBe(SESSION_MAX_AGE_SECONDS);
  });

  it("keeps deletions as deletions", () => {
    expect(hardenCookieOptions({ maxAge: 0 }).maxAge).toBe(0);
    expect(hardenCookieOptions({ expires: new Date(1000) }, 5000).maxAge).toBe(0);
    expect(hardenCookieOptions({ expires: new Date(9000) }, 5000).maxAge).toBe(SESSION_MAX_AGE_SECONDS);
  });

  it("uses the __Host- prefix so the browser itself refuses insecure or sub-domain-planted copies", () => {
    expect(SESSION_COOKIE.startsWith("__Host-")).toBe(true);
  });
});

describe("safeNextPath", () => {
  it.each([
    ["/system", "/system"],
    ["/leads?stage=verified&page=2", "/leads?stage=verified&page=2"],
    ["/a/b/c", "/a/b/c"],
  ])("accepts the same-site path %s", (input, expected) => {
    expect(safeNextPath(input)).toBe(expected);
  });

  it.each([
    "//evil.example",
    "//evil.example/path",
    "/\\evil.example",
    "\\\\evil.example",
    "https://evil.example",
    "http://evil.example/",
    "java" + "script:alert(1)",
    "data:text/html,<script>1</script>",
    "evil.example",
    "/\tevil.example",
    "/\nevil.example",
    "/\revil.example",
    "",
    "   ",
    "/" + "a".repeat(600),
  ])("falls back to the dashboard for the unsafe value %j", (input) => {
    expect(safeNextPath(input)).toBe("/");
  });

  it("keeps URL-encoded control characters inert (they are never decoded into a header injection)", () => {
    const result = safeNextPath("/%0d%0aSet-Cookie:x=1");
    expect(result).toBe("/%0d%0aSet-Cookie:x=1");
    expect(result).not.toMatch(/[\r\n]/);
  });

  it("never loops back to the login page or into the API proxy", () => {
    expect(safeNextPath("/login")).toBe("/");
    expect(safeNextPath("/login?next=/x")).toBe("/");
    expect(safeNextPath("/bff/api/v1/me")).toBe("/");
  });

  it("handles null and undefined and honours a custom fallback", () => {
    expect(safeNextPath(null)).toBe("/");
    expect(safeNextPath(undefined, "/system")).toBe("/system");
  });
});

describe("isTrustedOrigin", () => {
  const APP = "https://app.example.com";
  const headers = (init: Record<string, string>) => new Headers(init);

  it("accepts only the app's own origin", () => {
    expect(isTrustedOrigin(headers({ origin: APP }), APP)).toBe(true);
    expect(isTrustedOrigin(headers({ origin: APP, "sec-fetch-site": "same-origin" }), APP)).toBe(true);
  });

  it("rejects a missing Origin (the gap in Next's own Server Action check)", () => {
    expect(isTrustedOrigin(headers({}), APP)).toBe(false);
  });

  it.each([
    "https://evil.example",
    "null",
    "http://app.example.com", // same host, wrong scheme
    "https://app.example.com.evil.example",
    "https://app.example.com:8443",
    "https://sub.app.example.com",
  ])("rejects the foreign origin %s", (origin) => {
    expect(isTrustedOrigin(headers({ origin }), APP)).toBe(false);
  });

  it.each(["cross-site", "same-site", "none"])(
    "rejects Sec-Fetch-Site: %s even with a matching Origin",
    (site) => {
      expect(isTrustedOrigin(headers({ origin: APP, "sec-fetch-site": site }), APP)).toBe(false);
    },
  );
});

describe("resolveBackendTarget", () => {
  it("allows /api/v1/* and the two public probes", () => {
    expect(resolveBackendTarget(["api", "v1", "me"])).toEqual({ path: "api/v1/me", public: false });
    expect(resolveBackendTarget(["api", "v1", "leads", "3f2a-uuid", "approve"])).toEqual({
      path: "api/v1/leads/3f2a-uuid/approve",
      public: false,
    });
    expect(resolveBackendTarget(["healthz"])).toEqual({ path: "healthz", public: true });
    expect(resolveBackendTarget(["readyz"])).toEqual({ path: "readyz", public: true });
  });

  it.each([
    [undefined],
    [[]],
    [["docs"]],
    [["openapi.json"]],
    [["webhooks", "gmail", "pubsub"]],
    [["u", "token"]], // public unsubscribe endpoint is not for the dashboard to proxy
    [["api", "v1"]], // no resource
    [["api", "v2", "me"]],
    [["API", "v1", "me"]],
    [["healthz", "extra"]],
  ])("refuses the route %j that is not part of the dashboard API", (segments) => {
    expect(resolveBackendTarget(segments as string[] | undefined)).toBeNull();
  });

  it.each([
    [["api", "v1", ".."]],
    [["api", "v1", ".", "me"]],
    [["api", "v1", "..", "..", "etc"]],
    [["api", "v1", "a/b"]],
    [["api", "v1", "a\\b"]],
    [["api", "v1", "%2e%2e"]],
    [["api", "v1", "me%00"]],
    [["api", "v1", "me?x=1"]],
    [["api", "v1", "me#frag"]],
    [["api", "v1", "http://evil.example"]],
    [["api", "v1", "x".repeat(200)]],
    [["api", "v1", ""]],
    [["api", "v1", "café"]],
    [Array.from({ length: 20 }, () => "a")],
  ])("refuses a traversal / smuggling attempt %j", (segments) => {
    expect(resolveBackendTarget(segments as string[])).toBeNull();
  });
});
