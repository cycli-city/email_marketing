"""Redaction of credential-looking text before it is stored or shown.

Error text from vendors and libraries is persisted (job errors, provider state, dead letters) and displayed in the
dashboard, so it must never carry a credential — some APIs echo the offending key in their 401 message. Adapters
should not put secrets in exceptions in the first place; this is the safety net.
"""

from __future__ import annotations

import re

MAX_ERROR_CHARS = 2000

_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|"
        r"authorization|client[_-]?secret)\b\s*[=:]\s*(bearer\s+)?[^\s,;&\"']+"
    ),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._\-~+/]+=*"),
    re.compile(r"\b(?:gsk|sbp|ghp|gho|ghs|github_pat|sk|rk|xox[abp])[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
)


def scrub_error(message: str, *, known_secrets: tuple[str, ...] = ()) -> str:
    """Redact credential-looking substrings (and any ``known_secrets`` verbatim) and bound the length."""
    cleaned = message
    for secret in known_secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub(
            lambda m: f"{m.group(1)}=[redacted]" if m.lastindex else "[redacted]", cleaned
        )
    if len(cleaned) > MAX_ERROR_CHARS:
        cleaned = cleaned[:MAX_ERROR_CHARS] + "…[truncated]"
    return cleaned
