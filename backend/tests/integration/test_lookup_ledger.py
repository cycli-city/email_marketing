"""The lookup ledger against a real Postgres: a question answered once is never paid for again (ADR-028 §3)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FakeClock
from app.db.models import ProviderLookup
from app.services.discovery.ledger import NEGATIVE_TTL, POSITIVE_TTL, Asked, LookupLedger

OP = "contacts:domain"


class Counter:
    """A stand-in provider call that counts how often it is really made."""

    def __init__(self, asked: Asked | Exception) -> None:
        self.asked = asked
        self.calls = 0

    async def __call__(self) -> Asked:
        self.calls += 1
        await asyncio.sleep(0)  # yield, so concurrent callers genuinely interleave
        if isinstance(self.asked, Exception):
            raise self.asked
        return self.asked


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def ledger(factory: async_sessionmaker[AsyncSession], clock: FakeClock) -> LookupLedger:
    return LookupLedger(factory, clock)


FOUND = Asked(found=True, result={"emails": ["owner@example.com"]}, answered_by="hunter")
NOTHING = Asked(found=False, result={}, answered_by="hunter")


async def test_first_question_is_asked_second_is_answered_from_the_ledger(
    ledger: LookupLedger, workspace_id: uuid.UUID
) -> None:
    ask = Counter(FOUND)
    first = await ledger.through(workspace_id, OP, "example.com", ask)
    second = await ledger.through(workspace_id, OP, "example.com", ask)

    assert ask.calls == 1
    assert (first.cached, second.cached) == (False, True)
    assert second.result == {"emails": ["owner@example.com"]}
    assert second.answered_by == "hunter"  # provenance survives the cache
    assert second.found is True


async def test_a_negative_answer_is_remembered_too(
    ledger: LookupLedger, workspace_id: uuid.UUID
) -> None:
    ask = Counter(NOTHING)
    await ledger.through(workspace_id, OP, "empty.example.com", ask)
    again = await ledger.through(workspace_id, OP, "empty.example.com", ask)

    assert ask.calls == 1  # "nothing found" is not paid for twice either
    assert again.cached is True
    assert again.found is False


async def test_a_different_question_is_a_different_call(
    ledger: LookupLedger, workspace_id: uuid.UUID
) -> None:
    ask = Counter(FOUND)
    await ledger.through(workspace_id, OP, "a.example.com", ask)
    await ledger.through(workspace_id, OP, "b.example.com", ask)
    await ledger.through(workspace_id, "verify:email", "a.example.com", ask)
    assert ask.calls == 3


async def test_answers_are_never_shared_between_workspaces(
    ledger: LookupLedger, workspace_id: uuid.UUID, other_workspace_id: uuid.UUID
) -> None:
    ask = Counter(FOUND)
    await ledger.through(workspace_id, OP, "example.com", ask)
    other = await ledger.through(other_workspace_id, OP, "example.com", ask)
    assert ask.calls == 2
    assert other.cached is False


async def test_a_found_answer_expires_after_the_positive_ttl(
    ledger: LookupLedger, clock: FakeClock, workspace_id: uuid.UUID
) -> None:
    ask = Counter(FOUND)
    await ledger.through(workspace_id, OP, "example.com", ask)

    clock.advance(seconds=POSITIVE_TTL.total_seconds() - 1)
    assert (await ledger.through(workspace_id, OP, "example.com", ask)).cached is True

    clock.advance(seconds=2)
    refreshed = await ledger.through(workspace_id, OP, "example.com", ask)
    assert refreshed.cached is False
    assert ask.calls == 2


async def test_an_answer_is_stale_at_the_exact_moment_it_expires(
    ledger: LookupLedger, clock: FakeClock, workspace_id: uuid.UUID
) -> None:
    ask = Counter(FOUND)
    await ledger.through(workspace_id, OP, "example.com", ask)

    clock.advance(seconds=POSITIVE_TTL.total_seconds())  # exactly expires_at
    assert await ledger.recall(workspace_id, OP, "example.com") is None


async def test_a_slow_lookup_does_not_hold_up_other_questions(
    ledger: LookupLedger, workspace_id: uuid.UUID
) -> None:
    """Callers are serialised per question, not globally: one slow provider call must not block unrelated lookups."""
    release = asyncio.Event()

    async def slow() -> Asked:
        await release.wait()
        return FOUND

    slow_task = asyncio.create_task(ledger.through(workspace_id, OP, "slow.example.com", slow))
    await asyncio.sleep(0)  # let it start and take its lock

    fast = await asyncio.wait_for(
        ledger.through(workspace_id, OP, "fast.example.com", Counter(FOUND)), timeout=10
    )
    assert fast.cached is False
    assert not slow_task.done()

    release.set()
    assert (await slow_task).cached is False


async def test_a_negative_answer_expires_sooner_than_a_found_one(
    ledger: LookupLedger, clock: FakeClock, workspace_id: uuid.UUID
) -> None:
    assert NEGATIVE_TTL < POSITIVE_TTL
    found, nothing = Counter(FOUND), Counter(NOTHING)
    await ledger.through(workspace_id, OP, "has.example.com", found)
    await ledger.through(workspace_id, OP, "none.example.com", nothing)

    clock.advance(seconds=NEGATIVE_TTL.total_seconds() + 1)
    assert (await ledger.through(workspace_id, OP, "has.example.com", found)).cached is True
    assert (await ledger.through(workspace_id, OP, "none.example.com", nothing)).cached is False
    assert (found.calls, nothing.calls) == (1, 2)


async def test_a_failed_call_is_not_an_answer(
    ledger: LookupLedger, workspace_id: uuid.UUID, factory: async_sessionmaker[AsyncSession]
) -> None:
    broken = Counter(RuntimeError("provider down"))
    with pytest.raises(RuntimeError, match="provider down"):
        await ledger.through(workspace_id, OP, "example.com", broken)

    async with factory() as s:
        stored = (await s.execute(select(func.count()).select_from(ProviderLookup))).scalar_one()
    assert stored == 0  # nothing recorded, so the next attempt asks again

    healthy = Counter(FOUND)
    answer = await ledger.through(workspace_id, OP, "example.com", healthy)
    assert answer.cached is False
    assert healthy.calls == 1


async def test_concurrent_callers_share_one_provider_call(
    ledger: LookupLedger, workspace_id: uuid.UUID
) -> None:
    ask = Counter(FOUND)
    answers = await asyncio.gather(
        *(ledger.through(workspace_id, OP, "example.com", ask) for _ in range(8))
    )

    assert ask.calls == 1
    assert sorted(a.cached for a in answers) == [False] + [True] * 7


async def test_remembering_again_replaces_the_row_instead_of_duplicating_it(
    ledger: LookupLedger,
    clock: FakeClock,
    workspace_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await ledger.remember(workspace_id, OP, "example.com", NOTHING)
    clock.advance(days=1)
    await ledger.remember(workspace_id, OP, "example.com", FOUND)

    async with factory() as s:
        rows = (await s.execute(select(ProviderLookup))).scalars().all()
    assert len(rows) == 1
    assert rows[0].found is True
    assert rows[0].answered_by == "hunter"
    assert rows[0].expires_at - rows[0].looked_up_at == POSITIVE_TTL
    assert rows[0].looked_up_at == clock.now()


async def test_the_ttl_can_be_overridden_per_operation(
    ledger: LookupLedger, clock: FakeClock, workspace_id: uuid.UUID
) -> None:
    ask = Counter(FOUND)
    short = timedelta(hours=1)
    await ledger.through(workspace_id, "verify:email", "a@example.com", ask, positive_ttl=short)

    clock.advance(minutes=59)
    assert (await ledger.recall(workspace_id, "verify:email", "a@example.com")) is not None
    clock.advance(minutes=2)
    assert (await ledger.recall(workspace_id, "verify:email", "a@example.com")) is None
