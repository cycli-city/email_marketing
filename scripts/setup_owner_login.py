#!/usr/bin/env python3
"""Create the owner's dashboard login in the app's Supabase project and tighten the project's session settings.

    python scripts/setup_owner_login.py                    # create the login (once) + harden settings + verify a real sign-in
    python scripts/setup_owner_login.py --reset-password   # generate a NEW password for the existing login

What it does (all against the app's own project, recorded as SUPABASE_PROJECT_REF — any other project is refused):
* creates the owner as a confirmed Supabase Auth user with a generated 24-character password (public sign-ups stay
  disabled, so this is the only way in);
* stores OWNER_LOGIN_EMAIL / OWNER_LOGIN_PASSWORD in the git-ignored `.local/SECRETS.local.md` — the password is never
  printed, logged or committed;
* sets the access-token lifetime to 15 minutes (a stolen or signed-out access token is then useful for ≤ 15 min) and the
  minimum password length to 12 for any future password;
* signs in once for real and prints only non-sensitive facts about the resulting token (algorithm, lifetime, role, AAL).
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import supabase_api as sb

PRIVATE = Path(__file__).resolve().parents[1] / ".local" / "PRIVATE_CONTEXT.local.md"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
JWT_LIFETIME_SECONDS = 900
MIN_PASSWORD_LENGTH = 12


def owner_email() -> str:
    """The owner's primary address: the first e-mail on the `<OWNER_PRIMARY_GMAIL>` line of the private context file."""
    for line in PRIVATE.read_text(encoding="utf-8").splitlines():
        if "<OWNER_PRIMARY_GMAIL>" in line:
            found = EMAIL.search(line.split("<OWNER_PRIMARY_GMAIL>", 1)[1])
            if found:
                return found.group(0).lower()
    sys.exit("No <OWNER_PRIMARY_GMAIL> value found in .local/PRIVATE_CONTEXT.local.md")


def project() -> tuple[str, str, str, str]:
    ref, url = sb.secret("SUPABASE_PROJECT_REF"), sb.secret("SUPABASE_URL")
    service, anon = (
        sb.secret("SUPABASE_SERVICE_ROLE_KEY"),
        sb.secret("SUPABASE_ANON_KEY"),
    )
    if not (ref and url and service and anon):
        sys.exit(
            "SUPABASE_PROJECT_REF / SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / SUPABASE_ANON_KEY missing in .local"
        )
    if url.rstrip("/") != f"https://{ref}.supabase.co":
        sys.exit(
            "SUPABASE_URL does not belong to SUPABASE_PROJECT_REF — refusing to continue"
        )
    return ref, url.rstrip("/"), service, anon


def auth_request(
    method: str, url: str, key: str, body: Any = None, *, bearer: str | None = None
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {bearer or key}",
            "Accept": "application/json",
            "User-Agent": "aoi-dev-script",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
            return response.status, json.loads(payload) if payload else {}
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read().decode(errors="replace") or "{}")
        except ValueError:
            return error.code, {}


def find_user(url: str, service: str, email: str) -> dict[str, Any] | None:
    status, body = auth_request(
        "GET", f"{url}/auth/v1/admin/users?per_page=200", service
    )
    if status != 200:
        sys.exit(f"Could not list users (HTTP {status})")
    return next(
        (u for u in body.get("users", []) if (u.get("email") or "").lower() == email),
        None,
    )


def generate_password() -> str:
    while True:  # token_urlsafe can, rarely, lack a digit or an upper-case letter; insist on a mix
        candidate = secrets.token_urlsafe(18)
        if (
            any(c.isdigit() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.islower() for c in candidate)
        ):
            return candidate


def decode_part(token: str, index: int) -> dict[str, Any]:
    part = token.split(".")[index]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def main(argv: list[str]) -> None:
    reset = "--reset-password" in argv
    email = owner_email()
    ref, url, service, anon = project()

    # 1) the login
    existing = find_user(url, service, email)
    password: str | None = None
    if existing is None:
        password = generate_password()
        status, body = auth_request(
            "POST",
            f"{url}/auth/v1/admin/users",
            service,
            {"email": email, "password": password, "email_confirm": True},
        )
        if status not in (200, 201):
            sys.exit(
                f"Creating the owner login failed (HTTP {status}): {sb.mask(body)}"
            )
        print(f"created the owner login (user id {str(body.get('id'))[:8]}…)")
    elif reset:
        password = generate_password()
        status, body = auth_request(
            "PUT",
            f"{url}/auth/v1/admin/users/{existing['id']}",
            service,
            {"password": password},
        )
        if status != 200:
            sys.exit(f"Resetting the password failed (HTTP {status}): {sb.mask(body)}")
        print("generated a new password for the existing owner login")
    else:
        print(
            "the owner login already exists — leaving its password untouched (use --reset-password to replace it)"
        )

    if password:
        sb.set_secret("OWNER_LOGIN_EMAIL", email)
        sb.set_secret("OWNER_LOGIN_PASSWORD", password)
        print(
            "stored OWNER_LOGIN_EMAIL and OWNER_LOGIN_PASSWORD in .local/SECRETS.local.md (not shown)"
        )

    # 2) project settings (Management API; only this app's project)
    sb.request(
        "PATCH",
        f"/v1/projects/{ref}/config/auth",
        {"jwt_exp": JWT_LIFETIME_SECONDS, "password_min_length": MIN_PASSWORD_LENGTH},
    )
    settings = sb.request("GET", f"/v1/projects/{ref}/config/auth")
    print(
        "project auth settings:",
        {
            k: settings.get(k)
            for k in (
                "jwt_exp",
                "password_min_length",
                "disable_signup",
                "refresh_token_rotation_enabled",
            )
        },
    )

    # 3) a real sign-in, described without revealing anything sensitive
    login_password = password or sb.secret("OWNER_LOGIN_PASSWORD")
    if not login_password:
        print("no stored password to test a sign-in with; done")
        return
    started = time.time()
    status, session = auth_request(
        "POST",
        f"{url}/auth/v1/token?grant_type=password",
        anon,
        {"email": email, "password": login_password},
    )
    if status != 200:
        sys.exit(f"Test sign-in failed (HTTP {status}): {sb.mask(session)}")
    token = session["access_token"]
    header, claims = decode_part(token, 0), decode_part(token, 1)
    print(
        "test sign-in OK:",
        {
            "alg": header.get("alg"),
            "kid_present": bool(header.get("kid")),
            "role": claims.get("role"),
            "aal": claims.get("aal"),
            "lifetime_seconds": claims["exp"] - claims["iat"],
            "issued_ms_ago": round((started - claims["iat"]) * 1000)
            if claims["iat"] <= started
            else 0,
            "email_matches_owner": claims.get("email") == email,
        },
    )
    # Leave no active session behind from this check.
    auth_request("POST", f"{url}/auth/v1/logout?scope=global", anon, bearer=token)


if __name__ == "__main__":
    main(sys.argv[1:])
