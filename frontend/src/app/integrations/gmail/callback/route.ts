import { NextResponse } from "next/server";

import { authEnv } from "@/lib/auth/server-env";
import { getAccessToken } from "@/lib/auth/session";
import { completeGmailConnection } from "@/lib/gmail/callback";

// Never cached or prerendered: it consumes a one-time code on behalf of the signed-in owner.
export const dynamic = "force-dynamic";

/**
 * Where Google sends the browser after the owner approves (or refuses) access. The registered redirect address is this
 * dashboard route — not the API — so the owner's session cookie arrives with the request (it is `SameSite=Lax`, which is
 * sent on this top-level navigation) and the backend can tie the connection to the signed-in user.
 */
export async function GET(request: Request): Promise<Response> {
  let env;
  try {
    env = authEnv();
  } catch {
    return new Response("The dashboard is not configured", {
      status: 503,
      headers: { "Cache-Control": "no-store" },
    });
  }
  const path = await completeGmailConnection(request.url, { backendUrl: env.backendUrl, getAccessToken });
  const response = NextResponse.redirect(new URL(path, env.appOrigin), 303);
  response.headers.set("Cache-Control", "no-store");
  return response;
}
