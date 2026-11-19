"use client";

import { Button } from "@/components/ui/button";

/** Last-resort boundary for unexpected render errors. Shows a reference id, never the stack or message. */
export default function ErrorPage({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <main
      role="alert"
      className="flex min-h-screen flex-col items-center justify-center gap-4 p-6 text-center"
    >
      <h1 className="text-2xl font-semibold tracking-tight">Something went wrong</h1>
      <p className="text-muted-foreground max-w-md text-sm">
        An unexpected error occurred. You can try again; if it keeps happening, quote the reference below.
      </p>
      {error.digest && <code className="bg-muted rounded px-2 py-1 text-xs">ref: {error.digest}</code>}
      <Button onClick={reset}>Try again</Button>
    </main>
  );
}
