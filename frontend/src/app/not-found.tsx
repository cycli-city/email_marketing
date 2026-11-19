import Link from "next/link";

import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * Custom 404. Next's built-in page ships an inline <style> without a nonce, which our strict CSP
 * (rightly) blocks — this one is styled entirely with classes.
 */
export default function NotFound() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-4 p-6 text-center">
      <p className="text-muted-foreground text-sm font-medium">404</p>
      <h1 className="text-2xl font-semibold tracking-tight">Page not found</h1>
      <p className="text-muted-foreground max-w-md text-sm">
        The page you are looking for doesn&apos;t exist or has moved.
      </p>
      <Link href="/system" className={cn(buttonVariants({ variant: "outline" }))}>
        Back to the dashboard
      </Link>
    </main>
  );
}
