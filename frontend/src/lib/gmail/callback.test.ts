import { describe, expect, it, vi } from "vitest";

import { completeGmailConnection, type CallbackDeps } from "./callback";

const BACKEND = "https://api.internal.example";
const CALLBACK = "https://app.example.com/integrations/gmail/callback";

function deps(
  respond: () => Response | Promise<Response> = () => Response.json({ mode: "job_hunter" }),
  token: string | null = "owner-token",
) {
  const fetchMock = vi.fn<typeof fetch>(async () => respond());
  const value: CallbackDeps = {
    backendUrl: BACKEND,
    getAccessToken: async () => token,
    fetchImpl: fetchMock,
  };
  return { value, fetchMock };
}

describe("completeGmailConnection", () => {
  it("hands the code and state to the backend with the owner's token and reports the connected mode", async () => {
    const { value, fetchMock } = deps();
    const path = await completeGmailConnection(`${CALLBACK}?code=the-code&state=the-state`, value);
    expect(path).toBe("/settings/integrations?connected=job_hunter");

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe(`${BACKEND}/api/v1/integrations/gmail/exchange`);
    expect(init?.method).toBe("POST");
    expect(new Headers(init?.headers).get("authorization")).toBe("Bearer owner-token");
    expect(JSON.parse(String(init?.body))).toEqual({ code: "the-code", state: "the-state" });
    expect(init?.redirect).toBe("manual");
  });

  it("does not contact the backend when the owner declined on Google's screen", async () => {
    const { value, fetchMock } = deps();
    expect(await completeGmailConnection(`${CALLBACK}?error=access_denied&state=s`, value)).toBe(
      "/settings/integrations?error=denied",
    );
    expect(await completeGmailConnection(`${CALLBACK}?error=server_error`, value)).toBe(
      "/settings/integrations?error=google",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    ["no code", "?state=s"],
    ["no state", "?code=c"],
    ["nothing", ""],
    ["an enormous code", `?code=${"x".repeat(3000)}&state=s`],
    ["an enormous state", `?code=c&state=${"s".repeat(5000)}`],
  ])("rejects %s without touching the backend", async (_label, query) => {
    const { value, fetchMock } = deps();
    expect(await completeGmailConnection(`${CALLBACK}${query}`, value)).toBe(
      "/settings/integrations?error=invalid",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sends a signed-out visitor to sign in first — without carrying the one-time code around", async () => {
    const { value, fetchMock } = deps(undefined, null);
    const path = await completeGmailConnection(`${CALLBACK}?code=SECRET-CODE&state=SECRET-STATE`, value);
    expect(path).toBe("/login?next=%2Fsettings%2Fintegrations");
    expect(path).not.toContain("SECRET");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    ["oauth_state_invalid", 400, "state"],
    ["oauth_exchange_failed", 400, "exchange"],
    ["gmail_scopes_missing", 422, "scopes"],
    ["gmail_no_refresh_token", 422, "no_refresh"],
    ["mailbox_already_connected", 409, "already_connected"],
    ["integration_not_configured", 503, "not_configured"],
    ["google_unavailable", 503, "unavailable"],
    ["something_new_we_do_not_know", 500, "failed"],
  ])("maps the backend error %s to the fixed code %s", async (code, status, expected) => {
    const { value } = deps(() => Response.json({ error: { code } }, { status }));
    expect(await completeGmailConnection(`${CALLBACK}?code=c&state=s`, value)).toBe(
      `/settings/integrations?error=${expected}`,
    );
  });

  it("never reflects text from Google or the backend into the redirect (it can only choose a known message)", async () => {
    const hostile = "<script>alert(1)</script>&error=injected";
    const { value } = deps(() =>
      Response.json({ error: { code: hostile, message: hostile } }, { status: 400 }),
    );
    const path = await completeGmailConnection(`${CALLBACK}?code=c&state=s`, value);
    expect(path).toBe("/settings/integrations?error=failed");
    const connected = deps(() => Response.json({ mode: hostile }));
    expect(await completeGmailConnection(`${CALLBACK}?code=c&state=s`, connected.value)).toBe(
      "/settings/integrations?connected=ok",
    );
  });

  it("reports an unreachable or garbled backend as unavailable / failed", async () => {
    const down = deps(() => {
      throw new TypeError("connect ECONNREFUSED 10.0.0.7:8000");
    });
    const path = await completeGmailConnection(`${CALLBACK}?code=c&state=s`, down.value);
    expect(path).toBe("/settings/integrations?error=unavailable");
    expect(path).not.toContain("10.0.0.7");

    const garbled = deps(() => new Response("<html>bad gateway</html>", { status: 502 }));
    expect(await completeGmailConnection(`${CALLBACK}?code=c&state=s`, garbled.value)).toBe(
      "/settings/integrations?error=failed",
    );
  });
});
