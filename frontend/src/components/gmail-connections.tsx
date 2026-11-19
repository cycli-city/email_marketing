"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Mail, PlugZap, TriangleAlert } from "lucide-react";
import { useMemo, useState } from "react";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { createApiClient, type ApiClient } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { Notice } from "@/lib/gmail/notices";

type Account = components["schemas"]["AccountOut"];
type Mode = Account["mode"];

const MODES: { mode: Mode; title: string; description: string }[] = [
  {
    mode: "nexora",
    title: "NEXORA Sales",
    description: "The mailbox used to reach prospective NEXORA customers.",
  },
  {
    mode: "job_hunter",
    title: "Job Hunter",
    description: "The mailbox used for recruiters, jobs, part-time and freelance outreach.",
  },
];

type AccountsResult = { kind: "ok"; accounts: Account[] } | { kind: "not_configured" };

function StatusBadge({ status }: { status: Account["status"] }) {
  if (status === "active") {
    return (
      <Badge variant="secondary" className="gap-1">
        <CheckCircle2 className="size-3 text-emerald-600 dark:text-emerald-400" />
        Active
      </Badge>
    );
  }
  const label = { needs_reauth: "Needs reconnecting", error: "Error", disconnected: "Disconnected" }[status];
  return (
    <Badge variant="destructive" className="gap-1">
      <TriangleAlert className="size-3" />
      {label}
    </Badge>
  );
}

const message = (error: unknown, fallback: string): string =>
  (error as { error?: { message?: string } } | undefined)?.error?.message ?? fallback;

export function GmailConnections({
  notice = null,
  api,
  navigate = (url: string) => window.location.assign(url),
}: {
  notice?: Notice | null;
  api?: ApiClient;
  /** Leaves this site for Google's consent screen. Injectable so tests need no real navigation. */
  navigate?: (url: string) => void;
}) {
  const client = useMemo(() => api ?? createApiClient(), [api]);
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState<Account | null>(null);
  const [results, setResults] = useState<Record<string, { ok: boolean; text: string }>>({});

  const accounts = useQuery({
    queryKey: ["gmail-accounts"],
    queryFn: async (): Promise<AccountsResult> => {
      const { data, response } = await client.GET("/api/v1/integrations/gmail/accounts");
      if (data) return { kind: "ok", accounts: data.accounts };
      if (response.status === 503) return { kind: "not_configured" };
      throw new Error(`Could not load mailboxes (HTTP ${response.status})`);
    },
  });

  const connect = useMutation({
    mutationFn: async (mode: Mode) => {
      const { data, error } = await client.POST("/api/v1/integrations/gmail/connect", { body: { mode } });
      if (!data) throw new Error(message(error, "Could not start the connection"));
      return data.authorization_url;
    },
    onSuccess: (url) => navigate(url),
  });

  const test = useMutation({
    mutationFn: async (account: Account) => {
      const { data, error } = await client.POST("/api/v1/integrations/gmail/accounts/{account_id}/test", {
        params: { path: { account_id: account.id } },
      });
      return { account, data, error };
    },
    onSuccess: ({ account, data, error }) => {
      setResults((current) => ({
        ...current,
        [account.id]: data
          ? { ok: true, text: `Working — reached ${data.email}` }
          : { ok: false, text: message(error, "The connection test failed") },
      }));
      void queryClient.invalidateQueries({ queryKey: ["gmail-accounts"] });
    },
  });

  const disconnect = useMutation({
    mutationFn: async (account: Account) => {
      await client.DELETE("/api/v1/integrations/gmail/accounts/{account_id}", {
        params: { path: { account_id: account.id } },
      });
    },
    onSuccess: () => {
      setConfirming(null);
      void queryClient.invalidateQueries({ queryKey: ["gmail-accounts"] });
    },
  });

  const loaded = accounts.data?.kind === "ok" ? accounts.data.accounts : null;

  return (
    <div className="space-y-6">
      {notice && (
        <div
          role="status"
          className={`rounded-lg border p-3 text-sm ${
            notice.tone === "success"
              ? "border-emerald-600/30 bg-emerald-600/5"
              : "border-destructive/30 bg-destructive/5 text-destructive"
          }`}
        >
          {notice.text}
        </div>
      )}

      {accounts.isPending && (
        <div className="space-y-4" aria-label="Loading mailboxes">
          <Skeleton className="h-40 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
      )}

      {accounts.isError && (
        <Card>
          <CardContent className="text-destructive pt-6 text-sm" role="alert">
            Could not load the connected mailboxes. Check that the backend is running, then refresh.
          </CardContent>
        </Card>
      )}

      {accounts.data?.kind === "not_configured" && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <PlugZap className="size-4" />
              Gmail is not configured on the server
            </CardTitle>
            <CardDescription>
              The backend has no Google sign-in credentials or encryption key yet. Add them to the
              server&apos;s environment (see .env.example in the repository), restart it, then reload this
              page.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      {loaded &&
        MODES.map(({ mode, title, description }) => {
          const mailboxes = loaded.filter((a) => a.mode === mode);
          return (
            <Card key={mode} aria-label={title}>
              <CardHeader className="flex flex-row items-start justify-between gap-3">
                <div className="space-y-1">
                  <CardTitle className="text-base">{title}</CardTitle>
                  <CardDescription>{description}</CardDescription>
                </div>
                <Button
                  size="sm"
                  onClick={() => connect.mutate(mode)}
                  disabled={connect.isPending}
                  aria-label={`Connect a Gmail mailbox for ${title}`}
                >
                  <Mail />
                  {mailboxes.length ? "Connect another" : "Connect Gmail"}
                </Button>
              </CardHeader>
              <CardContent className="space-y-3">
                {mailboxes.length === 0 && (
                  <p className="text-muted-foreground text-sm">No mailbox connected yet.</p>
                )}
                {mailboxes.map((account) => {
                  const result = results[account.id];
                  return (
                    <div
                      key={account.id}
                      className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3"
                    >
                      <div className="space-y-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-medium">{account.email}</span>
                          <StatusBadge status={account.status} />
                        </div>
                        <p className="text-muted-foreground text-xs">
                          {account.account_type === "google_workspace"
                            ? "Google Workspace"
                            : "Personal Gmail"}
                          {account.connected_at
                            ? ` · connected ${new Date(account.connected_at).toLocaleString()}`
                            : ""}
                        </p>
                        {account.last_error && (
                          <p className="text-destructive text-xs">{account.last_error}</p>
                        )}
                        {result && (
                          <p
                            role="status"
                            className={`text-xs ${result.ok ? "text-emerald-700 dark:text-emerald-400" : "text-destructive"}`}
                          >
                            {result.text}
                          </p>
                        )}
                      </div>
                      <div className="flex gap-2">
                        {account.status === "needs_reauth" ? (
                          <Button
                            size="sm"
                            onClick={() => connect.mutate(account.mode)}
                            aria-label={`Reconnect ${account.email}`}
                          >
                            Reconnect
                          </Button>
                        ) : (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => test.mutate(account)}
                            disabled={test.isPending}
                            aria-label={`Test the connection to ${account.email}`}
                          >
                            Test connection
                          </Button>
                        )}
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setConfirming(account)}
                          aria-label={`Disconnect ${account.email}`}
                        >
                          Disconnect
                        </Button>
                      </div>
                    </div>
                  );
                })}
              </CardContent>
            </Card>
          );
        })}

      {connect.isError && (
        <p role="alert" className="text-destructive text-sm">
          {connect.error.message}
        </p>
      )}

      <AlertDialog open={confirming !== null} onOpenChange={(open) => !open && setConfirming(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Disconnect {confirming?.email}?</AlertDialogTitle>
            <AlertDialogDescription>
              This removes the mailbox from the dashboard and revokes Google&apos;s access. Messages already
              in Gmail are untouched. You can connect it again at any time.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep it</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={disconnect.isPending}
              onClick={() => confirming && disconnect.mutate(confirming)}
            >
              Disconnect
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
