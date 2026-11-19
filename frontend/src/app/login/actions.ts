"use server";

import { redirect } from "next/navigation";
import { z } from "zod";

import { fetchMe, type MeResult } from "@/lib/auth/backend";
import { LOGIN_PATH } from "@/lib/auth/constants";
import { safeNextPath } from "@/lib/auth/redirects";
import { serverSupabase } from "@/lib/auth/session";

export interface LoginState {
  error: string | null;
}

const credentials = z.object({
  email: z.email().max(254),
  password: z.string().min(1).max(256),
  next: z.string().max(512).optional(),
});

// One message for every credential failure: the form never reveals whether an address exists.
const BAD_CREDENTIALS = "Invalid email or password.";
const UNAVAILABLE = "Sign-in is temporarily unavailable. Please try again in a moment.";

const REFUSALS: Record<Exclude<MeResult["status"], "ok">, string> = {
  unauthenticated: BAD_CREDENTIALS,
  forbidden: "This account is not authorised to use this dashboard.",
  mfa_required:
    "Two-factor authentication is required for this account, but it is not set up in the dashboard yet.",
  unavailable: UNAVAILABLE,
};

/**
 * Password sign-in. The Supabase session is written to HttpOnly cookies by the server-side client (never exposed to
 * the page), and it is only *kept* if the backend — the authority — confirms the account is the owner.
 *
 * Server Actions are public POST endpoints, so everything is validated here; Next.js additionally compares `Origin` with
 * the host before running one.
 */
export async function signIn(_previous: LoginState, formData: FormData): Promise<LoginState> {
  const parsed = credentials.safeParse({
    email: String(formData.get("email") ?? "")
      .trim()
      .toLowerCase(),
    password: formData.get("password"),
    next: typeof formData.get("next") === "string" ? formData.get("next") : undefined,
  });
  if (!parsed.success) return { error: "Enter your email address and password." };
  const { email, password, next } = parsed.data;

  const supabase = await serverSupabase();
  const { data, error } = await supabase.auth.signInWithPassword({ email, password });
  if (error || !data.session) {
    if (error?.status === 429)
      return { error: "Too many attempts. Please wait a few minutes and try again." };
    if (error?.name === "AuthRetryableFetchError") return { error: UNAVAILABLE };
    return { error: BAD_CREDENTIALS };
  }

  const me = await fetchMe(data.session.access_token);
  if (me.status !== "ok") {
    await supabase.auth.signOut({ scope: "local" }); // do not keep a session for an account that may not use the app
    return { error: REFUSALS[me.status] };
  }

  redirect(safeNextPath(next)); // outside any try/catch: redirect() works by throwing
}

/** Ends the session everywhere: the refresh token is revoked at Supabase, so a copied cookie is worthless. */
export async function signOut(): Promise<void> {
  const supabase = await serverSupabase();
  await supabase.auth.signOut({ scope: "global" });
  redirect(LOGIN_PATH);
}
