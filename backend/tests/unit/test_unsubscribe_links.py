"""Unsubscribe links: signed, personal-data-free, never expiring, and good for nothing except unsubscribing."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.core.clock import FakeClock
from app.core.security import create_action_token
from app.services.outreach.unsubscribe import PURPOSE, Target, UnsubscribeLinks

SECRET = "s" * 48
BACKEND = "https://api.example.com"
WORKSPACE = uuid.uuid4()
MESSAGE = uuid.uuid4()


def links(
    secret: str = SECRET, clock: FakeClock | None = None, base: str = BACKEND
) -> UnsubscribeLinks:
    return UnsubscribeLinks(secret=secret, backend_url=base, clock=clock or FakeClock())


def token_of(url: str) -> str:
    return url.rsplit("/u/", 1)[1]


def test_a_link_resolves_to_the_message_it_was_made_for() -> None:
    url = links().link(WORKSPACE, MESSAGE)
    assert url.startswith(f"{BACKEND}/u/")
    assert links().resolve(token_of(url)) == Target(WORKSPACE, MESSAGE)


def test_the_link_carries_no_address_or_name() -> None:
    url = links().link(WORKSPACE, MESSAGE)
    assert "@" not in url
    assert "example.com/" not in url.removeprefix(BACKEND)


def test_a_trailing_slash_on_the_base_url_is_harmless() -> None:
    assert links(base=BACKEND + "/").link(WORKSPACE, MESSAGE).startswith(f"{BACKEND}/u/")


def test_a_link_never_expires() -> None:
    clock = FakeClock()
    token = token_of(links(clock=clock).link(WORKSPACE, MESSAGE))
    clock.advance(days=365 * 10)
    assert links(clock=clock).resolve(token) == Target(WORKSPACE, MESSAGE)


def test_every_link_is_distinct_but_all_resolve() -> None:
    a, b = links().link(WORKSPACE, MESSAGE), links().link(WORKSPACE, MESSAGE)
    assert a != b
    assert links().resolve(token_of(a)) == links().resolve(token_of(b))


def test_a_token_signed_with_another_secret_is_refused() -> None:
    assert links(secret="x" * 48).resolve(token_of(links().link(WORKSPACE, MESSAGE))) is None


def test_a_token_for_another_purpose_cannot_unsubscribe_anyone() -> None:
    other = create_action_token(
        secret=SECRET,
        purpose="oauth_state",
        subject=str(MESSAGE),
        ttl=None,
        claims={"ws": str(WORKSPACE)},
    )
    assert links().resolve(other) is None


def test_a_correctly_signed_token_without_the_workspace_is_refused() -> None:
    bare = create_action_token(secret=SECRET, purpose=PURPOSE, subject=str(MESSAGE), ttl=None)
    assert links().resolve(bare) is None


def test_a_token_with_a_malformed_identifier_is_refused() -> None:
    bad = create_action_token(
        secret=SECRET,
        purpose=PURPOSE,
        subject="not-a-uuid",
        ttl=None,
        claims={"ws": str(WORKSPACE)},
    )
    assert links().resolve(bad) is None


@pytest.mark.parametrize(
    "garbage", ["", "abc", "a.b.c", "x" * 500, "../../etc/passwd", "eyJhbGciOiJub25lIn0.e30."]
)
def test_garbage_is_refused_not_raised(garbage: str) -> None:
    assert links().resolve(garbage) is None


def test_tampering_with_the_payload_is_detected() -> None:
    token = token_of(links().link(WORKSPACE, MESSAGE))
    head, payload, signature = token.split(".")
    flipped = payload[:-2] + ("AA" if not payload.endswith("AA") else "BB")
    assert links().resolve(f"{head}.{flipped}.{signature}") is None


def test_an_expiring_token_made_by_mistake_still_expires() -> None:
    clock = FakeClock()
    short = create_action_token(
        secret=SECRET,
        purpose=PURPOSE,
        subject=str(MESSAGE),
        ttl=timedelta(days=1),
        clock=clock,
        claims={"ws": str(WORKSPACE)},
    )
    clock.advance(days=2)
    assert links(clock=clock).resolve(short) is None
