/**
 * Public (browser-safe) configuration. Only NEXT_PUBLIC_* values may ever appear here — anything else
 * would be a secret and must stay on the backend (CI scans the built bundle to enforce this).
 */
export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");
export const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL ?? "";

/** Origins the browser is allowed to talk to (used by the Content-Security-Policy `connect-src`). */
export function connectOrigins(): string[] {
  const origins = new Set<string>();
  for (const value of [API_URL, SUPABASE_URL]) {
    if (!value) continue;
    try {
      origins.add(new URL(value).origin);
    } catch {
      // An invalid URL simply contributes nothing; the policy stays restrictive.
    }
  }
  return [...origins];
}
