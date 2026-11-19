import createClient, { type Middleware } from "openapi-fetch";

import type { paths } from "./schema";

/** Base URL of the FastAPI backend. Public by design — it contains no secret. */
export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

export type AccessTokenProvider = () => Promise<string | null> | string | null;

/**
 * Typed API client generated from the backend's OpenAPI schema (contract-first).
 * When a token provider is given, every request carries `Authorization: Bearer <token>`.
 */
export function createApiClient(getAccessToken?: AccessTokenProvider, baseUrl: string = API_URL) {
  const client = createClient<paths>({ baseUrl });
  if (getAccessToken) {
    const auth: Middleware = {
      async onRequest({ request }) {
        const token = await getAccessToken();
        if (token) request.headers.set("Authorization", `Bearer ${token}`);
        return request;
      },
    };
    client.use(auth);
  }
  return client;
}

export type ApiClient = ReturnType<typeof createApiClient>;
