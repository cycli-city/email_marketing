import { describe, expect, it } from "vitest";

import { AuthConfigError, parseAuthEnv } from "./server-env";

const GOOD = {
  SUPABASE_URL: "https://abc.supabase.co/",
  SUPABASE_ANON_KEY: "a-publishable-key-value",
  BACKEND_URL: "https://api.example.com//",
  APP_URL: "https://app.example.com/dashboard/",
};

describe("parseAuthEnv", () => {
  it("normalises URLs and derives the origin browsers send", () => {
    const env = parseAuthEnv(GOOD);
    expect(env.supabaseUrl).toBe("https://abc.supabase.co");
    expect(env.backendUrl).toBe("https://api.example.com");
    expect(env.appOrigin).toBe("https://app.example.com");
  });

  it("allows plain http only to loopback hosts (local development and CI)", () => {
    for (const host of ["http://localhost:8000", "http://127.0.0.1:54321", "http://[::1]:3000"]) {
      expect(() => parseAuthEnv({ ...GOOD, BACKEND_URL: host })).not.toThrow();
    }
  });

  it.each([
    "http://api.example.com",
    "http://localhost.evil.example",
    "http://10.0.0.5:8000",
    "ftp://api.example.com",
    "java" + "script:alert(1)",
    "not a url",
    "",
  ])("refuses the backend address %j", (value) => {
    expect(() => parseAuthEnv({ ...GOOD, BACKEND_URL: value })).toThrow(AuthConfigError);
  });

  it("reports which settings are wrong without ever echoing their values", () => {
    try {
      parseAuthEnv({
        SUPABASE_URL: "http://insecure.example",
        SUPABASE_ANON_KEY: "tiny-key",
        BACKEND_URL: undefined,
        APP_URL: "https://app.example.com",
      });
      expect.unreachable("should have thrown");
    } catch (error) {
      expect(error).toBeInstanceOf(AuthConfigError);
      const message = (error as Error).message;
      expect(message).toContain("SUPABASE_URL");
      expect(message).toContain("BACKEND_URL");
      expect(message).toContain("SUPABASE_ANON_KEY");
      expect(message).not.toContain("insecure.example");
      expect(message).not.toContain("tiny-key");
    }
  });

  it("requires every setting", () => {
    for (const key of Object.keys(GOOD)) {
      const partial: Record<string, string | undefined> = { ...GOOD, [key]: undefined };
      expect(() => parseAuthEnv(partial)).toThrow(AuthConfigError);
    }
  });
});
