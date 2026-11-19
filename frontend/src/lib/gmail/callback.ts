import { LOGIN_PATH } from "@/lib/auth/constants";

export const INTEGRATIONS_PATH = "/settings/integrations";

export interface CallbackDeps {
  /** Backend base URL without a trailing slash. */
  backendUrl: string;
  /** The verified access token of the signed-in owner, or null. */
  getAccessToken(): Promise<string | null>;
  fetchImpl?: typeof fetch;
}

const MAX_CODE_CHARS = 2_048;
const MAX_STATE_CHARS = 4_096;
const TIMEOUT_MS = 30_000;
const MODES = new Set(["nexora", "job_hunter"]);

// The backend's error codes → the short, fixed codes the Settings page knows how to explain. Anything else (including
// any text from Google or the backend) is never copied into the redirect: the URL can only ever select a known message.
const ERROR_CODES: Record<string, string> = {
  oauth_state_invalid: "state",
  oauth_exchange_failed: "exchange",
  gmail_scopes_missing: "scopes",
  gmail_no_refresh_token: "no_refresh",
  mailbox_already_connected: "already_connected",
  integration_not_configured: "not_configured",
  google_unavailable: "unavailable",
};

const failure = (code: string) => `${INTEGRATIONS_PATH}?error=${code}`;

/**
 * Finishes the Gmail connection when Google sends the browser back with `?code=…&state=…`, and returns the dashboard
 * path to redirect to. Runs on the server with the owner's session, which is what lets the backend verify that the person
 * finishing the flow is the one who started it (the state is bound to the user).
 */
export async function completeGmailConnection(requestUrl: string, deps: CallbackDeps): Promise<string> {
  const params = new URL(requestUrl).searchParams;

  const googleError = params.get("error");
  if (googleError) return failure(googleError === "access_denied" ? "denied" : "google");

  const code = params.get("code");
  const state = params.get("state");
  if (!code || !state || code.length > MAX_CODE_CHARS || state.length > MAX_STATE_CHARS)
    return failure("invalid");

  const token = await deps.getAccessToken();
  if (!token) return `${LOGIN_PATH}?next=${encodeURIComponent(INTEGRATIONS_PATH)}`; // session ended: sign in, then retry

  let response: Response;
  try {
    response = await (deps.fetchImpl ?? fetch)(`${deps.backendUrl}/api/v1/integrations/gmail/exchange`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify({ code, state }),
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch {
    return failure("unavailable");
  }

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // an unreadable body is handled by the status below
  }

  if (response.status === 200) {
    const mode = (body as { mode?: unknown } | null)?.mode;
    return `${INTEGRATIONS_PATH}?connected=${typeof mode === "string" && MODES.has(mode) ? mode : "ok"}`;
  }
  const code_ = (body as { error?: { code?: unknown } } | null)?.error?.code;
  return failure((typeof code_ === "string" && ERROR_CODES[code_]) || "failed");
}
