"""The Gmail REST client: every response class maps to the right error, and 'maybe sent' is never mistaken for 'not sent'."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.core.clock import FakeClock
from app.integrations.gmail.api import GmailApi, classify_error
from app.integrations.gmail.mime import BuiltMessage, Mailbox, OutboundEmail, build_message
from app.providers.errors import (
    AuthError,
    ForbiddenReason,
    PermanentProviderError,
    PermissionDeniedError,
    ProviderError,
    QuotaExhaustedError,
    RateLimitedError,
    TransientProviderError,
)
from tests.gmail_fake import FakeGmail, Fate, alias

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
TOKEN = "ya29.fake-access-token-for-tests"


def api_and_fake(**kwargs: Any) -> tuple[GmailApi, FakeGmail]:
    clock = FakeClock(NOW)
    fake = FakeGmail(clock=clock, **kwargs)
    return GmailApi(httpx.AsyncClient(transport=fake.transport()), clock), fake


def built(unique: str = "0123456789abcdef0123456789abcdef") -> BuiltMessage:
    return build_message(
        OutboundEmail(
            sender=Mailbox("founder@example.com"),
            to=Mailbox("lead@example.org"),
            subject="Hello",
            body_text="Body",
            sent_at=NOW,
            message_id=f"<{unique}@example.com>",
        )
    )


def classify(
    status: int,
    message: str = "",
    reason: str = "",
    headers: dict[str, str] | None = None,
    *,
    sending: bool = False,
) -> ProviderError:
    body = {
        "error": {
            "code": status,
            "message": message,
            "errors": [{"reason": reason}] if reason else [],
        }
    }
    return classify_error(
        status, httpx.Headers(headers or {}), body, now=NOW, token=TOKEN, sending=sending
    )


class TestSending:
    async def test_a_successful_send_returns_the_gmail_ids(self) -> None:
        api, fake = api_and_fake()
        sent = await api.send(TOKEN, built().raw)
        assert sent.id == fake.stored[0].id
        assert sent.thread_id == fake.stored[0].thread_id
        assert fake.tokens_seen == [TOKEN]

    async def test_threading_is_passed_through(self) -> None:
        api, fake = api_and_fake()
        sent = await api.send(TOKEN, built().raw, thread_id="thread-abc")
        assert sent.thread_id == "thread-abc"
        assert fake.stored[0].thread_id == "thread-abc"

    @pytest.mark.parametrize(
        "fate",
        [Fate.ACCEPT_THEN_TIMEOUT, Fate.ACCEPT_THEN_GARBAGE, Fate.ACCEPT_THEN_503, Fate.REJECT_503],
    )
    async def test_anything_that_may_have_gone_through_is_flagged_as_possibly_processed(
        self, fate: Fate
    ) -> None:
        api, fake = api_and_fake()
        fake.script.append(fate)
        with pytest.raises(TransientProviderError) as raised:
            await api.send(TOKEN, built().raw)
        assert raised.value.request_may_have_been_processed is True

    async def test_a_connection_that_never_opened_is_definitely_not_sent(self) -> None:
        api, fake = api_and_fake()
        fake.script.append(Fate.CONNECT_ERROR)
        with pytest.raises(TransientProviderError) as raised:
            await api.send(TOKEN, built().raw)
        assert raised.value.request_may_have_been_processed is False
        assert fake.stored == []

    async def test_a_refused_request_is_permanent_and_never_stored(self) -> None:
        api, fake = api_and_fake()
        fake.script.append(Fate.BAD_REQUEST)
        with pytest.raises(PermanentProviderError):
            await api.send(TOKEN, built().raw)
        assert fake.stored == []

    async def test_the_sending_limit_is_a_quota_not_a_rate_limit(self) -> None:
        api, fake = api_and_fake()
        fake.script.append(Fate.SENDING_LIMIT)
        with pytest.raises(QuotaExhaustedError, match="sending limit"):
            await api.send(TOKEN, built().raw)

    async def test_a_429_carries_the_retry_after(self) -> None:
        api, fake = api_and_fake()
        fake.script.append(Fate.RATE_LIMITED)
        with pytest.raises(RateLimitedError) as raised:
            await api.send(TOKEN, built().raw)
        assert raised.value.retry_after == 45

    async def test_unauthorized_and_admin_blocked(self) -> None:
        api, fake = api_and_fake()
        fake.script.extend([Fate.UNAUTHORIZED, Fate.DOMAIN_POLICY])
        with pytest.raises(AuthError):
            await api.send(TOKEN, built().raw)
        with pytest.raises(PermissionDeniedError) as raised:
            await api.send(TOKEN, built().raw)
        assert raised.value.reason == ForbiddenReason.ACCOUNT_RESTRICTED


class TestClassification:
    def test_rate_limit_403_reasons(self) -> None:
        for reason in ("rateLimitExceeded", "userRateLimitExceeded"):
            assert isinstance(classify(403, "slow down", reason), RateLimitedError)

    def test_the_retry_time_google_puts_in_the_message_is_used(self) -> None:
        when = (NOW + timedelta(minutes=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        err = classify(403, f"User-rate limit exceeded. Retry after {when}", "rateLimitExceeded")
        assert isinstance(err, RateLimitedError)
        assert err.retry_after == pytest.approx(420)

    def test_a_far_future_retry_hint_is_capped_at_a_day(self) -> None:
        err = classify(429, "x", "", {"Retry-After": "99999999"})
        assert isinstance(err, RateLimitedError)
        assert err.retry_after == 24 * 3600

    @pytest.mark.parametrize(
        "message",
        [
            "Daily user sending quota exceeded.",
            "User-rate limit exceeded (Mail sending)",
            "Sending limit reached",
        ],
    )
    def test_sending_limits_are_quota_exhaustion_whatever_the_status(self, message: str) -> None:
        for status in (403, 429):
            assert isinstance(classify(status, message, "rateLimitExceeded"), QuotaExhaustedError)

    def test_api_daily_limit_is_quota(self) -> None:
        assert isinstance(
            classify(403, "Daily Limit Exceeded", "dailyLimitExceeded"), QuotaExhaustedError
        )

    def test_missing_scope_is_a_permissions_problem(self) -> None:
        err = classify(403, "Insufficient Permission", "insufficientPermissions")
        assert isinstance(err, PermissionDeniedError)
        assert err.reason == ForbiddenReason.PERMISSIONS

    def test_new_style_upper_snake_reasons_are_understood(self) -> None:
        body = {
            "error": {"code": 403, "message": "m", "details": [{"reason": "RATE_LIMIT_EXCEEDED"}]}
        }
        err = classify_error(403, httpx.Headers(), body, now=NOW, token=TOKEN)
        assert isinstance(err, RateLimitedError)

    def test_5xx_is_transient_and_only_maybe_processed_when_sending(self) -> None:
        for sending in (True, False):
            err = classify(502, "bad gateway", sending=sending)
            assert isinstance(err, TransientProviderError)
            assert err.request_may_have_been_processed is sending

    def test_the_token_is_never_echoed_in_an_error_message(self) -> None:
        err = classify(400, f"bad token {TOKEN} in request")
        assert TOKEN not in str(err)

    def test_other_4xx_are_permanent(self) -> None:
        for status in (400, 404, 409, 413, 422):
            assert isinstance(classify(status, "no"), PermanentProviderError)

    def test_an_unrecognised_403_is_not_guessed_at(self) -> None:
        err = classify(403, "mystery")
        assert isinstance(err, PermissionDeniedError)
        assert err.reason == ForbiddenReason.UNKNOWN


class TestReading:
    async def test_gmail_replaces_the_message_id_but_keeps_our_token(self) -> None:
        """The fact the whole reconciler rests on — observed on a real mailbox, reproduced by the fake."""
        api, _fake = api_and_fake()
        message = built()
        sent = await api.send(TOKEN, message.raw)
        ref = await api.get_message_ref(TOKEN, sent.id)
        assert ref.message_id_header != message.rfc_message_id  # Gmail rewrote it
        assert ref.message_id_header is not None
        assert ref.message_id_header.endswith("@mail.example.org>")
        assert ref.aoi_token == message.rfc_message_id[1:].split("@")[0]  # our header survived

    async def test_our_message_is_found_by_recipient_time_and_token(self) -> None:
        api, fake = api_and_fake()
        message = built()
        sent = await api.send(TOKEN, message.raw)
        found = await api.find_sent_by_token(
            TOKEN,
            recipient="lead@example.org",
            after=NOW - timedelta(minutes=5),
            aoi_token=message.rfc_message_id[1:].split("@")[0],
        )
        assert [f.id for f in found] == [sent.id]
        assert found[0].internal_date == NOW
        assert found[0].message_id_header == fake.stored[0].rfc_id

    async def test_another_message_to_the_same_recipient_is_not_mistaken_for_ours(self) -> None:
        api, _ = api_and_fake()
        ours, other = (
            built("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
            built("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
        )
        await api.send(TOKEN, other.raw)
        await api.send(TOKEN, ours.raw)
        found = await api.find_sent_by_token(
            TOKEN,
            recipient="lead@example.org",
            after=NOW - timedelta(minutes=5),
            aoi_token="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        assert len(found) == 1
        assert found[0].aoi_token == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    async def test_mail_sent_before_the_window_or_to_someone_else_is_not_a_candidate(self) -> None:
        api, _fake = api_and_fake()
        message = built()
        await api.send(TOKEN, message.raw)
        token = message.rfc_message_id[1:].split("@")[0]
        later = await api.find_sent_by_token(
            TOKEN, recipient="lead@example.org", after=NOW + timedelta(minutes=1), aoi_token=token
        )
        elsewhere = await api.find_sent_by_token(
            TOKEN,
            recipient="someone.else@example.org",
            after=NOW - timedelta(minutes=5),
            aoi_token=token,
        )
        assert (later, elsewhere) == ([], [])

    async def test_the_search_asks_for_sent_mail_to_that_recipient_since_a_time(self) -> None:
        api, fake = api_and_fake()
        await api.find_sent_by_token(
            TOKEN, recipient="lead@example.org", after=NOW, aoi_token="0123456789abcdef"
        )
        params = fake.requests[-1].url.params
        assert params["q"] == f"in:sent to:lead@example.org after:{int(NOW.timestamp())}"
        assert params["includeSpamTrash"] == "true"

    @pytest.mark.parametrize(
        ("recipient", "token"),
        [
            ("lead@example.org OR in:inbox", "0123456789abcdef"),
            ("lead@example.org\nBcc: x@example.net", "0123456789abcdef"),
            ("not an address", "0123456789abcdef"),
            ("lead@example.org", "short"),
            ("lead@example.org", "has space in it 12345"),
        ],
    )
    async def test_nothing_malformed_is_ever_put_in_a_query(
        self, recipient: str, token: str
    ) -> None:
        api, fake = api_and_fake()
        with pytest.raises(PermanentProviderError):
            await api.find_sent_by_token(TOKEN, recipient=recipient, after=NOW, aoi_token=token)
        assert fake.requests == []

    async def test_the_stored_time_comes_from_gmail(self) -> None:
        api, fake = api_and_fake()
        await api.send(TOKEN, built().raw)
        ref = await api.get_message_ref(TOKEN, fake.stored[0].id)
        assert ref.internal_date == NOW

    async def test_a_malformed_gmail_id_is_never_put_in_a_path(self) -> None:
        api, fake = api_and_fake()
        with pytest.raises(PermanentProviderError):
            await api.get_message_ref(TOKEN, "../settings/sendAs")
        assert fake.requests == []

    async def test_send_as_distinguishes_verified_from_pending_aliases(self) -> None:
        api, _ = api_and_fake(
            aliases=[alias("Good@Example.com"), alias("pending@example.com", verified=False)]
        )
        listing = {a.email: a for a in await api.list_send_as(TOKEN)}
        assert listing["good@example.com"].verified is True
        assert listing["pending@example.com"].verified is False
        primary = next(a for a in listing.values() if a.is_primary)
        assert primary.verified is True

    async def test_a_revoked_token_surfaces_as_an_auth_error(self) -> None:
        api, _ = api_and_fake(valid_tokens={"other"})
        with pytest.raises(AuthError):
            await api.list_send_as(TOKEN)
