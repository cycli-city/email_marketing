import type { ApiClient } from "@/lib/api/client";

export type ComponentState = "ok" | "error" | "unknown";

export interface BackendHealth {
  /** `unreachable` = the request itself failed (network/CORS/backend down). */
  api: "up" | "unreachable";
  version: string | null;
  database: ComponentState;
  redis: ComponentState;
  overall: "ready" | "degraded" | "unreachable";
  checkedAt: Date;
}

/**
 * Reads the backend's liveness + readiness probes. A 503 from /readyz is a *valid answer*
 * ("degraded", with per-component detail), not an exception.
 */
export async function fetchBackendHealth(
  api: ApiClient,
  now: () => Date = () => new Date(),
): Promise<BackendHealth> {
  try {
    const live = await api.GET("/healthz");
    if (!live.data) {
      return unreachable(now());
    }
    const ready = await api.GET("/readyz");
    const body = ready.data ?? ready.error;
    if (!body || !("checks" in body)) {
      return { ...unreachable(now()), api: "up", version: live.data.version, overall: "degraded" };
    }
    return {
      api: "up",
      version: live.data.version,
      database: body.checks.database ?? "unknown",
      redis: body.checks.redis ?? "unknown",
      overall: body.status,
      checkedAt: now(),
    };
  } catch {
    return unreachable(now());
  }
}

function unreachable(checkedAt: Date): BackendHealth {
  return {
    api: "unreachable",
    version: null,
    database: "unknown",
    redis: "unknown",
    overall: "unreachable",
    checkedAt,
  };
}
