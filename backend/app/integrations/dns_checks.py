"""Our own e-mail checks, before any vendor credit is spent: does the address's domain accept mail at all?

Order of cheapness (docs/PROVIDERS.md §3): syntax → disposable/role (``app.domain.identity``) → **MX lookup (this module)** →
only then a verification vendor. A domain with no mail server, or a *null MX* (RFC 7505: the domain says it never receives
mail), can be rejected for free.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

import dns.asyncresolver
import dns.exception
import dns.resolver

MailStatus = Literal["mx", "implicit_mx", "null_mx", "no_mail_server", "dns_error"]
#: ``(domain, record_type)`` → the record texts, or raises ``dns.resolver.NXDOMAIN`` / ``NoAnswer`` / other ``dns.exception``.
Lookup = Callable[[str, str], Awaitable[list[str]]]


async def system_lookup(domain: str, record_type: str) -> list[str]:
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = 6.0
    answer = await resolver.resolve(domain, record_type)
    return [rdata.to_text() for rdata in answer]


async def mail_status(domain: str, lookup: Lookup = system_lookup) -> MailStatus:
    """Can this domain receive mail?  ``mx`` / ``implicit_mx`` (no MX but an A/AAAA record, which SMTP falls back to) are
    usable; ``null_mx`` and ``no_mail_server`` are free, certain rejections; ``dns_error`` means "could not tell" — never a
    reason to reject (the resolver may simply be down)."""
    name = domain.strip().strip(".").lower()
    if not name or "." not in name:
        return "no_mail_server"
    try:
        records = await lookup(name, "MX")
    except dns.resolver.NXDOMAIN:
        return "no_mail_server"
    except dns.resolver.NoAnswer:
        return await _implicit(name, lookup)
    except dns.exception.DNSException:
        return "dns_error"
    hosts = [r.split()[-1].rstrip(".") for r in records if r.split()]
    if not hosts or all(h == "" for h in hosts):
        return "null_mx"  # RFC 7505: a single MX of "." means "this domain does not accept mail"
    return "mx"


async def _implicit(name: str, lookup: Lookup) -> MailStatus:
    for record_type in ("A", "AAAA"):
        try:
            if await lookup(name, record_type):
                return "implicit_mx"
        except dns.resolver.NoAnswer:
            continue
        except dns.resolver.NXDOMAIN:
            return "no_mail_server"
        except dns.exception.DNSException:
            return "dns_error"
    return "no_mail_server"


def usable_for_mail(status: MailStatus) -> bool | None:
    """``True``/``False`` when the lookup is conclusive, ``None`` when it could not tell."""
    if status in ("mx", "implicit_mx"):
        return True
    if status in ("null_mx", "no_mail_server"):
        return False
    return None
