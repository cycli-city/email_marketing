import "server-only";

import { z } from "zod";

/**
 * Server-only configuration for authentication and the API proxy. None of these are `NEXT_PUBLIC_*`: the browser
 * never learns the Supabase address, the publishable key or the backend address — it only talks to this server.
 *
 * Every URL must be HTTPS. The one exception is a loopback address (localhost / 127.0.0.1 / ::1) over plain HTTP,
 * which cannot be reached by a network attacker and is what local development and the CI end-to-end run use.
 */
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

function isAcceptableUrl(value: string): boolean {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return false;
  }
  if (url.protocol === "https:") return true;
  return url.protocol === "http:" && LOOPBACK_HOSTS.has(url.hostname);
}

const urlField = z
  .string()
  .min(1, "is required")
  .refine(isAcceptableUrl, "must be an https:// URL (http:// is allowed only for localhost)")
  .transform((value) => value.replace(/\/+$/, ""));

const schema = z.object({
  SUPABASE_URL: urlField,
  // The *publishable/anon* key. It is not a secret (Row-Level Security makes it harmless) but there is no reason
  // for the browser to have it either, so it stays server-side.
  SUPABASE_ANON_KEY: z.string().min(16, "is required"),
  BACKEND_URL: urlField,
  // The public origin of this dashboard, e.g. https://app.example.com — what a legitimate `Origin` header looks like.
  APP_URL: urlField,
});

export interface AuthEnv {
  supabaseUrl: string;
  supabaseAnonKey: string;
  backendUrl: string;
  appUrl: string;
  /** `APP_URL` reduced to scheme + host + port, as browsers send it in the `Origin` header. */
  appOrigin: string;
}

export class AuthConfigError extends Error {
  constructor(public readonly problems: string[]) {
    // Names only — never echo a configured value (it may be sensitive).
    super(`Authentication is not configured correctly: ${problems.join("; ")}`);
    this.name = "AuthConfigError";
  }
}

let cached: AuthEnv | null = null;

export function parseAuthEnv(source: Record<string, string | undefined>): AuthEnv {
  const result = schema.safeParse({
    SUPABASE_URL: source.SUPABASE_URL,
    SUPABASE_ANON_KEY: source.SUPABASE_ANON_KEY,
    BACKEND_URL: source.BACKEND_URL,
    APP_URL: source.APP_URL,
  });
  if (!result.success) {
    throw new AuthConfigError(result.error.issues.map((issue) => `${issue.path.join(".")} ${issue.message}`));
  }
  const env = result.data;
  return {
    supabaseUrl: env.SUPABASE_URL,
    supabaseAnonKey: env.SUPABASE_ANON_KEY,
    backendUrl: env.BACKEND_URL,
    appUrl: env.APP_URL,
    appOrigin: new URL(env.APP_URL).origin,
  };
}

export function authEnv(): AuthEnv {
  cached ??= parseAuthEnv(process.env);
  return cached;
}

export function resetAuthEnvCacheForTests(): void {
  cached = null;
}
