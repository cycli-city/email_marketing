"""Web search providers (SEARCH capability): Tavily (primary) and Brave Search (transient use only).

Verified on the owner's keys on 2026-11-20:

* **Tavily** — ``POST https://api.tavily.com/search`` (``Authorization: Bearer``) with ``{"query","max_results"}`` answers
  ``{"results":[{"url","title","content","score"}], …}``; bad key → ``401 {"detail":{"error":"Unauthorized: …"}}``.
* **Brave** — ``GET https://api.search.brave.com/res/v1/web/search?q=…&count=…`` (``X-Subscription-Token``) answers
  ``{"web":{"results":[{"title","url","description"}]}}`` with ``x-ratelimit-*`` headers. **A bad key answers HTTP 422
  (``SUBSCRIPTION_TOKEN_INVALID``), not 401** — mapped to an authentication problem here, because a default mapping would
  read 422 as "your request is wrong" and keep retrying a dead key.

Brave's standard plans give **no right to store results**: callers use Brave hits transiently (to pick a URL to fetch) and
never persist the text (docs/PROVIDERS.md §4); the provider spec records that as ``tier="transient"``.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
from pydantic import SecretStr

from app.core.clock import Clock
from app.domain.enums import Capability
from app.providers.adapters import _parse as p
from app.providers.discovery import SearchHit, SearchProvider
from app.providers.errors import (
    AuthError,
    InvalidResponseError,
    PermanentProviderError,
    ProviderError,
    QuotaExhaustedError,
)
from app.providers.http_base import HttpProvider
from app.providers.spec import ProviderResult, ProviderSpec

TAVILY_URL = "https://api.tavily.com/search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_QUERY_CHARS = 400


def _clean_query(query: str) -> str:
    cleaned = " ".join(query.split())[:MAX_QUERY_CHARS]
    if not cleaned:
        raise PermanentProviderError("empty search query")
    return cleaned


def _hits(
    items: list[object], *, title: str, url: str, snippet: str, limit: int
) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for item in items:
        data = p.mapping(item)
        link = p.text(data.get(url), limit=2000)
        if link is None or not link.startswith(("http://", "https://")):
            continue
        hits.append(
            SearchHit(
                title=p.text(data.get(title), limit=300) or link,
                url=link,
                snippet=p.text(data.get(snippet), limit=600) or "",
            )
        )
        if len(hits) >= limit:
            break
    return hits


class TavilyProvider(HttpProvider, SearchProvider):
    def __init__(
        self, *, api_key: SecretStr | None, client: httpx.AsyncClient, clock: Clock
    ) -> None:
        super().__init__(
            client=client, clock=clock, secrets=[api_key.get_secret_value()] if api_key else []
        )
        self._api_key = api_key
        self.spec = ProviderSpec(
            "tavily",
            Capability.SEARCH,
            "Tavily",
            free_tier=True,
            default_priority=10,
            monthly_limit=1000,
            rate_limit_per_minute=60,
        )

    @property
    def configured(self) -> bool:
        return self._api_key is not None and bool(self._api_key.get_secret_value())

    async def web_search(self, query: str, *, limit: int = 5) -> ProviderResult[list[SearchHit]]:
        if self._api_key is None:
            raise self.auth_problem("TAVILY_API_KEY is not configured")
        count = max(1, min(limit, 10))
        response = await self.request(
            "POST",
            TAVILY_URL,
            json={"query": _clean_query(query), "max_results": count, "include_answer": False},
            headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
        )
        body = p.mapping(self.json_body(response))
        if not isinstance(body.get("results"), list):
            raise InvalidResponseError("Tavily answered without a result list")
        hits = _hits(body["results"], title="title", url="url", snippet="content", limit=count)
        return ProviderResult(hits, units_used=Decimal(1))


class BraveSearchProvider(HttpProvider, SearchProvider):
    def __init__(
        self, *, api_key: SecretStr | None, client: httpx.AsyncClient, clock: Clock
    ) -> None:
        super().__init__(
            client=client, clock=clock, secrets=[api_key.get_secret_value()] if api_key else []
        )
        self._api_key = api_key
        self.spec = ProviderSpec(
            "brave",
            Capability.SEARCH,
            "Brave Search",
            free_tier=True,
            default_priority=20,
            monthly_limit=1000,
            rate_limit_per_minute=60,
            tier="transient",  # results may be used but not stored (docs/PROVIDERS.md §4)
        )

    @property
    def configured(self) -> bool:
        return self._api_key is not None and bool(self._api_key.get_secret_value())

    def refine_error(self, status: int, text: str) -> ProviderError | None:
        if "SUBSCRIPTION_TOKEN_INVALID" in text:
            return AuthError("Brave rejected the subscription token", http_status=status)
        if "USAGE_LIMIT" in text or "QUOTA_LIMITED" in text:
            return QuotaExhaustedError("Brave's plan allowance is used up", http_status=status)
        return None

    async def web_search(self, query: str, *, limit: int = 5) -> ProviderResult[list[SearchHit]]:
        if self._api_key is None:
            raise self.auth_problem("BRAVE_SEARCH_API_KEY is not configured")
        count = max(1, min(limit, 20))
        response = await self.request(
            "GET",
            BRAVE_URL,
            params={"q": _clean_query(query), "count": count},
            headers={
                "X-Subscription-Token": self._api_key.get_secret_value(),
                "Accept": "application/json",
            },
        )
        web = p.mapping(p.mapping(self.json_body(response)).get("web"))
        hits = _hits(
            p.sequence(web.get("results")),
            title="title",
            url="url",
            snippet="description",
            limit=count,
        )
        return ProviderResult(hits, units_used=Decimal(1))
