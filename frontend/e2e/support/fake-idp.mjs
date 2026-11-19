// A minimal fake of Supabase Auth (GoTrue) for end-to-end tests. TEST ONLY — never deployed.
//
// It speaks just enough of the protocol for @supabase/supabase-js and for the backend's JWT verification, and it signs
// REAL ES256 tokens, so the backend and the dashboard run unmodified against it:
//   POST /auth/v1/token?grant_type=password|refresh_token   GET /auth/v1/user   POST /auth/v1/logout
//   GET  /auth/v1/.well-known/jwks.json
// Plus a control API under /__test__/ that lets tests inspect what the dashboard did (sign-ins, refreshes, sign-outs)
// and obtain valid tokens to attempt bypasses with.
import { createPrivateKey, createPublicKey, generateKeyPairSync, randomUUID, sign } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const PORT = Number(process.env.FAKE_IDP_PORT ?? 54321);
const HOST = "127.0.0.1";
const BASE = `http://${HOST}:${PORT}`;
const ISSUER = `${BASE}/auth/v1`;
const KID = "e2e-key-1";
const REUSE_GRACE_MS = 10_000; // Supabase tolerates re-presenting the previous refresh token briefly (concurrent requests)

const PASSWORD = "e2e-correct-horse-battery";
// email -> { id, ttl (seconds an access token lives) }
const USERS = new Map([
  ["owner@example.com", { id: "11111111-1111-4111-8111-111111111111", ttl: 3600 }],
  // 95 s: just above supabase-js's 90 s "refresh soon" margin, so a fresh token is NOT refreshed at once but becomes
  // due for refresh ~5 s later. (Shorter than the margin would make every page render try to refresh.)
  ["shortlived@example.com", { id: "22222222-2222-4222-8222-222222222222", ttl: 95 }],
  ["intruder@example.com", { id: "33333333-3333-4333-8333-333333333333", ttl: 3600 }],
]);

// The signing key survives restarts of this process (in a git-ignored file), so a long-running backend that already
// cached the published key keeps verifying tokens after the fake provider is restarted. CI starts from scratch each time.
const KEY_FILE =
  process.env.FAKE_IDP_KEY_FILE ??
  join(dirname(fileURLToPath(import.meta.url)), "..", ".auth", "idp-key.pem");
function loadKeys() {
  if (existsSync(KEY_FILE)) {
    const privateKey = createPrivateKey(readFileSync(KEY_FILE));
    return { privateKey, publicKey: createPublicKey(privateKey) };
  }
  const pair = generateKeyPairSync("ec", { namedCurve: "P-256" });
  mkdirSync(dirname(KEY_FILE), { recursive: true });
  writeFileSync(KEY_FILE, pair.privateKey.export({ type: "pkcs8", format: "pem" }), { mode: 0o600 });
  return pair;
}
const { privateKey, publicKey } = loadKeys();
const jwk = {
  ...publicKey.export({ format: "jwk" }),
  kid: KID,
  alg: "ES256",
  use: "sig",
  key_ops: ["verify"],
};

const refreshTokens = new Map(); // token -> { email, session, usedAt|null, revoked }
const stats = {
  signIns: 0,
  failedSignIns: 0,
  refreshes: 0,
  logouts: [],
  lastAccessToken: null,
  lastRefreshToken: null,
};

const b64url = (input) => Buffer.from(input).toString("base64url");

function mintAccessToken(email, sessionId) {
  const user = USERS.get(email);
  const now = Math.floor(Date.now() / 1000);
  const header = { alg: "ES256", typ: "JWT", kid: KID };
  const payload = {
    iss: ISSUER,
    aud: "authenticated",
    sub: user.id,
    email,
    role: "authenticated",
    aal: "aal1",
    amr: [{ method: "password", timestamp: now }],
    session_id: sessionId,
    is_anonymous: false,
    iat: now,
    exp: now + user.ttl,
    app_metadata: { provider: "email", providers: ["email"] },
    user_metadata: { email_verified: true },
  };
  const data = `${b64url(JSON.stringify(header))}.${b64url(JSON.stringify(payload))}`;
  const signature = sign("sha256", Buffer.from(data), { key: privateKey, dsaEncoding: "ieee-p1363" });
  return { token: `${data}.${b64url(signature)}`, exp: payload.exp, expiresIn: user.ttl };
}

function sessionFor(email, sessionId = randomUUID(), previousRefresh = null) {
  const { token, exp, expiresIn } = mintAccessToken(email, sessionId);
  const refresh = randomUUID().replaceAll("-", "").slice(0, 12);
  refreshTokens.set(refresh, { email, session: sessionId, usedAt: null, revoked: false });
  if (previousRefresh) previousRefresh.usedAt = Date.now();
  stats.lastAccessToken = token;
  stats.lastRefreshToken = refresh;
  return {
    access_token: token,
    token_type: "bearer",
    expires_in: expiresIn,
    expires_at: exp,
    refresh_token: refresh,
    user: userObject(email),
  };
}

function userObject(email) {
  const stamp = new Date().toISOString();
  return {
    id: USERS.get(email).id,
    aud: "authenticated",
    role: "authenticated",
    email,
    email_confirmed_at: stamp,
    phone: "",
    app_metadata: { provider: "email", providers: ["email"] },
    user_metadata: { email_verified: true },
    identities: [],
    created_at: stamp,
    updated_at: stamp,
  };
}

function send(res, status, body, headers = {}) {
  const payload = body === null ? "" : JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json", "cache-control": "no-store", ...headers });
  res.end(payload);
}

const authError = (res, status, code, msg) => send(res, status, { code: status, error_code: code, msg });

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  } catch {
    return {};
  }
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", BASE);
  const route = `${req.method} ${url.pathname}`;

  if (route === "GET /auth/v1/.well-known/jwks.json") {
    return send(res, 200, { keys: [jwk] }, { "cache-control": "public, max-age=600" });
  }

  if (route === "POST /auth/v1/token") {
    const body = await readJson(req);
    const grant = url.searchParams.get("grant_type");
    if (grant === "password") {
      const email = String(body.email ?? "").toLowerCase();
      if (!USERS.has(email) || body.password !== PASSWORD) {
        stats.failedSignIns += 1;
        return authError(res, 400, "invalid_credentials", "Invalid login credentials");
      }
      stats.signIns += 1;
      return send(res, 200, sessionFor(email));
    }
    if (grant === "refresh_token") {
      const entry = refreshTokens.get(String(body.refresh_token ?? ""));
      const reusedInGrace = entry?.usedAt && Date.now() - entry.usedAt < REUSE_GRACE_MS;
      if (!entry || entry.revoked || (entry.usedAt && !reusedInGrace)) {
        return authError(
          res,
          400,
          "refresh_token_not_found",
          "Invalid Refresh Token: Refresh Token Not Found",
        );
      }
      stats.refreshes += 1;
      return send(res, 200, sessionFor(entry.email, entry.session, entry));
    }
    return authError(res, 400, "validation_failed", "unsupported grant_type");
  }

  if (route === "POST /auth/v1/logout") {
    const scope = url.searchParams.get("scope") ?? "global";
    stats.logouts.push(scope);
    // Like Supabase, "global" ends every session of the user who signed out — and ONLY that user's.
    let email = null;
    try {
      const bearer = String(req.headers.authorization ?? "").replace(/^Bearer /i, "");
      email = JSON.parse(Buffer.from(bearer.split(".")[1], "base64url").toString("utf8")).email;
    } catch {
      // no usable bearer token: nothing to revoke
    }
    if (scope === "global" && email) {
      for (const entry of refreshTokens.values()) if (entry.email === email) entry.revoked = true;
    }
    return send(res, 204, null);
  }

  if (route === "GET /auth/v1/user") {
    const bearer = String(req.headers.authorization ?? "").replace(/^Bearer /i, "");
    try {
      const claims = JSON.parse(Buffer.from(bearer.split(".")[1], "base64url").toString("utf8"));
      if (USERS.has(claims.email)) return send(res, 200, userObject(claims.email));
    } catch {
      // fall through to the 401 below
    }
    return authError(res, 401, "bad_jwt", "invalid JWT");
  }

  // ───────── control API for tests ─────────
  if (route === "GET /__test__/health") return send(res, 200, { ok: true });
  if (route === "GET /__test__/state") return send(res, 200, stats);
  if (route === "POST /__test__/reset") {
    Object.assign(stats, {
      signIns: 0,
      failedSignIns: 0,
      refreshes: 0,
      logouts: [],
      lastAccessToken: null,
      lastRefreshToken: null,
    });
    return send(res, 200, { ok: true });
  }
  if (route === "POST /__test__/mint") {
    const body = await readJson(req);
    const email = String(body.email ?? "owner@example.com");
    if (!USERS.has(email)) return send(res, 404, { error: "unknown user" });
    return send(res, 200, { access_token: mintAccessToken(email, randomUUID()).token });
  }
  if (route === "GET /__test__/credentials")
    return send(res, 200, { password: PASSWORD, users: [...USERS.keys()] });

  return send(res, 404, { error: "not found" });
});

server.listen(PORT, HOST, () => {
  console.log(`fake identity provider listening on ${BASE}`);
});
