"use client";

import { CSPProvider } from "@base-ui/react/csp-provider";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "next-themes";
import { useState, type ReactNode } from "react";

import { TooltipProvider } from "@/components/ui/tooltip";

/**
 * `nonce` is the per-request CSP nonce (set in src/proxy.ts). Libraries that must inject an inline
 * <style>/<script> (next-themes' pre-paint theme script, Base UI components) receive it here so they keep
 * working under a strict CSP — we never loosen the policy with `unsafe-inline` for them.
 *
 * NOTE: toasts are intentionally absent. `sonner` injects CSS at runtime without nonce support, which a strict
 * CSP rightly blocks; notifications will be built on Base UI's Toast (CSP-aware) when the dashboard needs them.
 */
export function Providers({ children, nonce }: { children: ReactNode; nonce?: string }) {
  // One QueryClient per browser session (created lazily so it is never shared between requests on the server).
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { staleTime: 15_000, retry: 1, refetchOnWindowFocus: true },
        },
      }),
  );

  return (
    <CSPProvider nonce={nonce}>
      <ThemeProvider
        attribute="class"
        defaultTheme="system"
        enableSystem
        disableTransitionOnChange
        nonce={nonce}
      >
        <QueryClientProvider client={queryClient}>
          <TooltipProvider>{children}</TooltipProvider>
        </QueryClientProvider>
      </ThemeProvider>
    </CSPProvider>
  );
}
