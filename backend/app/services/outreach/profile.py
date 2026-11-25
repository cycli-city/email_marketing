"""The sender profile: who the mail is from and where they are based (one per mode, ADR-043).

The compliance gate refuses to send unless the sender is named, and — for marketing — unless a postal address is on record,
so this is the place both are validated and saved. Input is cleaned before it is stored because it ends up in the footer of
every message: control characters are removed, lengths are capped, the reply-to must be a real address and the website a
real site. Nothing here ever invents a value.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.db.models import SenderProfile
from app.db.session import session_scope
from app.domain.enums import CampaignMode
from app.domain.identity import normalise_email, website_origin

MAX_NAME = 200
MAX_ADDRESS = 500
MAX_SIGNATURE = 2000
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # everything except tab and newline
_SPACES = re.compile(r"[ \t]+")


class ProfileError(ValueError):
    """The input is not acceptable. ``field`` says which part, so a form can point at it."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field


@dataclass(frozen=True, slots=True)
class ProfileInput:
    display_name: str
    postal_address: str | None = None
    reply_to: str | None = None
    signature_text: str | None = None
    website: str | None = None


def _single_line(value: str | None) -> str:
    return _SPACES.sub(
        " ", _CONTROL.sub("", (value or "").replace("\r", " ").replace("\n", " "))
    ).strip()


def _multi_line(value: str | None) -> str:
    text = _CONTROL.sub("", (value or "").replace("\r\n", "\n").replace("\r", "\n"))
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def validated(data: ProfileInput) -> ProfileInput:
    name = _single_line(data.display_name)
    if len(name) < 2:
        raise ProfileError("display_name", "the sender needs a name (at least two characters)")
    if len(name) > MAX_NAME:
        raise ProfileError("display_name", f"at most {MAX_NAME} characters")
    postal = _multi_line(data.postal_address) or None
    if postal is not None and len(postal) > MAX_ADDRESS:
        raise ProfileError("postal_address", f"at most {MAX_ADDRESS} characters")
    reply_to = None
    if data.reply_to and data.reply_to.strip():
        reply_to = normalise_email(data.reply_to)
        if reply_to is None:
            raise ProfileError("reply_to", "this is not a valid e-mail address")
    signature = _multi_line(data.signature_text) or None
    if signature is not None and len(signature) > MAX_SIGNATURE:
        raise ProfileError("signature_text", f"at most {MAX_SIGNATURE} characters")
    website = None
    if data.website and data.website.strip():
        website = website_origin(data.website.strip())
        if website is None:
            raise ProfileError("website", "this is not a usable web address")
    return ProfileInput(name, postal, reply_to, signature, website)


class SenderProfiles:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._factory = session_factory
        self._clock = clock

    async def get(self, workspace_id: uuid.UUID, mode: CampaignMode) -> SenderProfile | None:
        async with self._factory() as s:
            return (
                await s.execute(
                    select(SenderProfile).where(
                        SenderProfile.workspace_id == workspace_id, SenderProfile.mode == mode
                    )
                )
            ).scalar_one_or_none()

    async def save(
        self, workspace_id: uuid.UUID, mode: CampaignMode, data: ProfileInput
    ) -> SenderProfile:
        """Create or replace the profile for a mode. Replaces *all* editable fields: the form is the source of truth, and an
        emptied optional field means "none". The profile's grounding facts are not touched here."""
        clean = validated(data)
        now = self._clock.now()
        values = {
            "display_name": clean.display_name,
            "postal_address": clean.postal_address,
            "reply_to": clean.reply_to,
            "signature_text": clean.signature_text,
            "website": clean.website,
            "updated_at": now,
        }
        async with session_scope(self._factory) as s:
            await s.execute(
                pg_insert(SenderProfile)
                .values(
                    id=uuid.uuid4(), workspace_id=workspace_id, mode=mode, created_at=now, **values
                )
                .on_conflict_do_update(index_elements=["workspace_id", "mode"], set_=values)
            )
            profile = (
                await s.execute(
                    select(SenderProfile).where(
                        SenderProfile.workspace_id == workspace_id, SenderProfile.mode == mode
                    )
                )
            ).scalar_one()
            s.expunge(profile)
            return profile

    async def missing(self, workspace_id: uuid.UUID, mode: CampaignMode) -> list[str]:
        """What still has to be filled in before this mode may send. Empty = ready."""
        profile = await self.get(workspace_id, mode)
        if profile is None:
            problems = ["the sender's name"]
            if mode is CampaignMode.NEXORA:
                problems.append("the sender's postal address")
            return problems
        problems = []
        if len((profile.display_name or "").strip()) < 2:
            problems.append("the sender's name")
        if mode is CampaignMode.NEXORA and not (profile.postal_address or "").strip():
            problems.append("the sender's postal address")
        return problems
