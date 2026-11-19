import { NextResponse, type NextRequest } from "next/server";

import { buildCsp, generateNonce } from "@/lib/csp";
import { connectOrigins } from "@/lib/env";

/**
 * Runs before every page render: creates a fresh nonce, builds the Content-Security-Policy around it and
 * hands the nonce to Next.js (which stamps it onto its own scripts/styles) via the request headers.
 * Pages must render dynamically for the nonce to be applied — see `dynamic = "force-dynamic"` in the root layout.
 */
export function proxy(request: NextRequest) {
  const nonce = generateNonce();
  const csp = buildCsp({
    nonce,
    isDev: process.env.NODE_ENV === "development",
    connectOrigins: connectOrigins(),
  });

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
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
