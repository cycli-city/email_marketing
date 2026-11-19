"""Send gates that ship with the application.

The real compliance gate (suppression, bounces, replies, jurisdiction rules, sender identity, unsubscribe links — ARCHITECTURE
§7) arrives with the campaign pipeline. Until then the background worker is given :class:`DenyAllGate`: it can reconcile
unknown outcomes and read replies, but it can never send a message, whatever is in the queue.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConnectedEmailAccount, EmailMessage
from app.services.outreach.sender import Clearance, Refusal


class DenyAllGate:
    async def check(
        self, session: AsyncSession, message: EmailMessage, account: ConnectedEmailAccount
    ) -> Clearance | Refusal:
        return Refusal(
            "automatic sending is not enabled yet: the compliance gate has not been built"
        )
