import { describe, expect, it } from "vitest";

import { buildCsp, generateNonce } from "./csp";

/** Parse "a b c; d e" into { a: ["b","c"], d: ["e"] }. */
function parse(csp: string): Record<string, string[]> {
  return Object.fromEntries(
    csp.split(";").map((part) => {
      const [name, ...values] = part.trim().split(/\s+/);
      return [name, values];
    }),
  );
}

const prod = (nonce = "abc123") =>
  parse(buildCsp({ nonce, isDev: false, connectOrigins: ["https://api.example.com"] }));

describe("buildCsp (production)", () => {
  it("allows scripts only via nonce + strict-dynamic — never unsafe-inline or unsafe-eval", () => {
    const script = prod("N0NC3")["script-src"];
    expect(script).toEqual(["'self'", "'nonce-N0NC3'", "'strict-dynamic'"]);
    expect(script).not.toContain("'unsafe-inline'");
    expect(script).not.toContain("'unsafe-eval'");
  });

  it("styles need the nonce; only style ATTRIBUTES may be inline", () => {
    const csp = prod("N0NC3");
    expect(csp["style-src"]).toEqual(["'self'", "'nonce-N0NC3'"]);
    expect(csp["style-src-attr"]).toEqual(["'unsafe-inline'"]);
  });

  it("restricts network access to self + the configured API origins (blocks data exfiltration)", () => {
    expect(prod()["connect-src"]).toEqual(["'self'", "https://api.example.com"]);
  });

  it("blocks framing, plugins, <base> hijacking, foreign form posts and nested frames", () => {
    const csp = prod();
    expect(csp["frame-ancestors"]).toEqual(["'none'"]);
    expect(csp["object-src"]).toEqual(["'none'"]);
    expect(csp["frame-src"]).toEqual(["'none'"]);
    expect(csp["base-uri"]).toEqual(["'self'"]);
    expect(csp["form-action"]).toEqual(["'self'"]);
    expect(csp["default-src"]).toEqual(["'self'"]);
  });

  it("forces HTTPS for any sub-resource", () => {
    expect(prod()).toHaveProperty("upgrade-insecure-requests");
  });

  it("never contains a wildcard source or a plain-http origin", () => {
    const flat = buildCsp({ nonce: "n", isDev: false, connectOrigins: ["https://api.example.com"] });
    expect(flat).not.toMatch(/(^|\s)\*(\s|;|$)/);
    expect(flat).not.toMatch(/http:\/\//);
  });
});

describe("buildCsp (development)", () => {
  const dev = parse(buildCsp({ nonce: "n", isDev: true, connectOrigins: ["http://localhost:8000"] }));

  it("adds only the dev-tooling allowances (eval for React debugging, websockets for HMR)", () => {
    expect(dev["script-src"]).toContain("'unsafe-eval'");
    expect(dev["connect-src"]).toEqual(expect.arrayContaining(["http://localhost:8000", "ws://localhost:*"]));
  });

  it("does not upgrade to https (would break http://localhost)", () => {
    expect(dev).not.toHaveProperty("upgrade-insecure-requests");
  });

  it("still forbids framing and objects", () => {
    expect(dev["frame-ancestors"]).toEqual(["'none'"]);
    expect(dev["object-src"]).toEqual(["'none'"]);
  });
});

describe("generateNonce", () => {
  it("is unpredictable: unique across many calls", () => {
    const nonces = new Set(Array.from({ length: 500 }, generateNonce));
    expect(nonces.size).toBe(500);
  });

  it("is valid base64 with plenty of entropy (>= 40 chars)", () => {
    const nonce = generateNonce();
    expect(nonce).toMatch(/^[A-Za-z0-9+/]+=*$/);
    expect(nonce.length).toBeGreaterThanOrEqual(40);
  });
});
