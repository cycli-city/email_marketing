import type { NextConfig } from "next";

/**
 * Static security headers (the per-request Content-Security-Policy is set in src/proxy.ts because it
 * needs a fresh nonce). Cache Components / partial prefetching are intentionally OFF: a nonce-based CSP
 * requires every page to be rendered per request, and this is a private, authenticated dashboard where
 * static prerendering buys nothing. See docs/DECISIONS.md ADR-015.
 */
const securityHeaders = [
  // HTTPS only, for two years, including subdomains.
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" }, // legacy twin of CSP frame-ancestors
  { key: "Referrer-Policy", value: "no-referrer" },
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
  },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  { key: "Cross-Origin-Resource-Policy", value: "same-origin" },
  { key: "X-DNS-Prefetch-Control", value: "off" },
];

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false, // don't advertise the framework
  productionBrowserSourceMaps: false, // never ship source maps to the browser
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
