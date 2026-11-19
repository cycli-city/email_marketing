import "server-only";

import { createServerClient } from "@supabase/ssr";
import type { SupabaseClient } from "@supabase/supabase-js";

import { SESSION_COOKIE } from "./constants";
import { hardenCookieOptions, type HardenedCookieOptions } from "./cookies";
import { authEnv } from "./server-env";

export interface CookieToWrite {
  name: string;
  value: string;
  options: HardenedCookieOptions;
}

/** Where the Supabase client reads the session from and writes refreshed/cleared sessions to. */
export interface CookieJar {
  getAll(): { name: string; value: string }[];
  /** `headers` carries `Cache-Control: no-store` etc. that must accompany any response that sets auth cookies. */
  setAll(cookies: CookieToWrite[], headers: Record<string, string>): void;
}

/**
 * The ONLY way this application creates a Supabase client, and it is server-side only: there is no browser client, so
 * no access or refresh token ever exists in page JavaScript (ADR-014).
 *
 * Every cookie the library wants to write goes through {@link hardenCookieOptions}, so a library default or a future
 * library change can never produce a script-readable or long-lived session cookie.
 */
export function createSupabase(jar: CookieJar): SupabaseClient {
  const env = authEnv();
  return createServerClient(env.supabaseUrl, env.supabaseAnonKey, {
    cookieOptions: { name: SESSION_COOKIE, ...hardenCookieOptions() },
    cookies: {
      getAll: () => jar.getAll(),
      setAll: (cookies, headers) =>
        jar.setAll(
          cookies.map(({ name, value, options }) => ({
            name,
            value,
            options: hardenCookieOptions(options),
          })),
          headers,
        ),
    },
  });
}
