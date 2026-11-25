import type { Metadata } from "next";

import { SendingSettings } from "@/components/sending-settings";

export const metadata: Metadata = { title: "Sending" };

export default function SendingPage() {
  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Sending</h1>
        <p className="text-muted-foreground text-sm">
          The identity every message carries and the rules for each country. Nothing is sent until both are in
          order and you have approved a campaign.
        </p>
      </div>
      <SendingSettings />
    </div>
  );
}
