"""Which web-search result is *this business's own website*? (pure rules, no I/O)

Search engines return directories, review sites, social profiles and the occasional lookalike before — or instead of — the
company's own site. Mailing the wrong business, or a directory's contact address, is exactly the kind of mistake this project
must never make, so two independent checks stand between a search result and a company record:

1. :func:`choose_website` — a result is only a *candidate* if it is not a known aggregator and its **domain name** carries the
   business's distinctive name words (or, failing that, the page title carries all of them *and* the city).
2. :func:`confirms_identity` — after the candidate page is fetched, its own text must contain the business's full name or its
   phone number. A candidate that does not confirm is thrown away.

Search results are untrusted data: only their *URL host, title and snippet text* are compared with the name we already have;
nothing in them is ever interpreted as an instruction.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.domain.identity import normalise_name, registrable_domain, website_origin

#: Directories, review sites, social networks, maps and listing aggregators. A result on one of these is never a company's own site.
AGGREGATOR_DOMAINS = frozenset(
    {
        "yelp.com", "yelp.ca", "yelp.co.uk", "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
        "tiktok.com", "youtube.com", "pinterest.com", "reddit.com", "nextdoor.com", "yellowpages.com", "yellowpages.ca",
        "yell.com", "mapquest.com", "foursquare.com", "tripadvisor.com", "bbb.org", "manta.com", "angi.com", "angieslist.com",
        "thumbtack.com", "houzz.com", "healthgrades.com", "zocdoc.com", "webmd.com", "vitals.com", "ratemds.com",
        "doctor.com", "yellowbot.com", "superpages.com", "chamberofcommerce.com", "cylex.us.com", "cylex-uk.co.uk",
        "opencorporates.com", "dnb.com", "zoominfo.com", "crunchbase.com", "glassdoor.com", "indeed.com", "justdial.com",
        "sulekha.com", "indiamart.com", "tradeindia.com", "wikipedia.org", "wikidata.org", "google.com", "bing.com",
        "apple.com", "booking.com", "expedia.com", "opentable.com", "groupon.com", "alignable.com", "birdeye.com",
        "trustpilot.com", "sitejabber.com", "citysearch.com", "merchantcircle.com", "localstack.com", "mapcarta.com",
        "findagrave.com", "dentistry.com", "1800dentist.com", "colleges.com", "companieshouse.gov.uk",
    }
)  # fmt: skip

#: Words that describe *what kind* of business it is, not *which* one. They never prove a match on their own.
GENERIC_WORDS = frozenset(
    {
        "the", "and", "for", "of", "inc", "llc", "ltd", "co", "company", "group", "services", "service", "solutions",
        "associates", "partners", "clinic", "center", "centre", "office", "offices", "practice", "studio", "shop", "store",
        "salon", "restaurant", "cafe", "bar", "grill", "dental", "dentist", "dentistry", "dentists", "orthodontics",
        "medical", "health", "healthcare", "care", "family", "general", "law", "legal", "attorney", "attorneys", "lawyers",
        "accounting", "consulting", "agency", "design", "construction", "contractors", "plumbing", "repair", "auto",
        "realty", "properties", "real", "estate", "insurance", "fitness", "gym", "yoga", "spa", "pet", "vet", "veterinary",
        "hospital", "pharmacy", "school", "academy", "institute", "foundation", "club", "hotel", "inn", "best", "top",
    }
)  # fmt: skip

MIN_TOKEN = 3
#: How much of the domain's own letters (after removing generic words and the city) the business's name words must account for.
MIN_DOMAIN_COVERAGE = 0.75
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class SearchCandidate:
    """A result as returned by a search provider, reduced to the three things we compare."""

    url: str
    title: str
    snippet: str = ""


@dataclass(frozen=True, slots=True)
class SiteChoice:
    website: str  # canonical homepage, e.g. https://example.com
    domain: str
    reason: str  # "domain" | "title+city" — kept for the audit trail


def is_aggregator(url_or_host: str | None) -> bool:
    domain = registrable_domain(url_or_host)
    return domain is not None and domain in AGGREGATOR_DOMAINS


def distinctive_tokens(name: str | None) -> list[str]:
    """The words that identify *this* business: ``Smile Dental LLC`` → ``['smile']``; ``Dental Care`` → ``[]``."""
    seen: list[str] = []
    for token in normalise_name(name).split():
        if len(token) >= MIN_TOKEN and token not in GENERIC_WORDS and token not in seen:
            seen.append(token)
    return seen


def _squash(value: str) -> str:
    return _NON_ALNUM.sub("", value.lower())


def _domain_label(domain: str) -> str:
    """The brand part of a registrable domain, letters and digits only: ``smile-dental.co.uk`` → ``smiledental``."""
    return _squash(domain.split(".", 1)[0])


def _strip_words(label: str, words: Iterable[str]) -> str:
    # longest first so that "dentistry" is removed before "dentist"
    # Words shorter than three letters are left alone: removing "co" or "of" could eat letters of a real name word.
    for word in sorted(
        {_squash(w) for w in words if len(_squash(w)) >= MIN_TOKEN}, key=len, reverse=True
    ):
        label = label.replace(word, "")
    return label


def _domain_matches(label: str, tokens: list[str], city: str | None) -> bool:
    if not label or not tokens or not all(token in label for token in tokens):
        return False
    city_words = [_squash(w) for w in normalise_name(city).split()]
    leftover = _strip_words(label, [*GENERIC_WORDS, *city_words, "inc", "llc", "ltd"])
    if not leftover:
        return True
    # Name words must account for nearly all of what is left: "art" must not match "smartdentalgroup".
    covered = sum(len(token) for token in tokens)
    return covered / len(leftover) >= MIN_DOMAIN_COVERAGE


def choose_website(
    name: str, city: str | None, results: Iterable[SearchCandidate]
) -> SiteChoice | None:
    """The first search result that is plausibly the business's own homepage, or ``None``.

    Results are taken in the search engine's order, so a better-ranked page wins ties; a domain match always beats a
    title-only match regardless of rank. A business whose name has no distinctive word (``Dental Care``) is never matched.
    """
    tokens = distinctive_tokens(name)
    if not tokens:
        return None
    city_key = _squash(city or "")
    wanted_phrase = normalise_name(
        name
    )  # the *whole* name, generic words included: "Brightside Dental" ≠ "Brightside Orthodontics"
    title_match: SiteChoice | None = None
    for result in results:
        domain = registrable_domain(result.url)
        origin = website_origin(result.url)
        if domain is None or origin is None or domain in AGGREGATOR_DOMAINS:
            continue
        if _domain_matches(_domain_label(domain), tokens, city):
            return SiteChoice(website=origin, domain=domain, reason="domain")
        if title_match is None and city_key:
            text = _squash(f"{result.title} {result.snippet}")
            if wanted_phrase in normalise_name(result.title) and city_key in text:
                title_match = SiteChoice(website=origin, domain=domain, reason="title+city")
    return title_match


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def confirms_identity(name: str, phone: str | None, page_text: str) -> bool:
    """Does the fetched page really belong to this business? True when it contains the business's full normalised name, or
    its phone number (at least seven digits, compared digits-only so ``(512) 555-0100`` equals ``+1 512 555 0100``)."""
    wanted = normalise_name(name)
    if wanted and wanted in normalise_name(page_text):
        return True
    digits = _digits(phone or "")
    return len(digits) >= 7 and digits[-10:] in _digits(page_text)
