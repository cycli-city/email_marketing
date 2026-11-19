import { Radar } from "lucide-react";
import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { safeNextPath } from "@/lib/auth/redirects";
import { authEnv } from "@/lib/auth/server-env";
import { getOwnerState } from "@/lib/auth/session";

import { LoginForm } from "./login-form";

export const metadata: Metadata = { title: "Sign in" };

// Fixed messages keyed by a short code: the URL can select a message, never inject text into the page.
const NOTICES: Record<string, string> = {
  session: "Your session has ended. Please sign in again.",
  forbidden: "This account is not authorised to use this dashboard.",
  mfa_required:
    "Two-factor authentication is required for this account, but it is not set up in the dashboard yet.",
};

function single(value: string | string[] | undefined): string | undefined {
  return typeof value === "string" ? value : undefined;
}

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const next = safeNextPath(single(params.next));
  const code = single(params.error);
  const notice = code ? (NOTICES[code] ?? null) : null;

  let configured = true;
  try {
    authEnv();
  } catch {
    configured = false;
  }

  // Already signed in as the owner → straight to where they were going. (Skipped when we were sent here with an
  // error, which avoids any possibility of a redirect loop.)
  if (configured && !code) {
    const state = await getOwnerState();
    if (state.status === "ok") redirect(next);
  }

  return (
    <main className="bg-muted/30 flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="items-center text-center">
          <div className="bg-primary text-primary-foreground mb-2 flex size-10 items-center justify-center rounded-lg">
            <Radar className="size-5" />
          </div>
          <CardTitle>
            <h1 className="text-xl">AI Outreach Intelligence</h1>
          </CardTitle>
          <CardDescription>Sign in to continue</CardDescription>
        </CardHeader>
        <CardContent>
          {configured ? (
            <LoginForm next={next} notice={notice} />
          ) : (
            <p role="alert" className="text-destructive text-sm">
              Sign-in is not configured on this server. Check SUPABASE_URL, SUPABASE_ANON_KEY, BACKEND_URL and
              APP_URL.
            </p>
          )}
        </CardContent>
      </Card>
    </main>
  );
}
