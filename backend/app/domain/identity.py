"""Canonical identity for companies and contacts (ADR-028): the same business or person must collapse to ONE record however
the sources spell it, so no e-mail and no provider credit is ever spent on a repeat. Pure functions, no I/O.

* A **company** is its registrable domain (``www.``, scheme, port, path, case and IDN spelling all disappear; a chain's
  branches share one domain, hence one company). Without a website it is name + city + country, normalised.
* A **contact** is its lower-cased e-mail. For *matching only* (never for sending) Gmail's dots and ``+tags`` are ignored.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

import tldextract

from app.domain.enums import SubscriberType
from app.integrations.gmail.mime import ADDRESS_RE

# The public-suffix list ships inside the package; never download it at runtime (no network at import or first use).
# Private suffixes (github.io, wixsite.com, blogspot.com …) are included on purpose: every business hosted on a shared
# platform is its OWN company, and treating "wixsite.com" as one registrable domain would merge thousands of them into one.
_EXTRACT = tldextract.TLDExtract(
    suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True
)

_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in", "ymail.com", "outlook.com",
        "hotmail.com", "hotmail.co.uk", "live.com", "msn.com", "icloud.com", "me.com", "aol.com", "proton.me",
        "protonmail.com", "gmx.com", "gmx.de", "gmx.net", "mail.com", "zoho.com", "yandex.com", "yandex.ru",
        "rediffmail.com", "web.de", "t-online.de", "orange.fr", "wanadoo.fr", "libero.it", "btinternet.com",
    }
)  # fmt: skip
DISPOSABLE_DOMAINS = frozenset(
    {
        "mailinator.com", "guerrillamail.com", "guerrillamail.net", "guerrillamail.org", "sharklasers.com",
        "10minutemail.com", "10minutemail.net", "tempmail.com", "temp-mail.org", "yopmail.com", "yopmail.net",
        "trashmail.com", "trashmail.net", "getnada.com", "dispostable.com", "maildrop.cc", "throwawaymail.com",
        "fakeinbox.com", "mailnesia.com", "mintemail.com", "spamgourmet.com", "tempinbox.com", "mohmal.com",
        "emailondeck.com", "burnermail.io", "mytemp.email", "tmpmail.org", "discard.email", "moakt.com",
    }
)  # fmt: skip
ROLE_LOCAL_PARTS = frozenset(
    {
        "info", "contact", "contactus", "hello", "hi", "office", "admin", "administrator", "support", "help",
        "helpdesk", "sales", "marketing", "team", "enquiries", "enquiry", "inquiries", "inquiry", "mail", "email",
        "webmaster", "postmaster", "hostmaster", "abuse", "noreply", "no-reply", "donotreply", "do-not-reply",
        "billing", "accounts", "accounting", "finance", "hr", "jobs", "careers", "recruiting", "recruitment",
        "press", "media", "pr", "reception", "frontdesk", "booking", "bookings", "reservations", "orders",
        "service", "services", "general", "feedback", "newsletter", "privacy", "legal", "security", "customerservice",
    }
)  # fmt: skip

_LEGAL_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|ltd|limited|plc|corp|corporation|co|company|gmbh|ag|sa|sarl|srl|bv|nv|pty|pvt|llp|lp|pllc|pc)\b\.?",
    re.IGNORECASE,
)
_NON_WORD = re.compile(r"[^a-z0-9]+")


def registrable_domain(url_or_host: str | None) -> str | None:
    """``https://WWW.Shop.Example.co.uk:8443/a?b#c`` → ``example.co.uk``. ``None`` for anything that has no real domain
    (IP addresses, single labels, empty input)."""
    if not url_or_host or not url_or_host.strip():
        return None
    raw = url_or_host.strip()
    host = urlsplit(raw if "//" in raw else f"//{raw}").hostname
    if not host:
        return None
    parts = _EXTRACT(host.rstrip("."))
    if not parts.domain or not parts.suffix:
        return None
    domain = f"{parts.domain}.{parts.suffix}".lower()
    try:
        return domain.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def website_origin(url_or_host: str | None) -> str | None:
    """The canonical homepage address of a site: ``https://example.com``. ``None`` when there is no usable domain."""
    domain = registrable_domain(url_or_host)
    return f"https://{domain}" if domain else None


def normalise_email(value: str | None) -> str | None:
    """Lower-cased, trimmed, syntactically valid address — or ``None``. Syntax only; deliverability is a separate question."""
    if not value:
        return None
    candidate = value.strip().lower().removeprefix("mailto:").strip("<> ")
    return candidate if ADDRESS_RE.fullmatch(candidate) else None


def email_match_key(value: str | None) -> str | None:
    """Identity for *matching* two spellings of one mailbox (never used to send): Gmail ignores dots and ``+tags``."""
    address = normalise_email(value)
    if address is None:
        return None
    local, domain = address.rsplit("@", 1)
    if domain in _GMAIL_DOMAINS:
        return f"{local.split('+', 1)[0].replace('.', '')}@gmail.com"
    return address


def email_domain(value: str | None) -> str | None:
    address = normalise_email(value)
    return address.rsplit("@", 1)[1] if address else None


def is_role_address(value: str | None) -> bool:
    """``info@``, ``sales@``, ``noreply@`` … — a shared mailbox, not a named person."""
    address = normalise_email(value)
    if address is None:
        return False
    local = address.rsplit("@", 1)[0].split("+", 1)[0]
    return (
        local in ROLE_LOCAL_PARTS
        or local.replace(".", "").replace("_", "").replace("-", "") in ROLE_LOCAL_PARTS
    )


def is_free_mail(value: str | None) -> bool:
    return email_domain(value) in FREE_MAIL_DOMAINS


def subscriber_type_for(value: str | None) -> SubscriberType:
    """Company or individual? A personal mailbox (free-mail, disposable) belongs to an individual; an address at a business's
    own domain is read as a company's. That is the usual B2B reading but not always the legal one — a sole trader at their
    own domain is an individual subscriber in the UK and EU — so countries where it matters get their own policy."""
    if normalise_email(value) is None:
        return SubscriberType.UNKNOWN
    if is_free_mail(value) or is_disposable(value):
        return SubscriberType.INDIVIDUAL
    return SubscriberType.CORPORATE


def is_disposable(value: str | None) -> bool:
    domain = email_domain(value)
    return domain is not None and (
        domain in DISPOSABLE_DOMAINS or registrable_domain(domain) in DISPOSABLE_DOMAINS
    )


def company_key(
    *, website: str | None, name: str | None, city: str | None = None, country: str | None = None
) -> str | None:
    """The dedupe key for a company: its registrable domain, else normalised name + city + country. ``None`` if neither exists."""
    domain = registrable_domain(website)
    if domain:
        return domain
    normalised = normalise_name(name)
    if not normalised:
        return None
    return f"name:{normalised}|{normalise_name(city) or ''}|{(country or '').strip().lower()[:2]}"


def normalise_name(value: str | None) -> str:
    """Accent-free, lower-case, punctuation-free, without legal suffixes (Inc, LLC, GmbH …): ``Café  Möller, LLC`` → ``cafe moller``."""
    if not value:
        return ""
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    folded = _LEGAL_SUFFIXES.sub(" ", folded)
    return _NON_WORD.sub(" ", folded).strip()
