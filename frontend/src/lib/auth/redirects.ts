import { GMAIL_CALLBACK_PATH, INTEGRATIONS_PATH, LOGIN_PATH } from "./constants";

const MAX_LENGTH = 512;
const PLACEHOLDER_ORIGIN = "http://placeholder.invalid";

function hasControlOrBackslash(value: string): boolean {
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i);
    if (code <= 0x1f || code === 0x7f || code === 0x5c) return true; // control chars and "\" (browsers treat it as "/")
  }
  return false;
}

/**
 * Where to send the user after signing in. The value comes from the URL (`?next=`), i.e. from an attacker, so only a
 * plain same-site path is accepted; anything that could leave the site (`//evil.example`, `/\evil.example`,
 * `https://…`, `javascript:…`, control characters) falls back to the dashboard. Never redirects back to the login
 * page or into the API proxy.
 */
export function safeNextPath(raw: string | null | undefined, fallback = "/"): string {
  if (!raw || raw.length > MAX_LENGTH) return fallback;
  if (!raw.startsWith("/") || raw.startsWith("//") || hasControlOrBackslash(raw)) return fallback;
  let url: URL;
  try {
    url = new URL(raw, PLACEHOLDER_ORIGIN);
  } catch {
    return fallback;
  }
  if (url.origin !== PLACEHOLDER_ORIGIN) return fallback;
  if (url.pathname === LOGIN_PATH || url.pathname.startsWith("/bff")) return fallback;
  return `${url.pathname}${url.search}`;
}

/**
 * What to remember for "after sign-in" when a signed-out visitor asks for a page. Normally the full path and query —
 * except for Google's OAuth callback: its query holds a one-time authorization code and the `state`, which must not be
 * copied into the login page's URL (address bar, history, logs, Referer). The code is useless without the server-held
 * PKCE verifier, and a signed-out owner has to start the connection again anyway, so they are sent to the
 * integrations page instead.
 */
export function loginReturnTarget(pathname: string, search: string): string {
  if (pathname === GMAIL_CALLBACK_PATH) return INTEGRATIONS_PATH;
  return `${pathname}${search}`;
}
