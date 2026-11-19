import { NextResponse, type NextRequest } from "next/server";

import { LOGIN_PATH } from "@/lib/auth/constants";
import { refreshSession, type SessionRefresh } from "@/lib/auth/proxy-session";
import { loginReturnTarget } from "@/lib/auth/redirects";
import { buildCsp, generateNonce } from "@/lib/csp";

/**
 * Runs before every page render and API-proxy call:
 *
 * 1. creates a fresh nonce and builds the Content-Security-Policy around it (handed to Next.js via request headers);
 * 2. verifies / refreshes the Supabase session and writes the refreshed cookies (hardened) onto the response;
 * 3. sends visitors without a valid session to /login (an *optimistic* check — the real authorisation happens in the
 *    pages and the backend, because the proxy also sees prefetches and must stay cheap; see Next's authentication guide).
 *
 * The browser only ever talks to this origin (`connect-src 'self'`), so there are no external origins to allow.
 * Pages must render dynamically for the nonce to apply — see `dynamic = "force-dynamic"` in the root layout.
 */
export async function proxy(request: NextRequest) {
  const nonce = generateNonce();
  const csp = buildCsp({ nonce, isDev: process.env.NODE_ENV === "development", connectOrigins: [] });
  const { pathname } = request.nextUrl;

  const withCsp = (response: NextResponse) => {
    response.headers.set("Content-Security-Policy", csp);
    return response;
  };
  // Built after the session refresh so the page sees any cookie the refresh rewrote on the request.
  const passThrough = () => {
    const headers = new Headers(request.headers);
    headers.set("x-nonce", nonce);
    headers.set("Content-Security-Policy", csp);
    return NextResponse.next({ request: { headers } });
  };

  if (pathname === LOGIN_PATH) return withCsp(passThrough());

  const session = await refreshSession(request);
  const isApiProxy = pathname === "/bff" || pathname.startsWith("/bff/");

  if (!session.authenticated && !isApiProxy) {
    const login = request.nextUrl.clone();
    const target = loginReturnTarget(pathname, request.nextUrl.search);
    login.pathname = LOGIN_PATH;
    login.search = "";
    if (target !== "/") login.searchParams.set("next", target);
    return withCsp(carrySession(NextResponse.redirect(login), session));
  }
  // The API proxy answers its own 401 (JSON) rather than redirecting a fetch() to an HTML page.
  return withCsp(carrySession(passThrough(), session));
}

function carrySession(response: NextResponse, session: SessionRefresh): NextResponse {
  for (const { name, value, options } of session.cookies) response.cookies.set(name, value, options);
  for (const [key, value] of Object.entries(session.headers)) response.headers.set(key, value);
  return response;
}

export const config = {
  matcher: [
    {
      // Everything except static assets; prefetches don't render and don't need the header.
      source: "/((?!_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
