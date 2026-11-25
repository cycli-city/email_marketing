"""A fake page fetcher for tests: serves canned pages by URL, records what was requested, never touches the network."""

from __future__ import annotations

from app.integrations.web.fetcher import FetchError, Page


def html(body: str, title: str = "Smile Dental") -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def page(url: str, text: str) -> Page:
    return Page(url=url, status=200, content_type="text/html", text=text, truncated=False)


class FakeFetcher:
    def __init__(self, pages: dict[str, Page | FetchError] | None = None) -> None:
        self.pages: dict[str, Page | FetchError] = pages or {}
        self.requested: list[str] = []

    async def fetch(self, url: str, *, respect_robots: bool = True) -> Page:
        self.requested.append(url)
        outcome = self.pages.get(url, FetchError("not found", status=404))
        if isinstance(outcome, FetchError):
            raise outcome
        return outcome
