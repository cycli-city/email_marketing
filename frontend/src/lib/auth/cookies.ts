import { SESSION_MAX_AGE_SECONDS } from "./constants";

export interface HardenedCookieOptions {
  httpOnly: true;
  secure: true;
  sameSite: "lax";
  path: "/";
  maxAge: number;
}

interface IncomingOptions {
  maxAge?: number;
  expires?: Date;
  [key: string]: unknown;
}

/**
 * The only cookie attributes this application will ever emit for the session.
 *
 * The Supabase helper's own defaults are NOT safe for us (`httpOnly: false`, 400-day lifetime), so every cookie it
 * wants to write passes through here and everything it asked for is discarded except whether this is a *deletion*:
 *
 * - `HttpOnly`  — page JavaScript (and therefore any injected script) can never read the tokens.
 * - `Secure`    — never sent over plain HTTP (browsers also accept it on http://localhost for development).
 * - `SameSite=Lax` — not sent on cross-site sub-requests/POSTs (CSRF), but kept on top-level link clicks so a
 *   Telegram/e-mail deep link into the dashboard keeps you signed in. State-changing routes additionally verify `Origin`.
 * - `Path=/`, no `Domain` — host-only, as the `__Host-` prefix requires.
 */
export function hardenCookieOptions(
  incoming?: IncomingOptions,
  now: number = Date.now(),
): HardenedCookieOptions {
  const removing =
    incoming?.maxAge === 0 || (incoming?.expires instanceof Date && incoming.expires.getTime() <= now);
  return {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/",
    maxAge: removing ? 0 : SESSION_MAX_AGE_SECONDS,
  };
}
