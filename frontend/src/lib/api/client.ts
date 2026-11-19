import createClient from "openapi-fetch";

import { BFF_BASE, LOGIN_PATH } from "@/lib/auth/constants";

import type { paths } from "./schema";

export interface ApiClientOptions {
  /** Defaults to the same-origin API proxy. Tests pass an absolute URL. */
  baseUrl?: string;
  /** Called when the server says the session is gone. Defaults to sending the browser to the login page. */
  onUnauthorized?: () => void;
}

function redirectToLogin(): void {
  if (typeof window === "undefined") return;
  const here = `${window.location.pathname}${window.location.search}`;
  // A full page load on purpose (not router.push): it discards every piece of client state belonging to the dead session.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  window.location.assign(`${LOGIN_PATH}?next=${encodeURIComponent(here)}`);
}

/**
 * Typed API client generated from the backend's OpenAPI schema (contract-first).
 *
 * It talks to this app's own origin (`/bff/...`) — never to the backend directly — and carries NO credentials of its
 * own: the access token lives in an HttpOnly cookie that JavaScript cannot read, and the server-side proxy attaches it
 * (ADR-014). So there is nothing here for an injected script to steal, and no token provider to configure.
 */
export function createApiClient({
  baseUrl = BFF_BASE,
  onUnauthorized = redirectToLogin,
}: ApiClientOptions = {}) {
  const client = createClient<paths>({ baseUrl });
  client.use({
    onResponse({ response }) {
      if (response.status === 401) onUnauthorized();
      return undefined;
    },
  });
  return client;
}

export type ApiClient = ReturnType<typeof createApiClient>;
