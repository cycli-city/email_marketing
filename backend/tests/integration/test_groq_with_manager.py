"""The real Groq adapter driven by the ProviderManager over a mocked transport and a real database."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FakeClock
from app.core.http import build_http_client
from app.db.models import ProviderState
from app.domain.enums import Capability, CreditsAccuracy, ProviderStatus
from app.providers.errors import AllProvidersUnavailableError
from app.providers.groq import GroqProvider, build_groq_providers
from app.providers.llm import LLMProvider, LLMRequest, estimate_tokens
from app.providers.manager import ProviderManager
from app.providers.registry import ProviderRegistry
from app.providers.store import ProviderStore

Factory = async_sessionmaker[AsyncSession]
NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
REQUEST = LLMRequest(system="Classify.", user="Not now, thanks.", max_tokens=200)


def reply(remaining: int, reset: str = "10m") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "openai/gpt-oss-120b",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
        headers={
            "x-ratelimit-remaining-requests": str(remaining),
            "x-ratelimit-limit-requests": "1000",
            "x-ratelimit-reset-requests": reset,
        },
    )


class Stack:
    def __init__(
        self,
        factory: Factory,
        workspace_id: uuid.UUID,
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> None:
        self.factory, self.workspace_id = factory, workspace_id
        self.clock = FakeClock(NOW)
        self.requests = 0

        def counting(request: httpx.Request) -> httpx.Response:
            self.requests += 1
            return handler(request)

        client = build_http_client(transport=httpx.MockTransport(counting))
        self.registry = ProviderRegistry()
        for provider in build_groq_providers(
            api_key=SecretStr("gsk_" + "k" * 40),
            smart_model="openai/gpt-oss-120b",
            fast_model="openai/gpt-oss-20b",
            client=client,
            clock=self.clock,
        ):
            self.registry.register(provider)
        store = ProviderStore(factory, self.clock, workspace_id)
        self.manager = ProviderManager(store, self.registry, self.clock)

    async def call(self, tier: str = "smart") -> Any:
        async def operation(provider: Any) -> Any:
            assert isinstance(provider, GroqProvider)
            assert isinstance(provider, LLMProvider)
            return await provider.complete(REQUEST)

        return await self.manager.call(
            Capability.LLM,
            operation,
            tier=tier,
            weight=estimate_tokens(REQUEST),
            personal_data=True,
        )

    async def state(self, key: str) -> ProviderState:
        async with self.factory() as s:
            return (
                await s.execute(select(ProviderState).where(ProviderState.provider_key == key))
            ).scalar_one()


async def test_header_reported_quota_is_stored_as_provider_reported_and_blocks_at_zero(
    factory: Factory, workspace_id: uuid.UUID
) -> None:
    responses = iter([reply(1), reply(0), reply(999)])
    stack = Stack(factory, workspace_id, lambda r: next(responses))

    await stack.call()
    row = await stack.state("groq-smart")
    assert (row.remaining_credits, row.credits_total, row.credits_accuracy) == (
        Decimal(1),
        Decimal(1000),
        CreditsAccuracy.PROVIDER_REPORTED,
    )
    assert row.reset_at == NOW + timedelta(minutes=10)

    await stack.call()  # the last request of the day; Groq now reports 0 remaining
    with pytest.raises(AllProvidersUnavailableError) as blocked:
        await stack.call()
    assert stack.requests == 2, "a provider reporting zero is not even asked again"
    assert blocked.value.next_available_at == NOW + timedelta(minutes=10)
    assert "credits_exhausted" in blocked.value.reasons["groq-smart"]

    # After the reported reset time the stale zero no longer blocks, and a fresh report replaces it.
    stack.clock.advance(minutes=11)
    await stack.call()
    assert (await stack.state("groq-smart")).remaining_credits == Decimal(999)


async def test_a_daily_quota_429_parks_work_until_the_reset_the_provider_gave(
    factory: Factory, workspace_id: uuid.UUID
) -> None:
    def daily_limit(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "error": {
                    "message": "Rate limit reached on requests per day (RPD): Limit 1000. Please try again in 6h30m."
                }
            },
            headers={"retry-after": "23400"},
        )

    stack = Stack(factory, workspace_id, daily_limit)
    await (
        stack.manager.start()
    )  # as at application boot: every registered provider gets its state row
    with pytest.raises(AllProvidersUnavailableError) as raised:
        await stack.call()
    reset = NOW + timedelta(seconds=23400)
    assert raised.value.next_available_at == reset
    row = await stack.state("groq-smart")
    assert (row.status, row.reset_at) == (ProviderStatus.EXHAUSTED, reset)
    # The other model has its own, untouched allowance — but a "smart" request is never served by it.
    fast = await stack.state("groq-fast")
    assert fast.status == ProviderStatus.READY
    assert stack.requests == 1


async def test_the_two_models_keep_separate_counters(
    factory: Factory, workspace_id: uuid.UUID
) -> None:
    stack = Stack(factory, workspace_id, lambda r: reply(500))
    await stack.call("smart")
    await stack.call("fast")
    await stack.call("fast")
    assert (await stack.state("groq-smart")).daily_used == 1
    assert (await stack.state("groq-fast")).daily_used == 2
