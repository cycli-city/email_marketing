#!/usr/bin/env python3
"""Small GitHub helper for this repo (stdlib only). Reads GITHUB_TOKEN from .local/SECRETS.local.md.

The token is passed to git through environment variables (never argv, never .git/config, never printed)
and to the REST API through an Authorization header that is never logged.

    python scripts/github.py status                 # repo visibility, default branch, token permissions
    python scripts/github.py push [branch]          # push with the token (runs the pre-push hook)
    python scripts/github.py runs [--sha SHA]       # latest workflow runs + job conclusions
    python scripts/github.py wait [--sha SHA]       # poll until runs finish; print failing steps; exit 1 on failure
    python scripts/github.py logs JOB_ID            # failing job log (tail)
    python scripts/github.py api METHOD /path [JSON_BODY]
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRETS = REPO_ROOT / ".local" / "SECRETS.local.md"
OWNER, REPO = "cycli-city", "email_marketing"
API = "https://api.github.com"


def token() -> str:
    if env := os.environ.get("GITHUB_TOKEN"):
        return env
    match = re.search(r"^\W*GITHUB_TOKEN=(\S+)", SECRETS.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        sys.exit("GITHUB_TOKEN not found in .local/SECRETS.local.md")
    return match.group(1)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_: Any, **__: Any) -> None:  # never forward credentials to another host
        return None


def api(method: str, path: str, body: Any = None, *, raw: bool = False) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - fixed https base URL
        API + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "aoi-dev-script",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=30) as resp:  # noqa: S310
            payload = resp.read()
            return payload if raw else (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as err:
        if err.code in (301, 302, 303, 307) and raw:
            return fetch_unauthenticated(err.headers["Location"])
        detail = err.read().decode(errors="replace")[:500]
        raise SystemExit(f"GitHub API {method} {path} -> HTTP {err.code}: {detail}") from None


def fetch_unauthenticated(url: str) -> bytes:
    if not url.startswith("https://"):
        sys.exit("refusing non-https redirect")
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310
        return resp.read()


def git_env() -> dict[str, str]:
    basic = base64.b64encode(f"x-access-token:{token()}".encode()).decode()
    env = dict(os.environ)
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
        GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}",
    )
    return env


def cmd_status() -> None:
    info = api("GET", f"/repos/{OWNER}/{REPO}")
    print(f"repo: {info['full_name']}  private={info['private']}  default_branch={info['default_branch']}")
    print(f"permissions: {info.get('permissions')}  size_kb={info['size']}  archived={info['archived']}")


def cmd_push(branch: str) -> None:
    remotes = subprocess.run(["git", "remote"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
    if "origin" not in remotes.split():
        subprocess.run(
            ["git", "remote", "add", "origin", f"https://github.com/{OWNER}/{REPO}.git"], cwd=REPO_ROOT, check=True
        )
    sys.exit(subprocess.call(["git", "push", "-u", "origin", branch], cwd=REPO_ROOT, env=git_env()))


def head_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def runs_for(sha: str) -> list[dict[str, Any]]:
    return api("GET", f"/repos/{OWNER}/{REPO}/actions/runs?head_sha={sha}&per_page=20")["workflow_runs"]


def jobs_for(run_id: int) -> list[dict[str, Any]]:
    return api("GET", f"/repos/{OWNER}/{REPO}/actions/runs/{run_id}/jobs?per_page=50")["jobs"]


def cmd_runs(sha: str, *, verbose: bool = True) -> bool:
    """Print status; return True when every run has completed."""
    runs = runs_for(sha)
    if not runs:
        print(f"no workflow runs yet for {sha[:8]}")
        return False
    done = True
    for run in runs:
        print(f"● {run['name']}: {run['status']} / {run['conclusion']}  ({run['html_url']})")
        done &= run["status"] == "completed"
        if verbose:
            for job in jobs_for(run["id"]):
                print(f"    - {job['name']}: {job['status']} / {job['conclusion']}  [job {job['id']}]")
                for step in job.get("steps", []):
                    if step.get("conclusion") == "failure":
                        print(f"        ✖ step failed: {step['name']}")
    return done


def cmd_wait(sha: str, timeout_s: int = 1500) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        runs = runs_for(sha)
        if runs and all(r["status"] == "completed" for r in runs):
            cmd_runs(sha)
            sys.exit(0 if all(r["conclusion"] in ("success", "skipped") for r in runs) else 1)
        time.sleep(20)
    cmd_runs(sha)
    sys.exit("timed out waiting for CI")


def cmd_logs(job_id: int, tail: int = 80) -> None:
    text = api("GET", f"/repos/{OWNER}/{REPO}/actions/jobs/{job_id}/logs", raw=True).decode(errors="replace")
    print("\n".join(text.splitlines()[-tail:]))


def cmd_run_logs(run_id: int, needle: str, tail: int = 60) -> None:
    """Print the tail of every step log whose file name contains `needle` (from the run's log archive)."""
    import io
    import zipfile

    archive = api("GET", f"/repos/{OWNER}/{REPO}/actions/runs/{run_id}/logs", raw=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = [n for n in zf.namelist() if needle.lower() in n.lower() and n.endswith(".txt")]
        if not names:
            print("no matching step logs; available:")
            print("\n".join(sorted(zf.namelist())[:80]))
            return
        for name in sorted(names):
            print(f"\n──────── {name} ────────")
            lines = zf.read(name).decode(errors="replace").splitlines()
            print("\n".join(re.sub(r"^\S+Z ", "", line) for line in lines[-tail:]))


def _utf8_output() -> None:
    """Windows consoles default to cp1252, where printing a status marker raises mid-run."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str]) -> None:
    _utf8_output()
    if not argv:
        sys.exit(__doc__)
    cmd, rest = argv[0], argv[1:]
    sha = rest[rest.index("--sha") + 1] if "--sha" in rest else head_sha()
    if cmd == "status":
        cmd_status()
    elif cmd == "push":
        cmd_push(rest[0] if rest and not rest[0].startswith("--") else "main")
    elif cmd == "runs":
        cmd_runs(sha)
    elif cmd == "wait":
        cmd_wait(sha)
    elif cmd == "logs":
        cmd_logs(int(rest[0]))
    elif cmd == "rlogs":  # rlogs RUN_ID NAME_FILTER [TAIL]
        cmd_run_logs(int(rest[0]), rest[1], int(rest[2]) if len(rest) > 2 else 60)
    elif cmd == "api":
        body = json.loads(rest[2]) if len(rest) > 2 else None
        print(json.dumps(api(rest[0], rest[1], body), indent=2))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
