import { ServerCrash } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

/** Shown instead of the dashboard when the session is fine but the backend cannot be asked who you are. */
export function BackendUnavailable() {
  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle>
            <h1 className="flex items-center gap-2">
              <ServerCrash className="text-destructive size-5" />
              The backend is not responding
            </h1>
          </CardTitle>
          <CardDescription>
            You are still signed in. The dashboard could not reach the API to confirm your session.
          </CardDescription>
        </CardHeader>
        <CardContent className="text-muted-foreground text-sm">
          Check that the backend is running and that BACKEND_URL points at it, then{" "}
          {/* A plain link (not next/link) so the request is made again from scratch. */}
          <a href="" className="text-foreground underline underline-offset-4">
            try again
          </a>
          .
        </CardContent>
      </Card>
    </main>
  );
}
