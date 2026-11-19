"""Live checks of a REAL connected mailbox, using the same code the application runs.

    python scripts/live_mailbox_check.py status                       # what is connected (no tokens)
    python scripts/live_mailbox_check.py test <account-id>            # read-only: asks Gmail for the profile
    python scripts/live_mailbox_check.py poll <account-id>            # one inbox-sync poll, prints a summary
    python scripts/live_mailbox_check.py send-test <account-id> --to you@your-own-domain --yes-this-is-my-own-address

It talks to the local ``live`` database (the one ``scripts/live_stack.py`` runs against) and to real Google. Secrets
come from ``.local/SECRETS.local.md`` into this process only and are never printed.

``send-test`` sends exactly ONE clearly-labelled test message, and only to the address given, which must be passed with
``--yes-this-is-my-own-address``. A one-address gate refuses anything else, so this can never mail a third party.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

import pgserver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
sys.path.insert(0, str(REPO / "scripts"))
import live_stack  # noqa: E402  (reads .local/SECRETS.local.md; never prints it)

from app.core.clock import SYSTEM_CLOCK  # noqa: E402
from app.core.http import build_http_client  # noqa: E402
from app.core.security import Encryptor  # noqa: E402
from app.db.models import ConnectedEmailAccount, EmailMessage, IncomingReply  # noqa: E402
from app.db.session import create_session_factory, session_scope  # noqa: E402
from app.domain.enums import MessageKind, MessageStatus  # noqa: E402
from app.integrations.gmail.api import GmailApi  # noqa: E402
from app.integrations.gmail.mime import message_id_for  # noqa: E402
from app.integrations.gmail.oauth import GoogleClient, OAuthConfig  # noqa: E402
from app.integrations.gmail.service import GmailService  # noqa: E402
from app.services.inbox.sync import InboxSync  # noqa: E402
from app.services.outreach.sender import Clearance, MailboxSender, Refusal  # noqa: E402


class OneAddressGate:
    """Allows a send only to the one address the owner named; refuses everything else."""

    def __init__(self, address: str) -> None:
        self._address = address.lower()

    async def check(
        self, session: AsyncSession, message: EmailMessage, account: ConnectedEmailAccount
    ) -> Clearance | Refusal:
        if message.to_email.lower() != self._address:
            return Refusal("live self-test: only the owner's own address is allowed")
        return Clearance(
            footer_text="Sent by your own AI Outreach Intelligence platform (live self-test)."
        )


class Live:
    def __init__(self) -> None:
        secrets = live_stack.load_secrets()
        live_stack.need(
            secrets,
            "ENCRYPTION_KEY",
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
            "GOOGLE_REDIRECT_URI",
        )
        # Attach to the database the running API uses; never stop it (cleanup_mode=None).
        server = pgserver.get_server(BACKEND / ".pgdata" / live_stack.DB_NAME, cleanup_mode=None)
        uri = server.get_uri().replace("postgresql://", "postgresql+asyncpg://", 1)
        self.engine = create_async_engine(uri)
        self.factory: async_sessionmaker[AsyncSession] = create_session_factory(self.engine)
        config = OAuthConfig(
            client_id=secrets["GOOGLE_CLIENT_ID"],
            client_secret=secrets["GOOGLE_CLIENT_SECRET"],
            redirect_uri=secrets["GOOGLE_REDIRECT_URI"],
        )
        http = build_http_client()
        self.gmail = GmailService(
            session_factory=self.factory,
            clock=SYSTEM_CLOCK,
            encryptor=Encryptor([secrets["ENCRYPTION_KEY"]]),
            google=GoogleClient(http, config, SYSTEM_CLOCK),
            config=config,
        )
        self.api = GmailApi(http, SYSTEM_CLOCK)

    async def account(self, account_id: str) -> ConnectedEmailAccount:
        async with self.factory() as s:
            row = await s.get(ConnectedEmailAccount, uuid.UUID(account_id))
            if row is None:
                sys.exit("no such mailbox")
            return row

    async def close(self) -> None:
        await self.engine.dispose()


async def status(live: Live) -> None:
    async with live.factory() as s:
        rows = (await s.execute(select(ConnectedEmailAccount))).scalars().all()
    if not rows:
        print("no mailbox connected")
    for a in rows:
        print(
            f"{a.id}  mode={a.mode.value:10s} status={a.status.value:12s} type={a.account_type.value:16s} {a.email_address}"
        )
        print(f"    scopes: {', '.join(s.rsplit('/', 1)[-1] for s in a.scopes)}")


async def test(live: Live, account_id: str) -> None:
    account = await live.account(account_id)
    profile = await live.gmail.test(account.workspace_id, account.id)
    print(
        f"connected as {profile.email}; the mailbox holds {profile.messages_total} messages (nothing was sent or changed)"
    )


async def poll(live: Live, account_id: str) -> None:
    account = await live.account(account_id)
    sync = InboxSync(
        session_factory=live.factory, clock=SYSTEM_CLOCK, gmail=live.api, credentials=live.gmail
    )
    report = await sync.poll(account.workspace_id, account.id)
    print(report)
    async with live.factory() as s:
        replies = (
            (await s.execute(select(IncomingReply).where(IncomingReply.account_id == account.id)))
            .scalars()
            .all()
        )
    for r in replies:
        print(
            f"  reply: kind={r.kind.value} match={r.match_method} from={r.from_email!r} text={(r.body_text or '')[:120]!r}"
        )


async def send_test(live: Live, account_id: str, to: str) -> None:
    account = await live.account(account_id)
    if to.lower() == account.email_address.lower():
        sys.exit(
            "pick a DIFFERENT address of yours: a reply from the mailbox itself is not an inbound reply"
        )
    token = uuid.uuid4().hex
    async with session_scope(live.factory) as s:
        message = EmailMessage(
            workspace_id=account.workspace_id,
            account_id=account.id,
            kind=MessageKind.INITIAL,
            status=MessageStatus.QUEUED,
            to_email=to,
            to_name=None,
            subject="AI Outreach live test - safe to ignore",
            body_text=(
                "Hello,\n\nThis is a one-off test message from your own AI Outreach Intelligence platform, "
                "checking that sending works through the connected Gmail mailbox.\n\n"
                "To test reply detection, just reply to this message with a few words.\n"
            ),
            idempotency_key=f"live-selftest-{token}",
            rfc_message_id=message_id_for(token, account.email_address),
        )
        s.add(message)
        await s.flush()
        message_id = message.id
    sender = MailboxSender(
        session_factory=live.factory,
        clock=SYSTEM_CLOCK,
        gmail=live.api,
        credentials=live.gmail,
        gate=OneAddressGate(to),
    )
    result = await sender.send(account.workspace_id, message_id)
    print(f"send result: {result.outcome.value}", f"({result.detail})" if result.detail else "")
    async with live.factory() as s:
        row = await s.get(EmailMessage, message_id)
        assert row is not None
        print(
            f"message status: {row.status.value}; gmail id: {row.gmail_message_id}; thread: {row.gmail_thread_id}"
        )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    for name in ("test", "poll", "send-test"):
        p = sub.add_parser(name)
        p.add_argument("account_id")
        if name == "send-test":
            p.add_argument("--to", required=True)
            p.add_argument("--yes-this-is-my-own-address", action="store_true", required=True)
    args = parser.parse_args()
    live = Live()
    try:
        if args.command == "status":
            await status(live)
        elif args.command == "test":
            await test(live, args.account_id)
        elif args.command == "poll":
            await poll(live, args.account_id)
        else:
            await send_test(live, args.account_id, args.to)
    finally:
        await live.close()


if __name__ == "__main__":
    asyncio.run(main())
