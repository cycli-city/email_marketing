import "server-only";

import type { NextRequest } from "next/server";

import { createSupabase, type CookieToWrite } from "./supabase";

export interface SessionRefresh {
  /** True only if the request carries a session whose token signature verified against the project's keys. */
  authenticated: boolean;
  /** Cookies the Supabase client wants to write (a refreshed session, or a cleared one). Already hardened. */
  cookies: CookieToWrite[];
  /** Headers (`Cache-Control: no-store`…) that must accompany those cookies. */
  headers: Record<string, string>;
}

/**
 * Verifies and, if needed, refreshes the session for the proxy. Never throws: any failure (misconfiguration, Supabase
 * unreachable) is reported as "not authenticated", so the application fails closed.
 *
 * The refreshed cookies are also applied to the *request* so the page that renders next sees the new session instead
 * of refreshing again with a refresh token that has just been rotated.
 */
export async function refreshSession(request: NextRequest): Promise<SessionRefresh> {
  const cookies: CookieToWrite[] = [];
  const headers: Record<string, string> = {};
  try {
    const supabase = createSupabase({
      getAll: () => request.cookies.getAll().map(({ name, value }) => ({ name, value })),
      setAll: (list, extra) => {
        for (const cookie of list) {
          cookies.push(cookie);
          if (cookie.options.maxAge === 0) request.cookies.delete(cookie.name);
          else request.cookies.set(cookie.name, cookie.value);
        }
        Object.assign(headers, extra);
      },
    });
    const { data } = await supabase.auth.getClaims();
    return { authenticated: Boolean(data?.claims), cookies, headers };
  } catch (error) {
    console.error("auth.session_check_failed", error instanceof Error ? error.name : "unknown");
    return { authenticated: false, cookies, headers };
  }
}
