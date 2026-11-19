export interface BackendTarget {
  /** Path on the backend, without a leading slash (e.g. `api/v1/me`). */
  path: string;
  /** True for unauthenticated probes the dashboard's status page uses. */
  public: boolean;
}

const SEGMENT = /^[A-Za-z0-9._~-]{1,128}$/;
const PUBLIC_PROBES = new Set(["healthz", "readyz"]);
const MAX_SEGMENTS = 12;

/**
 * Maps the proxy's catch-all segments to a backend path — or `null` if it is not one we are willing to forward.
 *
 * The proxy is **not** a general-purpose forwarder: only `/api/v1/*` and the two health probes are reachable, every
 * segment is a plain token (no `/`, `\`, `%`, `:` or `.`/`..` traversal), so a crafted URL can never reach another
 * backend route, another host, or climb out of the allowed prefix.
 */
export function resolveBackendTarget(segments: string[] | undefined): BackendTarget | null {
  if (!segments || segments.length === 0 || segments.length > MAX_SEGMENTS) return null;
  if (!segments.every((s) => SEGMENT.test(s) && s !== "." && s !== "..")) return null;
  const path = segments.join("/");
  if (segments.length === 1 && PUBLIC_PROBES.has(segments[0])) return { path, public: true };
  if (segments[0] === "api" && segments[1] === "v1" && segments.length >= 3) {
    return { path, public: false };
  }
  return null;
}
