"""Reading published contact details out of a company's own web pages. Pure: takes HTML text, returns facts.

Only addresses the site **itself publishes** are returned (a ``mailto:`` link, text a visitor can read, a Cloudflare-protected
address a browser would reveal, the ``email`` of a JSON-LD block) — each with the page it came from, because provenance is what
makes an address *published* rather than guessed (REQUIREMENTS §3: a guessed address is never verified, never sent to). Nothing
here invents an address from a name and a pattern.

The HTML is untrusted: parsing is bounded, scripts/styles/comments are not visible text, and every candidate address goes through
the same normalisation and plausibility filters (no image-file names that happen to contain an at-sign, no template placeholders such as "you at yourdomain").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from app.domain.identity import normalise_email, registrable_domain
from app.integrations.web.ssrf import UnsafeUrlError, validate_url

MAX_HTML_CHARS = 1_500_000
MAX_JSONLD_CHARS = 200_000
_TEXT_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_OBFUSCATED = re.compile(
    r"([A-Za-z0-9._%+-]+)\s*[\[\(\{]\s*(?:at|AT)\s*[\]\)\}]\s*([A-Za-z0-9-]+(?:\s*[\[\(\{]\s*(?:dot|DOT)\s*[\]\)\}]\s*[A-Za-z0-9-]+)+)"
)
_DOT = re.compile(r"\s*[\[\(\{]\s*(?:dot|DOT)\s*[\]\)\}]\s*")
_FILE_TLDS = frozenset(
    {
        "png",
        "jpg",
        "jpeg",
        "gif",
        "svg",
        "webp",
        "css",
        "js",
        "ico",
        "woff",
        "woff2",
        "pdf",
        "zip",
        "mp4",
        "avif",
        "map",
    }
)
_PLACEHOLDER_DOMAINS = frozenset(
    {
        "example.com", "example.org", "example.net", "yourdomain.com", "yoursite.com", "yourcompany.com", "domain.com",
        "email.com", "mysite.com", "website.com", "company.com", "sentry.io", "sentry-next.wixpress.com", "wixpress.com",
    }
)  # fmt: skip
_PLACEHOLDER_LOCALS = frozenset(
    {
        "your",
        "you",
        "name",
        "yourname",
        "email",
        "user",
        "username",
        "test",
        "john.doe",
        "jane.doe",
        "example",
    }
)
_METHOD_RANK = {"mailto": 0, "cloudflare": 1, "jsonld": 2, "text": 3, "obfuscated": 4}

_PAGE_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("contact", 10), ("kontakt", 10), ("contacto", 10), ("impressum", 9), ("get-in-touch", 9), ("reach-us", 9),
    ("write-to-us", 8), ("team", 7), ("staff", 7), ("people", 6), ("doctors", 7), ("providers", 6), ("our-team", 8),
    ("meet", 5), ("leadership", 6), ("about", 5), ("who-we-are", 5), ("practice", 3), ("locations", 3),
)  # fmt: skip
_SKIP_EXTENSIONS = (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".zip",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".mp4",
    ".mp3",
    ".css",
    ".js",
    ".xml",
)


@dataclass(frozen=True, slots=True)
class FoundEmail:
    address: str
    page_url: str
    method: str  # mailto | cloudflare | jsonld | text | obfuscated
    same_site: bool  # the address is at the site's own domain (the strongest signal that it belongs to this business)
    label: str | None  # link text, when the address came from a link


# ───────────────────────────── e-mail addresses ─────────────────────────────


def extract_emails(html: str, page_url: str) -> list[FoundEmail]:
    """Every plausible address the page publishes, best method first, one entry per address."""
    soup = BeautifulSoup(html[:MAX_HTML_CHARS], "lxml")
    site = registrable_domain(page_url)
    found: dict[str, FoundEmail] = {}

    def add(raw: str, method: str, label: str | None = None) -> None:
        address = _plausible(raw)
        if address is None:
            return
        entry = FoundEmail(
            address=address,
            page_url=page_url,
            method=method,
            same_site=bool(site) and registrable_domain(address.rsplit("@", 1)[1]) == site,
            label=label,
        )
        current = found.get(address)
        if current is None or _METHOD_RANK[method] < _METHOD_RANK[current.method]:
            found[address] = entry

    for link in soup.select("a[href]"):
        href = str(link.get("href", ""))
        if href.lower().startswith("mailto:"):
            label = " ".join(link.get_text(" ").split())[:80] or None
            for part in unquote(href[7:].split("?", 1)[0]).split(","):
                add(part, "mailto", label)
    for node in soup.select("[data-cfemail]"):
        decoded = _decode_cloudflare(str(node.get("data-cfemail", "")))
        if decoded:
            add(decoded, "cloudflare", " ".join(node.get_text(" ").split())[:80] or None)
    for script in soup.select('script[type="application/ld+json"]'):
        for value in _jsonld_emails(script.string or ""):
            add(value, "jsonld")

    for hidden in soup(["script", "style", "noscript", "template"]):
        hidden.decompose()
    visible = soup.get_text(" ")
    for match in _TEXT_EMAIL.finditer(visible):
        add(match.group(0), "text")
    for match in _OBFUSCATED.finditer(visible):
        add(f"{match.group(1)}@{_DOT.sub('.', match.group(2))}".replace(" ", ""), "obfuscated")

    return sorted(
        found.values(), key=lambda e: (_METHOD_RANK[e.method], not e.same_site, e.address)
    )


def _plausible(raw: str) -> str | None:
    address = normalise_email(raw.strip().strip(".,;:()[]<>\"'"))
    if address is None:
        return None
    local, domain = address.rsplit("@", 1)
    if (
        domain.rsplit(".", 1)[-1] in _FILE_TLDS
        or domain in _PLACEHOLDER_DOMAINS
        or local in _PLACEHOLDER_LOCALS
    ):
        return None
    if len(domain.rsplit(".", 1)[-1]) < 2 or re.fullmatch(
        r"[0-9a-f]{24,}", local
    ):  # a tracking token, not a mailbox
        return None
    return address


def _decode_cloudflare(encoded: str) -> str | None:
    """Cloudflare's "email protection": hex bytes XOR-ed with the first byte. A browser shows the real address; so do we."""
    try:
        raw = bytes.fromhex(encoded.strip())
    except ValueError:
        return None
    if len(raw) < 4:
        return None
    key = raw[0]
    try:
        return bytes(b ^ key for b in raw[1:]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def _jsonld_emails(blob: str) -> list[str]:
    if not blob or len(blob) > MAX_JSONLD_CHARS:
        return []
    try:
        data = json.loads(blob)
    except ValueError:
        return []
    found: list[str] = []

    def walk(node: object, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "email" and isinstance(value, str):
                    found.append(value)
                else:
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:200]:
                walk(item, depth + 1)

    walk(data)
    return found


# ───────────────────────────── which pages to read next ─────────────────────────────


def candidate_pages(html: str, page_url: str, *, limit: int = 5) -> list[str]:
    """Same-site pages likely to hold contact details (contact, about, team …), most promising first. Off-site, unsafe,
    non-HTML and duplicate links are never returned."""
    site = registrable_domain(page_url)
    if site is None:
        return []
    soup = BeautifulSoup(html[:MAX_HTML_CHARS], "lxml")
    scored: dict[str, int] = {}
    for link in soup.select("a[href]"):
        score = _page_score(link, page_url, site)
        if score is None:
            continue
        url = _canonical(urljoin(page_url, str(link.get("href", "")).strip()))
        scored[url] = max(score, scored.get(url, 0))
    here = _canonical(page_url)
    ranked = sorted((u for u in scored if u != here), key=lambda u: (-scored[u], len(u), u))
    return ranked[:limit]


def _page_score(link: Tag, page_url: str, site: str) -> int | None:
    href = str(link.get("href", "")).strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:", "sms:")):
        return None
    absolute = urljoin(page_url, href)
    try:
        target = validate_url(absolute)
    except UnsafeUrlError:
        return None
    if registrable_domain(target.host) != site or urlsplit(target.url).path.lower().endswith(
        _SKIP_EXTENSIONS
    ):
        return None
    haystack = f"{urlsplit(target.url).path.lower()} {' '.join(link.get_text(' ').split()).lower()[:80]}".replace(
        "_", "-"
    ).replace(" ", "-")
    scores = [weight for keyword, weight in _PAGE_KEYWORDS if keyword in haystack]
    return max(scores) if scores else None


def _canonical(url: str) -> str:
    bare, _ = urldefrag(url)
    return bare.rstrip("/") or bare
