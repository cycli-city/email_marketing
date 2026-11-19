import "server-only";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { cache } from "react";

import { fetchMe, type MeResult, type Owner } from "./backend";
import { LOGIN_PATH } from "./constants";
import { createSupabase } from "./supabase";

/**
 * A Supabase client bound to the current request's cookies — for Server Components, Server Actions and Route Handlers.
 *
 * Server Components cannot write cookies, so a write there is ignored on purpose: the proxy (src/proxy.ts) refreshes
 * the session before any page renders, and Server Actions / Route Handlers *can* write.
 */
export async function serverSupabase() {
  const store = await cookies();
  return createSupabase({
    getAll: () => store.getAll().map(({ name, value }) => ({ name, value })),
    setAll: (list) => {
      try {
        for (const { name, value, options } of list) store.set(name, value, options);
      } catch {
        // Called while rendering a Server Component: cookies are read-only there.
      }
    },
  });
}

/**
 * The current access token, or `null`. `getClaims()` first: it verifies the token's signature against the project's
 * published keys and refreshes the session when it is about to expire — so a token returned here is a verified one.
 */
export async function getAccessToken(): Promise<string | null> {
  const supabase = await serverSupabase();
  const { data: verified } = await supabase.auth.getClaims();
  if (!verified?.claims) return null;
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

/** Who the backend says the current session belongs to. Memoised per request, so layouts and pages share one call. */
export const getOwnerState = cache(async (): Promise<MeResult> => {
  const token = await getAccessToken();
  return token ? fetchMe(token) : { status: "unauthenticated" };
});

/**
 * Data-access-layer guard for pages: the real authorisation check, close to the data (the proxy only does a quick,
 * optimistic redirect). Anything other than "you are the owner" leaves the page.
 */
export async function requireOwner(): Promise<Owner | { unavailable: true }> {
  const state = await getOwnerState();
  switch (state.status) {
    case "ok":
      return state.owner;
    case "unavailable":
      return { unavailable: true };
    case "mfa_required":
      return redirect(`${LOGIN_PATH}?error=mfa_required`);
    case "forbidden":
      return redirect(`${LOGIN_PATH}?error=forbidden`);
    default:
      return redirect(`${LOGIN_PATH}?error=session`);
  }
}
