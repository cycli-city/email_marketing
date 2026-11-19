import { isTrustedOrigin } from "@/lib/auth/origin";

import { resolveBackendTarget } from "./path";

/**
 * The browser's only route to the backend (ADR-014). The session lives in an HttpOnly cookie that page JavaScript cannot
 * read; this handler reads it on the server and attaches the access token as a bearer header on the way through.
 *
 * It is a narrow, fixed-destination gate, not a general proxy:
 * - only `/api/v1/*` and the health probes (see `resolveBackendTarget`); the host is fixed by configuration;
 * - the browser can NOT supply its own `Authorization` or `Cookie` — only an allow-list of harmless headers is forwarded;
 * - state-changing methods require a same-origin `Origin` (and `Sec-Fetch-Site`) — a missing header is rejected;
 * - bodies must be JSON with a declared, bounded length; queries and responses are bounded; upstream redirects are never
 *   followed (they could carry the bearer token to another host) and upstream cookies are never passed back.
 */

export interface BffDeps {
  /** The dashboard's own origin (scheme + host + port), as browsers send it in `Origin`. */
  appOrigin: string;
  /** The backend base URL, without a trailing slash. */
  backendUrl: string;
  /** The verified access token for the current session, or null. */
  getAccessToken(): Promise<string | null>;
  fetchImpl?: typeof fetch;
  requestId?(): string;
}

const MAX_BODY_BYTES = 1_000_000;
const MAX_QUERY_CHARS = 2_048;
const UPSTREAM_TIMEOUT_MS = 30_000;
const STATE_CHANGING = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const ALLOWED_METHODS = new Set(["GET", "HEAD", ...STATE_CHANGING]);
const FORWARDED_REQUEST_HEADERS = ["accept", "accept-language", "content-type", "if-none-match"];
const FORWARDED_RESPONSE_HEADERS = [
  "content-type",
  "etag",
  "retry-after",
  "x-request-id",
  "ratelimit-limit",
  "ratelimit-remaining",
];

function problem(status: number, code: string, message: string): Response {
  return Response.json(
    { error: { code, message, details: {}, request_id: null } },
    { status, headers: { "Cache-Control": "no-store" } },
  );
}

export async function handleBffRequest(
  request: Request,
  segments: string[] | undefined,
  deps: BffDeps,
): Promise<Response> {
  const target = resolveBackendTarget(segments);
  if (!target) return problem(404, "not_found", "Not found");

  const method = request.method.toUpperCase();
  if (!ALLOWED_METHODS.has(method)) return problem(405, "method_not_allowed", "Method not allowed");

  if (STATE_CHANGING.has(method) && !isTrustedOrigin(request.headers, deps.appOrigin)) {
    return problem(403, "cross_origin_blocked", "Cross-origin request blocked");
  }

  const search = new URL(request.url).search;
  if (search.length > MAX_QUERY_CHARS) return problem(414, "uri_too_long", "Query string too long");

  const headers = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  headers.set("X-Request-ID", deps.requestId?.() ?? crypto.randomUUID().replaceAll("-", ""));

  if (!target.public) {
    const token = await deps.getAccessToken();
    if (!token) return problem(401, "unauthorized", "Authentication required");
    headers.set("Authorization", `Bearer ${token}`);
  }

  let body: ArrayBuffer | undefined;
  if (STATE_CHANGING.has(method)) {
    const declared = request.headers.get("content-length");
    if (declared === null) {
      if (request.body !== null) return problem(411, "length_required", "Content-Length is required");
    } else {
      const length = Number(declared);
      if (!Number.isInteger(length) || length < 0 || length > MAX_BODY_BYTES) {
        return problem(413, "payload_too_large", "Request body too large");
      }
      if (length > 0) {
        if (!/^application\/json\s*(;|$)/i.test(request.headers.get("content-type") ?? "")) {
          return problem(415, "unsupported_media_type", "Only application/json bodies are accepted");
        }
        body = await request.arrayBuffer();
        if (body.byteLength > MAX_BODY_BYTES)
          return problem(413, "payload_too_large", "Request body too large");
      }
    }
  }

  let upstream: Response;
  try {
    upstream = await (deps.fetchImpl ?? fetch)(`${deps.backendUrl}/${target.path}${search}`, {
      method,
      headers,
      body,
      redirect: "manual",
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
  } catch {
    return problem(502, "backend_unreachable", "The backend is not reachable");
  }
  if (upstream.status >= 300 && upstream.status < 400) {
    return problem(502, "backend_redirected", "The backend answered with an unexpected redirect");
  }

  const responseHeaders = new Headers({ "Cache-Control": "no-store" });
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  return new Response(method === "HEAD" ? null : upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}
