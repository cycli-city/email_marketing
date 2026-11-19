import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { headers } from "next/headers";

import { Providers } from "@/components/providers";

import "./globals.css";

// Every page renders per request so Next.js can stamp the CSP nonce (set in src/proxy.ts) onto its scripts.
export const dynamic = "force-dynamic";

// shadcn's theme reads `--font-sans` / `--font-geist-mono`.
const geistSans = Geist({ variable: "--font-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: { default: "AI Outreach Intelligence", template: "%s · AI Outreach" },
  description: "Autonomous AI lead discovery, outreach and reply management.",
  robots: { index: false, follow: false }, // private operator tool
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // The nonce is created per request in src/proxy.ts and forwarded as a request header.
  const nonce = (await headers()).get("x-nonce") ?? undefined;

  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="bg-background text-foreground min-h-full font-sans">
        <Providers nonce={nonce}>{children}</Providers>
      </body>
    </html>
  );
}
