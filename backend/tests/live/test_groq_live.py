"""Live checks against the real Groq API. Opt-in only: ``RUN_LIVE=1 GROQ_API_KEY=... pytest -m live``.

Never runs in CI (no secrets there, and it spends a few of the free daily requests). Run it after changing the adapter or
when Groq's API changes, to confirm the assumptions the mocked unit tests encode.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

from app.core.clock import SYSTEM_CLOCK
from app.providers.errors import AuthError
from app.providers.groq import GroqProvider, build_groq_providers
from app.providers.http import build_http_client
from app.providers.llm import LLMRequest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (os.environ.get("RUN_LIVE") and os.environ.get("GROQ_API_KEY")),
        reason="live test: set RUN_LIVE=1 and GROQ_API_KEY",
    ),
]

LABELS = ["interested", "not_interested", "not_now", "out_of_office", "unsubscribe", "other"]
SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": LABELS}},
    "required": ["label"],
    "additionalProperties": False,
}


@pytest_asyncio.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    client = build_http_client()
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def providers(http: httpx.AsyncClient) -> list[GroqProvider]:
    from app.core.config import Settings

    settings = (
        Settings()
    )  # reads GROQ_MODEL / GROQ_FAST_MODEL defaults; key comes from the environment
    return build_groq_providers(
        api_key=SecretStr(os.environ["GROQ_API_KEY"]),
        smart_model=settings.groq_model,
        fast_model=settings.groq_fast_model,
        client=http,
        clock=SYSTEM_CLOCK,
    )


@pytest.mark.parametrize("which", [0, 1], ids=["smart", "fast"])
async def test_a_plain_completion_works_and_reports_real_quota(
    providers: list[GroqProvider], which: int
) -> None:
    provider = providers[which]
    result = await provider.complete(
        LLMRequest(system="Answer with one word.", user="Say: pong", max_tokens=300)
    )
    assert result.value.text.strip(), (
        "non-empty completion (hidden reasoning must not eat the whole budget)"
    )
    assert result.value.completion_tokens > 0
    # Provider-reported quota is real, not invented.
    assert result.remaining is not None
    assert result.limit is not None
    assert result.limit == provider.spec.daily_limit, (
        "the 1,000/day figure verified on 2026-11-19 still holds"
    )
    assert result.reset_at is not None


async def test_strict_json_schema_output_is_valid_and_constrained(
    providers: list[GroqProvider],
) -> None:
    fast = providers[1]
    result = await fast.complete(
        LLMRequest(
            system="Classify the prospect's email reply into exactly one label.",
            user="Thanks for reaching out, but we are not looking for this right now. Maybe next quarter.",
            json_schema=SCHEMA,
            schema_name="reply_label",
            max_tokens=400,
        )
    )
    assert isinstance(result.value.parsed, dict)
    assert result.value.parsed["label"] in LABELS


async def test_a_wrong_key_is_a_clean_auth_error_that_does_not_echo_the_key(
    http: httpx.AsyncClient,
) -> None:
    bad = "gsk_" + "0" * 52
    provider = build_groq_providers(
        api_key=SecretStr(bad),
        smart_model="openai/gpt-oss-120b",
        fast_model="openai/gpt-oss-20b",
        client=http,
        clock=SYSTEM_CLOCK,
    )[0]
    with pytest.raises(AuthError) as raised:
        await provider.complete(LLMRequest(system="s", user="u", max_tokens=16))
    assert bad not in str(raised.value)
