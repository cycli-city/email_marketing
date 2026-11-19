"""Value objects produced by token verification."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Principal:
    """A cryptographically verified Supabase session. Being a Principal does NOT mean being the owner —
    :func:`app.auth.deps.require_owner` additionally checks the allow-list."""

    user_id: uuid.UUID  # the Supabase auth user id (the token's ``sub``)
    email: str  # lower-cased
    aal: str  # authenticator assurance level: "aal1" (password) or "aal2" (password + MFA)
    session_id: str | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Owner:
    """The authorised, provisioned owner of the (single) workspace."""

    principal: Principal
    user_id: uuid.UUID  # our ``users.id``
    workspace_id: uuid.UUID

    @property
    def email(self) -> str:
        return self.principal.email
