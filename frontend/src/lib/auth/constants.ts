/**
 * The session cookie name. The `__Host-` prefix makes the *browser* enforce `Secure`, `Path=/` and no `Domain`:
 * the cookie cannot be planted or overwritten by a sibling subdomain or a plain-HTTP origin.
 */
export const SESSION_COOKIE = "__Host-aoi-session";

/** Sliding lifetime: refreshed whenever the session is refreshed, so an idle browser is signed out after a week. */
export const SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7;

export const LOGIN_PATH = "/login";

/** Google sends the browser here with a one-time authorization code in the query string (ADR-034). */
export const GMAIL_CALLBACK_PATH = "/integrations/gmail/callback";
export const INTEGRATIONS_PATH = "/settings/integrations";

/** Where the browser reaches the backend: always through our own server, never directly (ADR-014). */
export const BFF_BASE = "/bff";
