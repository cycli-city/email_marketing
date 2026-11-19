"""Client address and scheme resolution behind reverse proxies — without trusting spoofable headers.

``X-Forwarded-For`` is appended to by every proxy, so only the entries added by OUR proxies can be
trusted: with ``trusted_proxies = N`` the real client is the N-th entry from the RIGHT. Anything a client
puts at the left of the header is ignored. If the header is shorter than expected (e.g. a request that
bypassed the proxy) we fall back to the TCP peer address, which cannot be forged.
"""

from __future__ import annotations

import ipaddress

UNKNOWN_IP = "unknown"


def _valid_ip(candidate: str) -> str | None:
    try:
        return str(ipaddress.ip_address(candidate.strip().strip("[]")))
    except ValueError:
        return None


def resolve_client_ip(peer: str | None, forwarded_for: str | None, trusted_proxies: int) -> str:
    peer_ip = _valid_ip(peer) if peer else None
    fallback = peer_ip or UNKNOWN_IP
    if trusted_proxies <= 0 or not forwarded_for:
        return fallback
    entries = [part.strip() for part in forwarded_for.split(",") if part.strip()]
    if len(entries) < trusted_proxies:
        return fallback
    return _valid_ip(entries[-trusted_proxies]) or fallback


def resolve_scheme(scope_scheme: str, forwarded_proto: str | None, trusted_proxies: int) -> str:
    """The scheme the *client* used. Only believed when a trusted proxy is configured."""
    if trusted_proxies > 0 and forwarded_proto:
        last = forwarded_proto.split(",")[-1].strip().lower()
        if last in {"http", "https"}:
            return last
    return scope_scheme
