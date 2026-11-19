import type { Metadata } from "next";

import { SystemStatus } from "@/components/system-status";

export const metadata: Metadata = { title: "System" };

export default function SystemPage() {
  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">System status</h1>
        <p className="text-muted-foreground text-sm">
          Live health of the backend API and its dependencies. This page will grow into the full System
          Operations console (providers, queues, activity log) as those features land.
        </p>
      </div>
      <SystemStatus />
    </div>
  );
}
