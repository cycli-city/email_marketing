"""Fetch a public web page safely and politely (ADR-038).

Every request goes through the SSRF guard (:mod:`~app.integrations.web.ssrf`) and then connects to **the address that was
checked** — the URL handed to the HTTP client carries the IP, ``Host`` carries the name and TLS verifies the certificate
against the name (``sni_hostname``) — so DNS cannot change its answer between the check and the connection. Redirects are
followed by hand, each hop re-validated and re-resolved. On top of that:

* ``robots.txt`` is honoured (RFC 9309): a missing file allows everything, an unreachable one forbids everything, ``Crawl-delay``
  is respected (capped) and requests to one host are spaced out;
* responses are streamed and **capped in size** (decoded, so a compression bomb is cut off), time-limited, and limited to
  text content types;
* no cookies, no credentials, no environment proxies (a proxy would bypass the pinning), HTTPS certificates always verified.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
import structlog
from protego import Protego

from app.integrations.web.ssrf import (
    Resolver,
    Target,
    UnsafeUrlError,
    resolve_public,
    system_resolver,
    validate_url,
)

log = structlog.get_logger(__name__)

ROBOTS_AGENT = "aoi-outreach"
USER_AGENT = f"{ROBOTS_AGENT}/0.1"  # generic: carries no personal details (CLAUDE.md §1b)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_TEXT_TYPES = frozenset({"application/xhtml+xml", "application/xml", "text/xml"})
_ROBOTS_MAX_BYTES = 64_000


@dataclass(frozen=True, slots=True)
class FetchLimits:
    max_bytes: int = 1_500_000
    timeout_seconds: float = 15.0
    max_redirects: int = 4
    default_delay_seconds: float = 1.0  # minimum gap between requests to one host
    max_crawl_delay_seconds: float = 10.0  # a site asking for more than this is simply fetched less
    robots_ttl_seconds: float = 24 * 3600
    robots_failure_ttl_seconds: float = 600.0


class FetchError(Exception):
    """A page could not be fetched. ``reason`` is a short stable code; ``unsafe`` marks SSRF-guard refusals."""

    def __init__(self, reason: str, *, status: int | None = None, unsafe: bool = False) -> None:
        super().__init__(f"{reason} (HTTP {status})" if status else reason)
        self.reason = reason
        self.status = status
        self.unsafe = unsafe


@dataclass(frozen=True, slots=True)
class Page:
    url: str  # the final URL, after redirects
    status: int
    content_type: str
    text: str
    truncated: bool
    redirected_from: tuple[str, ...] = ()


@dataclass(slots=True)
class _Robots:
    parser: Protego | None  # None → no rules at all (allow everything)
    allow_all_override: bool
    expires_at: float
    delay: float | None = None
    blocked: bool = False  # robots.txt could not be read: assume everything is forbidden (for now)


@dataclass(slots=True)
class _Hop:
    status: int
    headers: httpx.Headers
    body: bytes
    truncated: bool


def build_crawler_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """A client that never follows redirects, never trusts proxy environment variables and keeps no cookies."""
    return httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        verify=True,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
        headers={"User-Agent": USER_AGENT},
    )


class SafeFetcher:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        resolver: Resolver = system_resolver,
        limits: FetchLimits | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._limits = limits or FetchLimits()
        self._sleep = sleep
        self._monotonic = monotonic
        self._robots: dict[str, _Robots] = {}
        self._next_slot: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ───────────────────────────── public API ─────────────────────────────

    async def fetch(self, url: str, *, respect_robots: bool = True) -> Page:
        try:
            target = validate_url(url)
        except UnsafeUrlError as exc:
            raise FetchError(exc.reason, unsafe=True) from exc
        redirected: list[str] = []
        for _ in range(self._limits.max_redirects + 1):
            if respect_robots and not await self._allowed(target):
                raise FetchError("blocked_by_robots")
            hop = await self._hop(target, max_bytes=self._limits.max_bytes, politely=True)
            if hop.status in _REDIRECT_STATUSES:
                target = self._next_target(target, hop)
                redirected.append(target.url)
                continue
            return self._page(target, hop, tuple(redirected))
        raise FetchError("too_many_redirects")

    # ───────────────────────────── one request ─────────────────────────────

    async def _hop(self, target: Target, *, max_bytes: int, politely: bool) -> _Hop:
        try:
            addresses = await resolve_public(target, self._resolver)
        except UnsafeUrlError as exc:
            raise FetchError(exc.reason, unsafe=True) from exc
        if politely:
            await self._wait_turn(target.host)
        pinned = httpx.URL(target.url).copy_with(host=addresses[0])
        default_port = 443 if target.scheme == "https" else 80
        host_header = target.host if target.port == default_port else f"{target.host}:{target.port}"
        extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
        try:
            async with self._client.stream(
                "GET",
                pinned,
                headers={
                    "Host": host_header,
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1",
                },
                extensions=extensions,
                timeout=self._limits.timeout_seconds,
            ) as response:
                body, truncated = await self._read_capped(response, max_bytes)
                self._client.cookies.clear()
                return _Hop(response.status_code, response.headers, body, truncated)
        except httpx.TimeoutException as exc:
            raise FetchError("timeout") from exc
        except httpx.HTTPError as exc:
            raise FetchError("network_error") from exc

    @staticmethod
    async def _read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
        """Read the DECODED body up to ``max_bytes`` and stop: a gzip bomb is cut off at the cap, not expanded."""
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            room = max_bytes - size
            if len(chunk) > room:
                chunks.append(chunk[:room])
                return b"".join(chunks), True
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks), False

    def _next_target(self, current: Target, hop: _Hop) -> Target:
        location = hop.headers.get("location")
        if not location:
            raise FetchError("redirect_without_location", status=hop.status)
        try:
            return validate_url(urljoin(current.url, location))
        except UnsafeUrlError as exc:  # a redirect INTO an internal address is exactly the attack
            raise FetchError(exc.reason, unsafe=True) from exc

    @staticmethod
    def _page(target: Target, hop: _Hop, redirected: tuple[str, ...]) -> Page:
        if not 200 <= hop.status < 300:
            raise FetchError("http_error", status=hop.status)
        content_type = hop.headers.get("content-type", "text/html").split(";")[0].strip().lower()
        if not (content_type.startswith("text/") or content_type in _TEXT_TYPES):
            raise FetchError("unsupported_content_type")
        encoding = httpx.Response(200, headers=hop.headers).charset_encoding or "utf-8"
        try:
            text = hop.body.decode(encoding, errors="replace")
        except LookupError:
            text = hop.body.decode("utf-8", errors="replace")
        return Page(
            url=target.url,
            status=hop.status,
            content_type=content_type,
            text=text,
            truncated=hop.truncated,
            redirected_from=redirected,
        )

    # ───────────────────────────── politeness ─────────────────────────────

    async def _wait_turn(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            delay = self._delay_for(host)
            wait = self._next_slot.get(host, 0.0) - self._monotonic()
            if wait > 0:
                await self._sleep(wait)
            self._next_slot[host] = self._monotonic() + delay

    def _delay_for(self, host: str) -> float:
        for origin, rules in self._robots.items():
            if origin.split("://", 1)[-1].split(":", 1)[0] == host and rules.delay is not None:
                return min(
                    max(rules.delay, self._limits.default_delay_seconds),
                    self._limits.max_crawl_delay_seconds,
                )
        return self._limits.default_delay_seconds

    # ───────────────────────────── robots.txt ─────────────────────────────

    async def _allowed(self, target: Target) -> bool:
        rules = await self._rules_for(target)
        if rules.blocked:
            return False
        if rules.parser is None or rules.allow_all_override:
            return True
        return bool(rules.parser.can_fetch(target.url, ROBOTS_AGENT))

    async def _rules_for(self, target: Target) -> _Robots:
        cached = self._robots.get(target.origin)
        if cached is not None and cached.expires_at > self._monotonic():
            return cached
        rules = await self._load_robots(target)
        self._robots[target.origin] = rules
        return rules

    async def _load_robots(self, target: Target) -> _Robots:
        now = self._monotonic()
        failure = _Robots(None, False, now + self._limits.robots_failure_ttl_seconds, blocked=True)
        current = validate_url(f"{target.origin}/robots.txt")
        for hops in range(self._limits.max_redirects + 1):
            try:
                hop = await self._hop(current, max_bytes=_ROBOTS_MAX_BYTES, politely=False)
            except FetchError as exc:
                if exc.unsafe and hops == 0:
                    raise  # the site's OWN address is refused: that is the answer, not "robots unreadable"
                log.info("fetch.robots_unreadable", host=target.host, reason=exc.reason)
                return failure
            if hop.status not in _REDIRECT_STATUSES:
                return self._interpret_robots(hop, now) or failure
            try:
                current = self._next_target(current, hop)
            except (
                FetchError
            ) as exc:  # a robots.txt that redirects somewhere unsafe is simply unreadable
                log.info("fetch.robots_unreadable", host=target.host, reason=exc.reason)
                return failure
        return failure

    def _interpret_robots(self, hop: _Hop, now: float) -> _Robots | None:
        ttl = now + self._limits.robots_ttl_seconds
        if 200 <= hop.status < 300:
            parser = Protego.parse(hop.body.decode("utf-8", errors="replace"))
            delay = parser.crawl_delay(ROBOTS_AGENT)
            return _Robots(parser, False, ttl, delay=float(delay) if delay else None)
        if 400 <= hop.status < 500 and hop.status != 429:
            return _Robots(None, True, ttl)  # RFC 9309: no robots.txt → no restrictions
        return None  # 5xx / 429 / anything odd → unreachable → assume forbidden for now
