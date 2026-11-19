export interface CspOptions {
  /** Fresh, unguessable per-request value. Every legitimate script/style tag carries it. */
  nonce: string;
  isDev: boolean;
  /** Extra origins the browser may call with fetch/XHR/WebSocket (the API, Supabase). */
  connectOrigins: string[];
}

/**
 * Strict Content-Security-Policy.
 *
 * - `script-src` uses a nonce + `strict-dynamic`: injected inline scripts and event-handler attributes do
 *   not run, even if an attacker finds an HTML-injection bug. There is no `unsafe-inline` for scripts.
 * - `connect-src` is an allow-list, so injected code cannot exfiltrate data to an attacker's server.
 * - `frame-ancestors 'none'` prevents clickjacking; `object-src 'none'` and `base-uri` close plugin and
 *   `<base>` tag tricks; `form-action 'self'` stops forms posting to other origins.
 * - `style-src-attr 'unsafe-inline'` only permits inline `style="…"` ATTRIBUTES (component libraries set
 *   CSS variables that way). Style attributes cannot execute script; <style> elements still need the nonce.
 */
export function buildCsp({ nonce, isDev, connectOrigins }: CspOptions): string {
  const directives: Record<string, string[]> = {
    "default-src": ["'self'"],
    "script-src": ["'self'", `'nonce-${nonce}'`, "'strict-dynamic'", ...(isDev ? ["'unsafe-eval'"] : [])],
    // Dev tooling (HMR) injects un-nonced <style>; production stays strict.
    "style-src": isDev ? ["'self'", "'unsafe-inline'"] : ["'self'", `'nonce-${nonce}'`],
    "style-src-attr": ["'unsafe-inline'"],
    "img-src": ["'self'", "data:", "blob:"],
    "font-src": ["'self'"],
    "connect-src": ["'self'", ...connectOrigins, ...(isDev ? ["ws://localhost:*", "ws://127.0.0.1:*"] : [])],
    "frame-src": ["'none'"],
    "object-src": ["'none'"],
    "base-uri": ["'self'"],
    "form-action": ["'self'"],
    "frame-ancestors": ["'none'"],
    "manifest-src": ["'self'"],
    // Would break plain-http localhost in development.
    ...(isDev ? {} : { "upgrade-insecure-requests": [] }),
  };
  return Object.entries(directives)
    .map(([name, values]) => (values.length ? `${name} ${values.join(" ")}` : name))
    .join("; ");
}

/** A 128-bit-class random nonce (UUIDv4 carries 122 random bits), base64 encoded. */
export function generateNonce(): string {
  return btoa(crypto.randomUUID());
}
