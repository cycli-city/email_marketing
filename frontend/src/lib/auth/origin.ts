/**
 * CSRF check for state-changing requests to our own API proxy.
 *
 * Next.js's built-in Server Action check lets a request with **no** `Origin` header through (with a warning). For the
 * API proxy that would be a hole, so here a missing, opaque (`null`) or foreign `Origin` is rejected outright, and when
 * the browser also sends `Sec-Fetch-Site` it must say `same-origin`. (`SameSite=Lax` cookies already stop the browser
 * attaching the session to cross-site POSTs; this is the second, independent layer.)
 */
export function isTrustedOrigin(headers: Headers, appOrigin: string): boolean {
  const origin = headers.get("origin");
  if (origin === null || origin !== appOrigin) return false;
  const fetchSite = headers.get("sec-fetch-site");
  return fetchSite === null || fetchSite === "same-origin";
}
