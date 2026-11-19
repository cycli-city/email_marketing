// Shared by the end-to-end specs. These describe the FAKE identity provider in fake-idp.mjs (test-only values).
export const IDP_PORT = process.env.FAKE_IDP_PORT ?? "54321";
export const IDP_URL = `http://127.0.0.1:${IDP_PORT}`;
export const BACKEND_URL = process.env.E2E_BACKEND_URL ?? "http://127.0.0.1:8000";

// Not a secret: the password of accounts that exist only inside the throw-away fake identity provider.
export const PASSWORD = "e2e-correct-horse-battery";

export const OWNER = { email: "owner@example.com" };
/** A valid account that is NOT on the backend's owner allow-list. */
export const INTRUDER = { email: "intruder@example.com" };
/** An owner whose access tokens live 95 seconds (just over the client's 90 s refresh margin), to exercise session refresh. */
export const SHORT_LIVED = { email: "shortlived@example.com" };

export const SESSION_COOKIE_PREFIX = "__Host-aoi-session";
export const OWNER_STATE = "e2e/.auth/owner.json";
