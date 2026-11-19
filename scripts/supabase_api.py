#!/usr/bin/env python3
"""Minimal Supabase Management API helper (stdlib only). Reads SUPABASE_ACCESS_TOKEN from .local/SECRETS.local.md.

The account token is extremely powerful (every project on the account), so this tool is deliberately narrow:
* it never prints the token or any key/secret/password field (values are masked in output);
* project-scoped commands require an explicit project ref and refuse any ref that is not the app's project
  recorded as SUPABASE_PROJECT_REF in the secrets file.

    python scripts/supabase_api.py orgs                      # list organisations (read-only)
    python scripts/supabase_api.py projects                  # list projects: ref, name, region, status (read-only)
    python scripts/supabase_api.py get PATH                  # GET any /v1 path (masked output)
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SECRETS = Path(__file__).resolve().parents[1] / ".local" / "SECRETS.local.md"
BASE = "https://api.supabase.com"
SENSITIVE = re.compile(r"(key|secret|password|pass|token|jwt|connection|uri|url_with)", re.IGNORECASE)


def secret(name: str) -> str | None:
    match = re.search(rf"^\W*{re.escape(name)}=(\S+)", SECRETS.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else None


def request(method: str, path: str, body: Any = None) -> Any:
    token = secret("SUPABASE_ACCESS_TOKEN")
    if not token:
        sys.exit("SUPABASE_ACCESS_TOKEN not found in .local/SECRETS.local.md")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - fixed https base URL
        BASE + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "aoi-dev-script",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            payload = resp.read()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")[:600]
        raise SystemExit(f"Supabase API {method} {path} -> HTTP {err.code}: {detail}") from None


def mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***masked***" if SENSITIVE.search(k) and isinstance(v, str) else mask(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    return value


def set_secret(name: str, value: str, path: Path = SECRETS) -> None:
    """Write `- NAME=value` into the secrets file, preserving any trailing `# comment`. Never prints the value."""
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^(\W*{re.escape(name)}=)\S*(\s+#.*)?$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(lambda m: f"{m.group(1)}{value}{m.group(2) or ''}", text, count=1)
    else:
        text = text.rstrip("\n") + f"\n- {name}={value}\n"
    path.write_text(text, encoding="utf-8")


APP_ORG_NAME = "AI Outreach Intelligence"
APP_PROJECT_NAME = "aoi-outreach"
APP_REGION = "ap-south-1"  # Mumbai — closest to the owner and to Render's Singapore/Oregon regions we can reach


def provision() -> None:
    """Create a NEW, isolated free organisation + project for this app and store its credentials locally.

    Guards: never touches existing projects; aborts unless the new org is verified to be on the FREE plan;
    idempotent (does nothing if SUPABASE_PROJECT_REF is already recorded)."""
    import secrets
    import time

    if secret("SUPABASE_PROJECT_REF"):
        print("SUPABASE_PROJECT_REF already recorded — nothing to do.")
        return

    # Never creates organisations (an API-created org turned out to be unreadable/orphaned). Uses the owner's only org,
    # or the one named by SUPABASE_ORG_SLUG, and only if it is verified to be on the FREE plan with a free slot left.
    orgs = request("GET", "/v1/organizations")
    wanted = secret("SUPABASE_ORG_SLUG")
    candidates = [o for o in orgs if not wanted or (o.get("slug") or o.get("id")) == wanted]
    if len(candidates) != 1:
        sys.exit(f"ABORT: expected exactly one candidate organisation, found {len(candidates)}; set SUPABASE_ORG_SLUG")
    org = candidates[0]
    slug = org.get("slug") or org.get("id")
    detail = request("GET", f"/v1/organizations/{slug}")
    if detail.get("plan") != "free":
        sys.exit(f"ABORT: organisation plan is {detail.get('plan')!r}, not 'free' — refusing to create billable resources.")
    active = [p for p in request("GET", "/v1/projects") if p.get("organization_id") == slug and str(p.get("status", "")).startswith("ACTIVE")]
    if len(active) >= 2:
        sys.exit("ABORT: the free plan allows 2 active projects and both slots are used — not pausing or deleting anything.")
    print(f"organisation: {detail.get('name')} (plan={detail.get('plan')}, active projects={len(active)}/2) — existing projects are left untouched")

    db_pass = secret("SUPABASE_DB_PASSWORD") or secrets.token_urlsafe(24)
    set_secret("SUPABASE_DB_PASSWORD", db_pass)  # stored BEFORE creation so it can never be lost
    created = request(
        "POST",
        "/v1/projects",
        {
            "name": APP_PROJECT_NAME,
            "organization_slug": slug,
            "db_pass": db_pass,
            "region": APP_REGION,
            "plan": "free",
            "data_api_revoke_default_privileges": True,
            "auth_site_url": "http://localhost:3000",
        },
    )
    ref = created["id"]
    set_secret("SUPABASE_PROJECT_REF", ref)
    print(f"project created: {created.get('name')} region={created.get('region')} status={created.get('status')}")

    for _ in range(60):  # up to ~10 minutes
        status = request("GET", f"/v1/projects/{ref}").get("status")
        if status == "ACTIVE_HEALTHY":
            break
        time.sleep(10)
    else:
        sys.exit(f"project {ref} did not become healthy in time (status={status}); check the dashboard")
    print("project is ACTIVE_HEALTHY")

    keys = request("GET", f"/v1/projects/{ref}/api-keys?reveal=true")
    by_name = {k.get("name"): k.get("api_key") for k in keys}
    set_secret("SUPABASE_URL", f"https://{ref}.supabase.co")
    for name, env in (("anon", "SUPABASE_ANON_KEY"), ("service_role", "SUPABASE_SERVICE_ROLE_KEY")):
        if by_name.get(name):
            set_secret(env, by_name[name])
    print("api keys stored:", sorted(n for n in by_name if by_name[n]))

    pooler = request("GET", f"/v1/projects/{ref}/config/database/pooler")
    session = next((p for p in pooler if p.get("pool_mode") == "session"), None) or (pooler[0] if pooler else None)
    if session:
        host, user = session["db_host"], session["db_user"]
        set_secret("DATABASE_URL", f"postgresql://{user}:{db_pass}@{host}:5432/{session.get('db_name', 'postgres')}?sslmode=require")
        print(f"database URL stored (session pooler, host={host})")
    else:
        print("WARNING: no pooler config returned; set DATABASE_URL manually")

    # Single-owner tool: nobody should be able to self-register through the public anon key.
    request("PATCH", f"/v1/projects/{ref}/config/auth", {"disable_signup": True})
    print("auth: public sign-ups disabled")


def main(argv: list[str]) -> None:
    if not argv:
        sys.exit(__doc__)
    cmd = argv[0]
    if cmd == "provision":
        provision()
    elif cmd == "orgs":
        for org in request("GET", "/v1/organizations"):
            print(json.dumps(mask(org)))
    elif cmd == "projects":
        for p in request("GET", "/v1/projects"):
            print(f"{p.get('id')}  {p.get('name')!s:<28} {p.get('region')!s:<14} {p.get('status')}  org={p.get('organization_id')}")
    elif cmd == "get" and len(argv) > 1:
        print(json.dumps(mask(request("GET", argv[1])), indent=2))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
