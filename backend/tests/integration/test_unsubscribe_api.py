"""The public unsubscribe route end to end: real ASGI app, real PostgreSQL, real signed tokens."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FakeClock
from app.db.models import EmailMessage, SuppressionEntry
from app.db.session import session_scope
from app.domain.enums import MessageStatus
from app.services.outreach.unsubscribe import UnsubscribeLinks
from tests.factories import make_workspace
from tests.helpers import make_settings, running
from tests.integration.test_unsubscribe import new_message

ADDRESS = "pat@example.com"
SECRET = "j" * 40  # what ``make_settings`` configures


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


class Fixture:
    def __init__(
        self, client: httpx.AsyncClient, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self.client = client
        self.factory = factory
        self.links = UnsubscribeLinks(
            secret=SECRET, backend_url="http://localhost:8000", clock=FakeClock()
        )
        self.workspace_id: uuid.UUID
        self.message_id: uuid.UUID
        self.queued_id: uuid.UUID

    async def seed(self) -> Fixture:
        async with session_scope(self.factory) as s:
            ws = await make_workspace(s)
            sent = new_message(ws.id, ADDRESS, MessageStatus.SENT)
            queued = new_message(ws.id, ADDRESS, MessageStatus.QUEUED, step=1)
            s.add_all([sent, queued])
            await s.flush()
            self.workspace_id, self.message_id, self.queued_id = ws.id, sent.id, queued.id
        return self

    @property
    def token(self) -> str:
        return self.links.link(self.workspace_id, self.message_id).rsplit("/u/", 1)[1]

    async def suppression_count(self) -> int:
        async with self.factory() as s:
            return (
                await s.execute(select(func.count()).select_from(SuppressionEntry))
            ).scalar_one()

    async def queued_status(self) -> MessageStatus:
        async with self.factory() as s:
            row = await s.get(EmailMessage, self.queued_id)
            assert row is not None
            return row.status


@pytest.fixture
async def fx(
    migrated_database: str, factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> AsyncIterator[Fixture]:
    settings = make_settings(database_url=migrated_database)
    async with running(settings, clock=clock) as (_, client):
        yield await Fixture(client, factory).seed()


async def test_opening_the_link_shows_a_confirmation_and_changes_nothing(fx: Fixture) -> None:
    response = await fx.client.get(f"/u/{fx.token}")

    assert response.status_code == 200
    assert "Unsubscribe me" in response.text
    assert (
        await fx.suppression_count() == 0
    )  # link previewers and mail scanners open links; they unsubscribe nobody
    assert await fx.queued_status() is MessageStatus.QUEUED


async def test_the_confirmation_form_posts_back_to_the_same_link(fx: Fixture) -> None:
    token = fx.token  # every access mints a new signed token, so take one and use it for both
    page = (await fx.client.get(f"/u/{token}")).text
    assert re.search(rf'<form method="post" action="/u/{re.escape(token)}">', page)


async def test_posting_unsubscribes_and_cancels_what_was_queued(fx: Fixture) -> None:
    response = await fx.client.post(f"/u/{fx.token}")

    assert response.status_code == 200
    assert "You are unsubscribed" in response.text
    assert await fx.suppression_count() == 1
    assert await fx.queued_status() is MessageStatus.CANCELLED


async def test_a_mail_clients_one_click_request_works(fx: Fixture) -> None:
    """RFC 8058: the client POSTs ``List-Unsubscribe=One-Click`` to the https URL from the header."""
    response = await fx.client.post(
        f"/u/{fx.token}",
        content="List-Unsubscribe=One-Click",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200
    assert await fx.suppression_count() == 1


async def test_doing_it_again_is_harmless(fx: Fixture) -> None:
    for _ in range(3):
        assert (await fx.client.post(f"/u/{fx.token}")).status_code == 200
    assert await fx.suppression_count() == 1


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("token", ["nonsense", "a.b.c", "x" * 3000])
async def test_a_bad_link_looks_the_same_whatever_is_wrong_with_it(
    fx: Fixture, method: str, token: str
) -> None:
    response = await getattr(fx.client, method)(f"/u/{token}")
    assert response.status_code == 404
    assert "not valid" in response.text
    assert await fx.suppression_count() == 0


async def test_a_genuine_token_for_a_message_that_is_gone_is_just_as_unrevealing(
    fx: Fixture,
) -> None:
    ghost = fx.links.link(fx.workspace_id, uuid.uuid4()).rsplit("/u/", 1)[1]
    response = await fx.client.post(f"/u/{ghost}")
    assert response.status_code == 404
    assert "not valid" in response.text
    assert await fx.suppression_count() == 0


async def test_a_token_signed_with_another_secret_is_refused(fx: Fixture) -> None:
    forged = UnsubscribeLinks(secret="x" * 40, backend_url="http://localhost:8000").link(
        fx.workspace_id, fx.message_id
    )
    response = await fx.client.post("/u/" + forged.rsplit("/u/", 1)[1])
    assert response.status_code == 404
    assert await fx.suppression_count() == 0


async def test_the_pages_are_locked_down_and_say_nothing_about_the_recipient(fx: Fixture) -> None:
    for response in (await fx.client.get(f"/u/{fx.token}"), await fx.client.post(f"/u/{fx.token}")):
        csp = response.headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "form-action 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "script-src" not in csp
        assert "<script" not in response.text.lower()
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "noindex" in response.headers["x-robots-tag"]
        assert ADDRESS not in response.text
        assert response.headers["content-type"].startswith("text/html")


async def test_the_stylesheet_is_pinned_by_hash_so_no_inline_style_is_allowed_in_general(
    fx: Fixture,
) -> None:
    response = await fx.client.get(f"/u/{fx.token}")
    csp = response.headers["content-security-policy"]
    assert re.search(r"style-src 'sha256-[A-Za-z0-9+/=]+'", csp)
    assert "'unsafe-inline'" not in csp


async def test_the_route_is_not_part_of_the_published_api_schema(fx: Fixture) -> None:
    schema = (await fx.client.get("/openapi.json")).json()
    assert not any(path.startswith("/u/") for path in schema["paths"])
