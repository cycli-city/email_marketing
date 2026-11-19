import { generateKeyPairSync, sign } from "node:crypto";

import { IDP_URL } from "./constants";

const b64url = (value: string | Buffer) => Buffer.from(value).toString("base64url");

/** A JWT that LOOKS exactly like the identity provider's, but is signed with a key the provider never published. */
export function forgeAccessToken(email: string, kid = "e2e-key-1"): string {
  const { privateKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
  const now = Math.floor(Date.now() / 1000);
  const header = { alg: "ES256", typ: "JWT", kid };
  const payload = {
    iss: `${IDP_URL}/auth/v1`,
    aud: "authenticated",
    sub: "11111111-1111-4111-8111-111111111111",
    email,
    role: "authenticated",
    aal: "aal1",
    session_id: "99999999-9999-4999-8999-999999999999",
    iat: now,
    exp: now + 3600,
    user_metadata: { email_verified: true },
  };
  const data = `${b64url(JSON.stringify(header))}.${b64url(JSON.stringify(payload))}`;
  const signature = sign("sha256", Buffer.from(data), { key: privateKey, dsaEncoding: "ieee-p1363" });
  return `${data}.${b64url(signature)}`;
}

/**
 * The value of the dashboard's session cookie for a given access token, in the Supabase SSR cookie format
 * (`base64-` + base64url(JSON)). Used to show that the dashboard trusts the token's SIGNATURE, not the cookie's shape.
 */
export function sessionCookieValue(accessToken: string, email: string): string {
  const now = Math.floor(Date.now() / 1000);
  const session = {
    access_token: accessToken,
    token_type: "bearer",
    expires_in: 3600,
    expires_at: now + 3600,
    refresh_token: "not-a-real-refresh-token",
    user: { id: "11111111-1111-4111-8111-111111111111", aud: "authenticated", role: "authenticated", email },
  };
  return `base64-${b64url(JSON.stringify(session))}`;
}
