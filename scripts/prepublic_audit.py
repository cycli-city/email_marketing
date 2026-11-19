#!/usr/bin/env python3
"""Pre-public audit (stdlib only). Run before a repository is made public and on every CI run afterwards.

Anything pushed to a public repository is permanent (clones, forks, caches, Actions logs, pull-request refs), so this
checks what ``scan_secrets.py`` does not: PERSONAL data and unsafe public-CI patterns.

    python scripts/prepublic_audit.py                 # --tree --workflows (default)
    python scripts/prepublic_audit.py --history       # also inspect every commit (authors + added lines)
    python scripts/prepublic_audit.py --all

Checks
  tree       e-mail addresses (except documented examples / noreply), phone numbers, and every private value
             listed in `.local/PRIVATE_CONTEXT.local.md`, in all tracked text files
  history    the same checks on every added line of every commit + author/committer e-mails of every commit
  workflows  no `pull_request_target`/`workflow_run`, explicit least-privilege `permissions`, third-party actions
             pinned to a commit SHA, `persist-credentials: false` on checkouts, no secrets in fork-reachable jobs
  phrases    wording that must not be published (e.g. "pasted in chat")
Exit status: 0 = ready, 1 = findings.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRIVATE_CONTEXT = REPO / ".local" / "PRIVATE_CONTEXT.local.md"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
PHONE_RE = re.compile(r"(?<![\w.])\+\d{1,3}[ -]?\d{4,5}[ -]?\d{4,6}(?!\d)")
PUBLIC_EMAILS = {"support@nexora-ai.co.in"}  # published on the product's own public website
ALLOWED_DOMAIN_SUFFIXES = (
    "example.com",
    "example.org",
    "example.net",
    ".example",
    ".test",
    ".invalid",
    ".localhost",
    "users.noreply.github.com",
    "anthropic.com",  # Co-Authored-By trailer: noreply@anthropic.com
)
BAD_PHRASES = ("pasted in chat", "exposed to chat", "shared in chat", "provided in chat")
SKIP_FILES = re.compile(r"(^|/)(uv\.lock|package-lock\.json|openapi\.json|schema\.d\.ts)$")
TEXT_SUFFIX_SKIP = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".pdf", ".zip"}


def is_allowed_email(email: str) -> bool:
    lowered = email.lower()
    if lowered in PUBLIC_EMAILS:
        return True
    domain = lowered.rsplit("@", 1)[1]
    if all(label.isdigit() for label in domain.split(".")):
        return True  # `user:pass@127.0.0.1` in a connection string is not an e-mail address
    return any(domain == s.lstrip(".") or domain.endswith(s if s.startswith(".") else "." + s) for s in ALLOWED_DOMAIN_SUFFIXES)


def parse_private_values(text: str) -> set[str]:
    """Extract the real e-mails, phone numbers and address text listed in the private context file."""
    values: set[str] = set(EMAIL_RE.findall(text)) | set(PHONE_RE.findall(text))
    for line in text.splitlines():
        match = re.match(r"^\s*-\s*<[A-Z_]*ADDRESS>\s*=\s*(.+?)(?:\s{2,}\(|\s*$)", line)
        if match and "@" not in match.group(1):
            values.add(match.group(1).strip())
    return {v for v in values if len(v) >= 6}


def find_personal_data(text: str, *, private_values: set[str] | frozenset[str] = frozenset()) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for email in EMAIL_RE.findall(text):
        if not is_allowed_email(email):
            findings.append(("personal-email", _mask(email)))
    for phone in PHONE_RE.findall(text):
        findings.append(("phone-number", _mask(phone)))
    for value in private_values:
        if value and value in text:
            findings.append(("known-private-value", f"<{len(value)} chars, value hidden>"))
    for phrase in BAD_PHRASES:
        if phrase in text.lower():
            findings.append(("unpublishable-wording", phrase))
    return findings


def _mask(value: str) -> str:
    return value[0] + "***" + value[-4:] if len(value) > 6 else "***"


# ───────────────────────────── workflows ─────────────────────────────

_USES_RE = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.MULTILINE)


def audit_workflow(text: str, name: str) -> list[str]:
    problems: list[str] = []
    if re.search(r"^\s*pull_request_target\s*:", text, re.MULTILINE) or "pull_request_target" in text.split("jobs:")[0]:
        problems.append(f"{name}: uses pull_request_target (runs untrusted fork code with repo privileges)")
    if re.search(r"^\s*workflow_run\s*:", text, re.MULTILINE):
        problems.append(f"{name}: uses workflow_run (privilege-escalation risk from fork PRs)")
    if not re.search(r"^permissions\s*:", text, re.MULTILINE):
        problems.append(f"{name}: no top-level `permissions:` (default token may be broader than needed)")
    elif re.search(r"^permissions\s*:\s*write-all", text, re.MULTILINE):
        problems.append(f"{name}: permissions: write-all")
    for ref in _USES_RE.findall(text):
        if ref.startswith(("./", "docker://")):
            continue
        if "@" not in ref or not re.fullmatch(r"[0-9a-f]{40}", ref.split("@", 1)[1]):
            problems.append(f"{name}: action `{ref}` is not pinned to a full commit SHA")
    checkouts = len(re.findall(r"uses:\s*actions/checkout@", text))
    if checkouts > len(re.findall(r"persist-credentials:\s*false", text)):
        problems.append(f"{name}: a checkout step lacks `persist-credentials: false`")
    for secret in set(re.findall(r"\$\{\{[^}]*?\bsecrets\.([A-Za-z0-9_]+)", text)):
        if secret != "GITHUB_TOKEN":
            problems.append(f"{name}: references secret `{secret}` — secrets must never be reachable from a public repo's CI")
    return problems


# ───────────────────────────── git helpers ─────────────────────────────


def git(*args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True, encoding="utf-8", errors="replace"
    ).stdout


def tracked_text_files() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for rel in git("ls-files", "-z").split("\0"):
        if not rel or SKIP_FILES.search(rel) or Path(rel).suffix.lower() in TEXT_SUFFIX_SKIP:
            continue
        try:
            data = (REPO / rel).read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:4096] or len(data) > 2_000_000:
            continue
        out.append((rel, data.decode("utf-8", errors="replace")))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tree", action="store_true")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--workflows", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    if not (args.tree or args.history or args.workflows or args.all):
        args.tree = args.workflows = True
    if args.all:
        args.tree = args.history = args.workflows = True

    private = parse_private_values(PRIVATE_CONTEXT.read_text(encoding="utf-8")) if PRIVATE_CONTEXT.exists() else set()
    failures: list[str] = []

    if args.tree:
        for rel, text in tracked_text_files():
            if rel == "scripts/prepublic_audit.py":
                continue  # this file defines the patterns
            failures += [f"{rel}: {rule} [{prev}]" for rule, prev in find_personal_data(text, private_values=private)]

    if args.workflows:
        for wf in sorted((REPO / ".github" / "workflows").glob("*.y*ml")):
            failures += audit_workflow(wf.read_text(encoding="utf-8"), f".github/workflows/{wf.name}")

    if args.history:
        emails = set(git("log", "--all", "--format=%ae%n%ce").split())
        failures += [f"history: commit author/committer e-mail {_mask(e)} is not a noreply address" for e in sorted(emails) if not is_allowed_email(e)]
        diff = git("log", "--all", "-p", "-U0", "--no-color", "--format=commit:%H")
        seen: set[tuple[str, str]] = set()
        for line in diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                for rule, prev in find_personal_data(line[1:], private_values=private):
                    seen.add((rule, prev))
        failures += [f"history: added lines contain {rule} [{prev}]" for rule, prev in sorted(seen)]

    scope = [n for n, on in (("tree", args.tree), ("history", args.history), ("workflows", args.workflows)) if on]
    if failures:
        print(f"✖ pre-public audit FAILED ({', '.join(scope)}): {len(failures)} finding(s)", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print(f"✔ pre-public audit clean ({', '.join(scope)}; {len(private)} private value(s) checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
