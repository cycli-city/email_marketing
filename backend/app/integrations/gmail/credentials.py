"""What the send and sync code needs from the connection layer — and nothing more."""

from __future__ import annotations

import uuid
from typing import Protocol


class MailboxCredentials(Protocol):
    """Satisfied by :class:`~app.integrations.gmail.service.GmailService`."""

    async def access_token(self, workspace_id: uuid.UUID, account_id: uuid.UUID) -> str:
        """A valid access token (refreshed if needed). Raises ``AccountNeedsReauthError`` / ``GoogleUnavailableError``."""
        ...

    async def mark_needs_reauth(self, workspace_id: uuid.UUID, account_id: uuid.UUID) -> None:
        """Google refused this mailbox's credentials: stop using it until the owner reconnects. Idempotent."""
        ...
