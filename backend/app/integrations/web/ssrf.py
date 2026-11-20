"""SSRF guard (CLAUDE.md §1b): decide whether a URL we were given may be fetched at all.

The crawler reads websites named by third parties (business directories, search results, scraped pages). Any of those
can name ``http://169.254.169.254/`` (cloud metadata), ``http://localhost:5432/`` or a hostname whose DNS answer is an
internal address — and a plain ``httpx.get(url)`` would happily fetch it from *our* network position. Rules, all tested:

* only ``http``/``https`` on ports 80/443, no credentials in the URL, bounded length, no fragments;
* the **resolved** addresses decide, not the spelling: ``2130706433``, ``0x7f.1``, ``127.1`` and ``localhost`` all reach
  loopback and are caught after resolution; numeric-looking hostnames that are not canonical IPs are refused outright;
* **every** address a name resolves to must be public (a name answering both a public and a private address is refused —
  that is the shape of a DNS-rebinding attempt), and the caller then connects to the address that was checked
  (:mod:`~app.integrations.web.fetcher` pins it), so the answer cannot change between check and use;
* IPv6 forms that smuggle an IPv4 address (IPv4-mapped, 6to4, Teredo, NAT64) are judged by the IPv4 inside;
* reserved/local-use name suffixes are refused before any lookup is made.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443})
MAX_URL_LENGTH = 2048
_MAX_HOSTNAME_LENGTH = 253
_BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home",
    ".corp",
    ".private",
    ".home.arpa",
    ".onion",
    ".test",
    ".invalid",
    ".example",
)
_BLOCKED_NAMES = frozenset({"localhost", "metadata", "metadata.google.internal"})
_NUMERIC_LOOKING = re.compile(r"^[0-9a-fx.]+$", re.IGNORECASE)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")


class UnsafeUrlError(ValueError):
    """The URL must not be fetched. ``reason`` is a short stable code (used in logs and tests)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Target:
    """A URL that passed the static checks (nothing has been resolved or fetched yet)."""

    url: str  # normalised, fragment removed
    scheme: str
    host: str  # lower-case, IDNA (punycode) for names; the canonical text of an IP literal
    port: int
    is_ip_literal: bool

    @property
    def origin(self) -> str:
        default = 443 if self.scheme == "https" else 80
        return f"{self.scheme}://{self.host}" + ("" if self.port == default else f":{self.port}")


Resolver = Callable[[str, int], Awaitable[list[str]]]


# ───────────────────────────── addresses ─────────────────────────────


def is_public_ip(value: str) -> bool:
    """True only for addresses that are globally routable unicast. Never raises."""
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        return _is_public_v6(ip)
    return ip.is_global and not ip.is_multicast


def _is_public_v6(ip: ipaddress.IPv6Address) -> bool:
    if ip.ipv4_mapped is not None:
        return is_public_ip(str(ip.ipv4_mapped))
    if ip.sixtofour is not None:
        return is_public_ip(str(ip.sixtofour))
    if ip.teredo is not None:
        server, client = ip.teredo
        return is_public_ip(str(server)) and is_public_ip(str(client))
    if ip in _NAT64_LOCAL:
        return False
    if ip in _NAT64:
        return is_public_ip(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
    return ip.is_global and not ip.is_multicast


# ───────────────────────────── URLs ─────────────────────────────


def validate_url(raw: str) -> Target:
    """Static checks only (no DNS). Raises :class:`UnsafeUrlError`."""
    if not isinstance(raw, str) or not raw.strip():
        raise UnsafeUrlError("empty_url")
    candidate = raw.strip()
    if len(candidate) > MAX_URL_LENGTH:
        raise UnsafeUrlError("url_too_long")
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in candidate) or "\\" in candidate:
        raise UnsafeUrlError("illegal_characters")
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("malformed_url", str(exc)) from exc
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError("scheme_not_allowed", scheme or "none")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeUrlError("credentials_in_url")
    if not parts.hostname:
        raise UnsafeUrlError("no_host")
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    if effective_port not in ALLOWED_PORTS:
        raise UnsafeUrlError("port_not_allowed", str(effective_port))
    host, is_ip = _clean_host(parts.hostname)
    return Target(
        url=_rebuild(parts, scheme, host, port, is_ip),
        scheme=scheme,
        host=host,
        port=effective_port,
        is_ip_literal=is_ip,
    )


def _clean_host(hostname: str) -> tuple[str, bool]:
    host = hostname.strip().rstrip(".").lower()
    if not host:
        raise UnsafeUrlError("no_host")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_ip(str(literal)):
            raise UnsafeUrlError("address_not_public", str(literal))
        return str(literal), True
    if _NUMERIC_LOOKING.fullmatch(host):
        # 2130706433, 0x7f.1, 127.1 ... — spellings of an IP that are not canonical. Refuse rather than interpret.
        raise UnsafeUrlError("numeric_hostname", host)
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeUrlError("bad_hostname", host) from exc
    if len(ascii_host) > _MAX_HOSTNAME_LENGTH or ".." in ascii_host or "." not in ascii_host:
        raise UnsafeUrlError("bad_hostname", ascii_host)
    if ascii_host in _BLOCKED_NAMES or ascii_host.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeUrlError("reserved_hostname", ascii_host)
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", ascii_host):
        raise UnsafeUrlError("bad_hostname", ascii_host)
    return ascii_host, False


def _rebuild(parts: SplitResult, scheme: str, host: str, port: int | None, is_ip: bool) -> str:
    shown = f"[{host}]" if is_ip and ":" in host else host
    netloc = shown if port is None else f"{shown}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


# ───────────────────────────── resolution ─────────────────────────────


async def system_resolver(host: str, port: int) -> list[str]:
    """Ask the operating system, without blocking the event loop."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError("dns_failure", host) from exc
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


async def resolve_public(target: Target, resolver: Resolver = system_resolver) -> list[str]:
    """The addresses to connect to. Refuses unless EVERY answer is public (a mixed answer is a rebinding attempt)."""
    if target.is_ip_literal:
        return [target.host]
    addresses = await resolver(target.host, target.port)
    if not addresses:
        raise UnsafeUrlError("dns_failure", target.host)
    bad = [a for a in addresses if not is_public_ip(a)]
    if bad:
        raise UnsafeUrlError("resolves_to_non_public_address", f"{target.host} -> {bad[0]}")
    return addresses
