import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { server } from "@/test/server";

import { SystemStatus } from "./system-status";

const BASE = "http://api.test";

function renderStatus(children?: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      {children ?? <SystemStatus api={createApiClient(undefined, BASE)} />}
    </QueryClientProvider>,
  );
}

const live = http.get(`${BASE}/healthz`, () => HttpResponse.json({ status: "ok", version: "0.1.0" }));

describe("<SystemStatus />", () => {
  it("shows a loading state, then healthy components", async () => {
    server.use(
      live,
      http.get(`${BASE}/readyz`, () => HttpResponse.json({ status: "ready", checks: { database: "ok" } })),
    );
    renderStatus();

    expect(screen.getByLabelText(/loading system status/i)).toBeInTheDocument();
    expect(await screen.findByText("All systems operational")).toBeInTheDocument();
    expect(screen.getByText(/API v0\.1\.0/)).toBeInTheDocument();
    expect(screen.getAllByText("Healthy")).toHaveLength(1); // database
    expect(screen.getByText("Up")).toBeInTheDocument(); // API
    expect(screen.queryByText("Redis")).not.toBeInTheDocument(); // ADR-024
  });

  it("flags the failing dependency when the backend is degraded", async () => {
    server.use(
      live,
      http.get(`${BASE}/readyz`, () =>
        HttpResponse.json({ status: "degraded", checks: { database: "error" } }, { status: 503 }),
      ),
    );
    renderStatus();

    expect(await screen.findByText(/degraded — a dependency is failing/i)).toBeInTheDocument();
    expect(screen.getAllByText("Failing")).toHaveLength(1);
    expect(screen.queryByText("Healthy")).not.toBeInTheDocument();
  });

  it("explains what to check when the backend is unreachable", async () => {
    server.use(http.get(`${BASE}/healthz`, () => HttpResponse.error()));
    renderStatus();

    expect(await screen.findByText("Backend unreachable")).toBeInTheDocument();
    expect(screen.getByText(/NEXT_PUBLIC_API_URL/)).toBeInTheDocument();
  });

  it("Refresh button re-queries the backend", async () => {
    let calls = 0;
    server.use(
      http.get(`${BASE}/healthz`, () => {
        calls += 1;
        return HttpResponse.json({ status: "ok", version: "0.1.0" });
      }),
      http.get(`${BASE}/readyz`, () => HttpResponse.json({ status: "ready", checks: { database: "ok" } })),
    );
    renderStatus();
    await screen.findByText("All systems operational");
    expect(calls).toBe(1);

    await userEvent.click(screen.getByRole("button", { name: /refresh/i }));
    await waitFor(() => expect(calls).toBe(2));
  });
});
