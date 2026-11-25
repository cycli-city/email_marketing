"""Provider results ↔ the JSON stored in the lookup ledger.

The ledger keeps *what a provider answered*, so a later identical question is answered without spending a credit. Decoders are
deliberately tolerant (missing keys take defaults, wrong types are dropped) so an answer written by an older version of the
code can never crash a run.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from app.providers.discovery import (
    ContactHit,
    DomainContacts,
    SearchHit,
    Verdict,
    VerificationOutcome,
)

_VERDICTS: frozenset[str] = frozenset({"deliverable", "undeliverable", "risky", "unknown"})


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _opt_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _opt_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _items(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


# ── web search ──


def encode_hits(hits: Sequence[SearchHit]) -> dict[str, Any]:
    return {"hits": [{"title": h.title, "url": h.url, "snippet": h.snippet} for h in hits]}


def decode_hits(data: dict[str, Any]) -> list[SearchHit]:
    return [
        SearchHit(
            title=_text(i.get("title")), url=_text(i.get("url")), snippet=_text(i.get("snippet"))
        )
        for i in _items(data.get("hits"))
        if _text(i.get("url"))
    ]


# ── contacts at a domain ──


def encode_domain_contacts(found: DomainContacts) -> dict[str, Any]:
    return {
        "domain": found.domain,
        "organization": found.organization,
        "pattern": found.pattern,
        "accept_all": found.accept_all,
        "contacts": [
            {
                "email": c.email,
                "type": c.type,
                "first_name": c.first_name,
                "last_name": c.last_name,
                "position": c.position,
                "department": c.department,
                "seniority": c.seniority,
                "confidence": c.confidence,
                "source_urls": list(c.source_urls),
                "vendor_verified": c.vendor_verified,
            }
            for c in found.contacts
        ],
    }


def decode_domain_contacts(data: dict[str, Any]) -> DomainContacts:
    contacts: list[ContactHit] = []
    for item in _items(data.get("contacts")):
        email = _text(item.get("email"))
        if not email:
            continue
        kind = item.get("type")
        confidence = item.get("confidence")
        urls = item.get("source_urls")
        contacts.append(
            ContactHit(
                email=email,
                type="generic" if kind == "generic" else "personal",
                first_name=_opt_text(item.get("first_name")),
                last_name=_opt_text(item.get("last_name")),
                position=_opt_text(item.get("position")),
                department=_opt_text(item.get("department")),
                seniority=_opt_text(item.get("seniority")),
                confidence=confidence
                if isinstance(confidence, int) and not isinstance(confidence, bool)
                else None,
                source_urls=tuple(u for u in urls if isinstance(u, str))
                if isinstance(urls, list)
                else (),
                vendor_verified=item.get("vendor_verified") is True,
            )
        )
    return DomainContacts(
        domain=_text(data.get("domain")),
        organization=_opt_text(data.get("organization")),
        pattern=_opt_text(data.get("pattern")),
        accept_all=data.get("accept_all") is True,
        contacts=tuple(contacts),
    )


# ── e-mail verification ──


def encode_outcome(outcome: VerificationOutcome) -> dict[str, Any]:
    return {
        "email": outcome.email,
        "verdict": outcome.verdict,
        "detail": outcome.detail,
        "is_disposable": outcome.is_disposable,
        "is_role": outcome.is_role,
        "is_catch_all": outcome.is_catch_all,
        "is_free_provider": outcome.is_free_provider,
    }


def decode_outcome(data: dict[str, Any]) -> VerificationOutcome:
    verdict = data.get("verdict")
    return VerificationOutcome(
        email=_text(data.get("email")),
        verdict=cast(Verdict, verdict if verdict in _VERDICTS else "unknown"),
        detail=_text(data.get("detail")),
        is_disposable=_opt_bool(data.get("is_disposable")),
        is_role=_opt_bool(data.get("is_role")),
        is_catch_all=_opt_bool(data.get("is_catch_all")),
        is_free_provider=_opt_bool(data.get("is_free_provider")),
    )
