"use client";

import { useQuery } from "@tanstack/react-query";
import {
  CheckCircle2,
  Database,
  HelpCircle,
  RefreshCw,
  Server,
  XCircle,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { useMemo } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { createApiClient, type ApiClient } from "@/lib/api/client";
import { fetchBackendHealth, type ComponentState } from "@/lib/health";

const REFRESH_MS = 15_000;

function StateBadge({ state }: { state: ComponentState | "up" | "unreachable" }) {
  if (state === "ok" || state === "up") {
    return (
      <Badge variant="secondary" className="gap-1">
        <CheckCircle2 className="size-3 text-emerald-600 dark:text-emerald-400" />
        {state === "up" ? "Up" : "Healthy"}
      </Badge>
    );
  }
  if (state === "error" || state === "unreachable") {
    return (
      <Badge variant="destructive" className="gap-1">
        <XCircle className="size-3" />
        {state === "unreachable" ? "Unreachable" : "Failing"}
      </Badge>
    );
  }
  return (
    <Badge variant="outline" className="gap-1">
      <HelpCircle className="size-3" />
      Unknown
    </Badge>
  );
}

function StatusCard({
  icon: Icon,
  title,
  description,
  state,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
  state: ComponentState | "up" | "unreachable";
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-2">
        <div className="space-y-1">
          <CardTitle className="flex items-center gap-2 text-base">
            <Icon className="text-muted-foreground size-4" />
            {title}
          </CardTitle>
          <CardDescription>{description}</CardDescription>
        </div>
        <StateBadge state={state} />
      </CardHeader>
    </Card>
  );
}

export function SystemStatus({ api }: { api?: ApiClient }) {
  const client = useMemo(() => api ?? createApiClient(), [api]);
  const { data, isPending, isFetching, refetch } = useQuery({
    queryKey: ["backend-health"],
    queryFn: () => fetchBackendHealth(client),
    refetchInterval: REFRESH_MS,
  });

  if (isPending || !data) {
    return (
      <div className="grid gap-4 sm:grid-cols-3" aria-busy="true" aria-label="Loading system status">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-24 w-full" />
        ))}
      </div>
    );
  }

  const overall = {
    ready: { label: "All systems operational", variant: "secondary" as const },
    degraded: { label: "Degraded — a dependency is failing", variant: "destructive" as const },
    unreachable: { label: "Backend unreachable", variant: "destructive" as const },
  }[data.overall];

  return (
    <section aria-label="Backend health" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Badge variant={overall.variant} role="status">
            {overall.label}
          </Badge>
          <span className="text-muted-foreground text-xs">
            Checked {data.checkedAt.toLocaleTimeString()}
            {data.version ? ` · API v${data.version}` : ""}
          </span>
        </div>
        <Button variant="outline" size="sm" onClick={() => void refetch()} disabled={isFetching}>
          <RefreshCw className={isFetching ? "animate-spin" : ""} />
          Refresh
        </Button>
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <StatusCard icon={Server} title="API" description="FastAPI service" state={data.api} />
        <StatusCard icon={Database} title="Database" description="PostgreSQL" state={data.database} />
        <StatusCard icon={Zap} title="Redis" description="Job queue & rate limits" state={data.redis} />
      </div>

      {data.overall === "unreachable" && (
        <Card>
          <CardContent className="text-muted-foreground pt-6 text-sm">
            The dashboard could not reach the backend. Check that the API is running and that{" "}
            <code className="bg-muted rounded px-1 py-0.5">NEXT_PUBLIC_API_URL</code> points at it.
          </CardContent>
        </Card>
      )}
    </section>
  );
}
