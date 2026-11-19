#!/usr/bin/env python3
"""Repository secret scanner (stdlib only) — blocks credentials from reaching git, CI logs or the browser.

Modes (combine freely):
    --staged            scan lines being committed (pre-commit hook)
    --tree              scan every tracked file
    --history           scan every added line in the whole git history
    --path DIR [...]    scan arbitrary files, e.g. the built frontend bundle: --path frontend/.next/static
    --forbid-names      (with --path) also fail if server-only variable NAMES appear in the scanned files,
                        which means a server-only module leaked into a browser bundle

Detection layers:
    1. well-known token formats (GitHub, Groq, OpenAI/Anthropic, Google, AWS, Slack, Telegram, Supabase,
       private keys, Fernet keys, JWTs — a Supabase *service_role* JWT is always fatal)
    2. the EXACT secret values stored in `.local/SECRETS.local.md` (strongest guard: it knows your real secrets)
    3. forbidden tracked file names (.env, *.pem, client_secret*.json, .local/ …)

A line containing `secretscan:allow` is ignored. Findings never print the full secret.
Exit status: 0 clean, 1 findings.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOCAL_SECRETS = REPO / ".local" / "SECRETS.local.md"
ALLOW_MARKER = "secretscan:allow"
MAX_FILE_BYTES = 5_000_000

# (rule name, compiled pattern)
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("github-fine-grained-token", re.compile(r"github_pat_[A-Za-z0-9_]{40,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("groq-api-key", re.compile(r"\bgsk_[A-Za-z0-9]{30,}\b")),
    ("anthropic-api-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-style-api-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("google-oauth-client-secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}")),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("telegram-bot-token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("supabase-secret-key", re.compile(r"\bsb_secret_[A-Za-z0-9_-]{20,}")),
    # Account-level Management API token: full control of every project on the account.
    ("supabase-access-token", re.compile(r"\bsbp_[0-9a-f]{40}\b")),
    ("private-key-block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY")),
    ("fernet-key-assignment", re.compile(r"ENCRYPTION_KEY\s*[=:]\s*['\"]?[A-Za-z0-9_-]{43}=")),
    (
        "hardcoded-credential-in-url",
        re.compile(r"[a-z][a-z0-9+.-]*://[^\s:/@]{2,}:[^\s:/@<>{}$]{6,}@(?!localhost|127\.0\.0\.1)[\w.-]+"),
    ),
]
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")

FORBIDDEN_FILENAMES = re.compile(
    r"(^|/)(\.env(\..+)?|.*\.pem|.*\.p12|.*\.pfx|id_rsa|id_ed25519|client_secret.*\.json|"
    r"service-account.*\.json|credentials.*\.json|.*\.local\.md)$"
)
FORBIDDEN_DIRS = (".local/",)
ALLOWED_FILENAMES = {".env.example", "frontend/.env.example", "backend/.env.example"}

# Names that must never appear in code shipped to the browser.
SERVER_ONLY_NAMES = (
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_JWT_SECRET",
    "ENCRYPTION_KEY",
    "JWT_SECRET",
    "GOOGLE_CLIENT_SECRET",
    "GROQ_API_KEY",
    "HUNTER_API_KEY",
    "FIRECRAWL_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "DATABASE_URL",
    "REDIS_URL",
)

SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".pgdata", ".mypy_cache", ".ruff_cache"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".pdf", ".zip", ".lock"}


@dataclass(frozen=True)
class Finding:
    where: str
    line: int
    rule: str
    preview: str

    def __str__(self) -> str:
        return f"{self.where}:{self.line}: {self.rule} [{self.preview}]"


def redact(value: str) -> str:
    return f"{value[:4]}…({len(value)} chars)"


# ───────────────────────────── detection ─────────────────────────────


def jwt_role(token: str) -> str | None:
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, IndexError, json.JSONDecodeError):
        return None
    role = data.get("role") if isinstance(data, dict) else None
    return role if isinstance(role, str) else "unknown"


def load_known_secret_values(path: Path = LOCAL_SECRETS) -> set[str]:
    """Extract the real secret values the owner stored locally, so we can detect them anywhere."""
    if not path.exists():
        return set()
    values: set[str] = set()
    key_re = re.compile(r"(token|key|secret|password|passwd|pass)\b", re.IGNORECASE)
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip().lstrip("-* ").strip()
        if "NOT YET PROVIDED" in line or not line:
            continue
        match = re.match(r"^([A-Za-z0-9_ ./()-]+?)\s*[:=]\s*(\S+)\s*$", line)
        if not match:
            continue
        name, value = match.groups()
        if re.search(r"anon|publishable", name, re.IGNORECASE):
            continue  # public by design: it legitimately ships in the browser bundle
        if key_re.search(name) and len(value) >= 8 and not value.startswith(("http", "<", "(")):
            values.add(value)
    return values


def scan_line(
    where: str,
    lineno: int,
    line: str,
    *,
    known: set[str],
    allow_anon_jwt: bool = False,
) -> Iterator[Finding]:
    if ALLOW_MARKER in line:
        return
    for rule, pattern in PATTERNS:
        for m in pattern.finditer(line):
            yield Finding(where, lineno, rule, redact(m.group(0)))
    for m in JWT_RE.finditer(line):
        role = jwt_role(m.group(0))
        if role == "service_role":
            yield Finding(where, lineno, "supabase-service-role-jwt", redact(m.group(0)))
        elif not (allow_anon_jwt and role == "anon"):
            yield Finding(where, lineno, f"jwt(role={role})", redact(m.group(0)))
    for secret in known:
        if secret in line:
            # Reveal nothing but the length: a short password's first characters are a large part of it.
            yield Finding(where, lineno, "known-local-secret-value", f"<{len(secret)} chars, value hidden>")


def scan_text(where: str, text: str, **kw: object) -> list[Finding]:
    known = kw.pop("known", set())
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        findings.extend(scan_line(where, lineno, line, known=known, **kw))  # type: ignore[arg-type]
    return findings


def check_filename(rel: str) -> Finding | None:
    rel = rel.replace("\\", "/")
    if rel in ALLOWED_FILENAMES or rel.endswith(".env.example"):
        return None
    if rel.startswith(FORBIDDEN_DIRS) or FORBIDDEN_FILENAMES.search(rel):
        return Finding(rel, 0, "forbidden-file-tracked", rel)
    return None


# ───────────────────────────── sources ─────────────────────────────


def git(*args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True, encoding="utf-8", errors="replace"
    ).stdout


def parse_diff_added_lines(diff: str) -> Iterator[tuple[str, int, str, str]]:
    """Yield (commit, file, lineno, text) for every added line of a unified diff (-U0)."""
    commit, current, lineno = "", "", 0
    for raw in diff.splitlines():
        if raw.startswith("commit:"):
            commit = raw[7:15]
        elif raw.startswith("+++ b/"):
            current = raw[6:]
        elif raw.startswith("+++ "):
            current = ""
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            lineno = int(m.group(1)) - 1 if m else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            lineno += 1
            if current:
                yield commit, current, lineno, raw[1:]


def scan_diff(diff: str, *, known: set[str], label_commit: bool) -> list[Finding]:
    findings: list[Finding] = []
    for commit, path, lineno, text in parse_diff_added_lines(diff):
        where = f"{commit}:{path}" if label_commit and commit else path
        findings.extend(scan_line(where, lineno, text, known=known))
        name_finding = check_filename(path) if lineno == 1 else None
        if name_finding:
            findings.append(Finding(where, 0, name_finding.rule, name_finding.preview))
    return findings


def iter_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def read_text_file(path: Path) -> str | None:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return None
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


def scan_paths(
    paths: Iterable[Path], *, known: set[str], forbid_names: bool, allow_anon_jwt: bool
) -> list[Finding]:
    findings: list[Finding] = []
    for root in paths:
        for file in iter_files(root):
            text = read_text_file(file)
            if text is None:
                continue
            where = str(file)
            findings.extend(scan_text(where, text, known=known, allow_anon_jwt=allow_anon_jwt))
            if forbid_names:
                for name in SERVER_ONLY_NAMES:
                    if name in text:
                        findings.append(Finding(where, 0, "server-only-name-in-client-code", name))
    return findings


def scan_tree(*, known: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    for rel in git("ls-files", "-z").split("\0"):
        if not rel:
            continue
        name_finding = check_filename(rel)
        if name_finding:
            findings.append(name_finding)
        text = read_text_file(REPO / rel)
        if text is not None:
            findings.extend(scan_text(rel, text, known=known))
    return findings


# ───────────────────────────── CLI ─────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--tree", action="store_true")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--path", nargs="+", type=Path, default=[])
    parser.add_argument("--forbid-names", action="store_true")
    parser.add_argument("--allow-anon-jwt", action="store_true", help="permit Supabase *anon* JWTs (public by design)")
    args = parser.parse_args(argv)
    if not (args.staged or args.tree or args.history or args.path):
        parser.error("choose at least one of --staged --tree --history --path")

    known = load_known_secret_values()
    findings: list[Finding] = []

    if args.staged:
        diff = git("diff", "--cached", "-U0", "--no-color", "--diff-filter=ACMR")
        findings += scan_diff(diff, known=known, label_commit=False)
        for rel in git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines():
            f = check_filename(rel)
            if f:
                findings.append(f)
    if args.tree:
        findings += scan_tree(known=known)
    if args.history:
        diff = git("log", "--all", "-p", "-U0", "--no-color", "--format=commit:%H")
        findings += scan_diff(diff, known=known, label_commit=True)
    if args.path:
        findings += scan_paths(
            args.path, known=known, forbid_names=args.forbid_names, allow_anon_jwt=args.allow_anon_jwt
        )

    unique = sorted(set(findings), key=lambda f: (f.where, f.line, f.rule))
    if unique:
        print(f"✖ secret scan FAILED: {len(unique)} finding(s)", file=sys.stderr)
        for f in unique:
            print(f"  {f}", file=sys.stderr)
        print(
            "\nRemove the secret (rotate it if it was ever pushed). Put real values only in .local/ or your host's secret store.",
            file=sys.stderr,
        )
        return 1
    scanned = [m for m, on in (("staged", args.staged), ("tree", args.tree), ("history", args.history)) if on]
    scanned += [str(p) for p in args.path]
    print(f"✔ secret scan clean ({', '.join(scanned)}; {len(known)} local secret value(s) checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
