import type { Metadata } from "next";

import { GmailConnections } from "@/components/gmail-connections";
import { noticeFrom } from "@/lib/gmail/notices";

export const metadata: Metadata = { title: "Integrations" };

const single = (value: string | string[] | undefined) => (typeof value === "string" ? value : undefined);

export default async function IntegrationsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  // Selects a message from a fixed list; the raw query values are never rendered.
  const notice = noticeFrom({ connected: single(params.connected), error: single(params.error) });

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Integrations</h1>
        <p className="text-muted-foreground text-sm">
          Connect the Gmail mailboxes the system sends from and reads replies in. Each mailbox serves one
          mode, and nothing is sent until you approve a campaign. You can disconnect a mailbox here at any
          time.
        </p>
      </div>
      <GmailConnections notice={notice} />
    </div>
  );
}
