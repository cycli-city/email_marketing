import "server-only";

import { z } from "zod";

import { authEnv } from "./server-env";

const meSchema = z.object({
  user_id: z.string(),
  email: z.string(),
  workspace_id: z.string(),
  assurance_level: z.string(),
  session_expires_at: z.string(),
});

export interface Owner {
  userId: string;
  email: string;
  workspaceId: string;
  assuranceLevel: string;
  sessionExpiresAt: string;
}

/** What the backend said about a session. Distinguishes "sign in again" from "this isn't you" from "try later". */
export type MeResult =
  | { status: "ok"; owner: Owner }
  | { status: "unauthenticated" }
  | { status: "forbidden" }
  | { status: "mfa_required" }
  | { status: "unavailable" };

const TIMEOUT_MS = 10_000;

async function errorCode(response: Response): Promise<string | null> {
  try {
    const body: unknown = await response.json();
    const code = (body as { error?: { code?: unknown } })?.error?.code;
    return typeof code === "string" ? code : null;
  } catch {
    return null;
  }
}

/**
 * Asks the backend — the authority — who this access token belongs to. The backend verifies the signature, checks the
 * owner allow-list and (if enabled) the second factor; the dashboard never decides that for itself.
 */
export async function fetchMe(accessToken: string, fetchImpl: typeof fetch = fetch): Promise<MeResult> {
  let response: Response;
  try {
    response = await fetchImpl(`${authEnv().backendUrl}/api/v1/me`, {
      headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
      cache: "no-store",
      redirect: "manual", // a redirect could carry the bearer token to another host
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch {
    return { status: "unavailable" };
  }

  if (response.status === 200) {
    try {
      const me = meSchema.parse(await response.json());
      return {
        status: "ok",
        owner: {
          userId: me.user_id,
          email: me.email,
          workspaceId: me.workspace_id,
          assuranceLevel: me.assurance_level,
          sessionExpiresAt: me.session_expires_at,
        },
      };
    } catch {
      return { status: "unavailable" };
    }
  }
  if (response.status === 401) return { status: "unauthenticated" };
  if (response.status === 403) {
    return (await errorCode(response)) === "mfa_required"
      ? { status: "mfa_required" }
      : { status: "forbidden" };
  }
  return { status: "unavailable" };
}
