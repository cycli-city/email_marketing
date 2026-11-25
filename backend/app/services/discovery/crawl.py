"""Reading a company's own website, politely and safely, for the addresses it publishes.

Everything goes through the SSRF-guarded fetcher (ADR-038): robots.txt is honoured, hosts are throttled, private addresses are
unreachable. This module adds the *site-level* rules on top of that page-level safety:

* a small page budget — the home page plus the few most promising contact/about/team pages;
* a page that the site redirects to **on a different domain is ignored**: an address found there would look "same-site"
  relative to *that* domain and could belong to an unrelated business;
* the visible text of the pages read is returned so the caller can confirm the site really is about the business.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.identity import registrable_domain
from app.integrations.web.extract import FoundEmail, candidate_pages, extract_emails, visible_text
from app.integrations.web.fetcher import FetchError, Page

MAX_PAGES_PER_SITE = 3
MAX_TEXT_CHARS = 200_000


class PageFetcher(Protocol):
    async def fetch(self, url: str, *, respect_robots: bool = True) -> Page: ...


@dataclass(frozen=True, slots=True)
class SiteReading:
    website: str
    emails: tuple[FoundEmail, ...]  # same-site addresses first
    pages: tuple[str, ...]  # the final URL of every page that was read
    text: str  # what a visitor would read on those pages (bounded)
    problem: str | None  # the first reason a page could not be read, if any


class SiteReader:
    def __init__(self, fetcher: PageFetcher, *, max_pages: int = MAX_PAGES_PER_SITE) -> None:
        self._fetcher = fetcher
        self._max_pages = max_pages

    async def read(self, website: str) -> SiteReading:
        """Home page first, then the most promising contact/about pages. Never raises for a page that cannot be read."""
        site = registrable_domain(website)
        emails: dict[str, FoundEmail] = {}
        read: list[str] = []
        texts: list[str] = []
        queue = [website]
        seen: set[str] = set()
        problem: str | None = None
        while queue and len(read) < self._max_pages:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                page = await self._fetcher.fetch(url)
            except FetchError as exc:
                problem = problem or exc.reason + (" (unsafe)" if exc.unsafe else "")
                continue
            if registrable_domain(page.url) != site:
                problem = problem or "redirected to another site"
                continue
            read.append(page.url)
            texts.append(visible_text(page.text))
            for found in extract_emails(page.text, page.url):
                emails.setdefault(found.address, found)
            if len(read) == 1:
                queue.extend(candidate_pages(page.text, page.url, limit=self._max_pages - 1))
        return SiteReading(
            website=website,
            emails=tuple(sorted(emails.values(), key=lambda e: (not e.same_site, e.address))),
            pages=tuple(read),
            text=" ".join(texts)[:MAX_TEXT_CHARS],
            problem=problem,
        )
