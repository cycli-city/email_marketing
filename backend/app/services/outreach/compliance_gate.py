"""The send gate that fronts the real mailbox sender: the compliance rules of ``app.domain.compliance`` fed with what the
database knows (ARCHITECTURE §5 step 3, §7).

The sender runs it **after** it has claimed a message, so whatever changed since the message was queued — an unsubscribe, a
bounce, a reply, a lead closed by hand — is honoured at the last possible moment. It only ever *reads*: a refusal is returned,
and the sender records it. It never reaches out to a provider and cannot send anything itself.

When the message may go, the gate also supplies what only the compliance layer knows: the signed unsubscribe link (with the
RFC 8058 one-click header) and the footer that names the sender and gives their postal address.
"""

from __future__ import annotations

import uuid
from html import escape

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.db.models import (
    Campaign,
    CampaignSettings,
    Company,
    ConnectedEmailAccount,
    Contact,
    EmailMessage,
    JurisdictionPolicy,
    Lead,
    SenderProfile,
    SuppressionEntry,
)
from app.domain.compliance import (
    Context,
    Policy,
    Recipient,
    Rules,
    Sender,
    Suppressed,
    evaluate,
)
from app.domain.enums import (
    CampaignMode,
    EmailKind,
    EmailOrigin,
    LawfulBasis,
    MessageKind,
    MessageStatus,
    SubscriberType,
    VerificationStatus,
)
from app.domain.identity import email_domain, normalise_email
from app.services.outreach.sender import Clearance, Refusal
from app.services.outreach.unsubscribe import UnsubscribeLinks

DEFAULT_COOLDOWN_DAYS = 180
_COUNTS_AS_CONTACT = (MessageStatus.SENT, MessageStatus.SEND_UNCERTAIN)


class ComplianceGate:
    def __init__(
        self, *, links: UnsubscribeLinks, clock: Clock, cooldown_days: int = DEFAULT_COOLDOWN_DAYS
    ) -> None:
        self._links = links
        self._clock = clock
        self._cooldown_days = cooldown_days

    async def check(
        self, session: AsyncSession, message: EmailMessage, account: ConnectedEmailAccount
    ) -> Clearance | Refusal:
        address = normalise_email(message.to_email)
        if address is None:
            return Refusal("blocked by the compliance gate: the recipient address is not valid")
        contact = await self._contact(session, message, address)
        if contact is None and message.kind is not MessageKind.REPLY:
            return Refusal(
                "blocked by the compliance gate: there is no contact record for this address"
            )

        campaign = await self._campaign(session, message)
        mode = campaign.mode if campaign is not None else account.mode
        settings = await self._campaign_settings(session, campaign)
        link = self._links.link(message.workspace_id, message.id)
        decision = evaluate(
            await self._recipient(session, message, contact, address),
            Context(
                rules=Rules(
                    mode=mode,
                    require_verified_email=settings.require_verified_email if settings else True,
                    allow_role_addresses=settings.allow_role_addresses if settings else True,
                    cooldown_days=self._cooldown_days,
                ),
                sender=await self._sender(session, message.workspace_id, mode, account, link),
                policies=await self._policies(session, message.workspace_id),
                suppressed=await self._suppressed(session, message.workspace_id, address),
            ),
            kind=message.kind,
            now=self._clock.now(),
        )
        if not decision.allowed:
            return Refusal(
                "blocked by the compliance gate: "
                + "; ".join(reason.detail for reason in decision.reasons)
            )
        profile = await self._profile(session, message.workspace_id, mode)
        name = (profile.display_name if profile else None) or account.display_name or ""
        postal = profile.postal_address if profile else None
        return Clearance(
            unsubscribe_https=link,
            footer_text=_footer_text(name, postal, link),
            footer_html=_footer_html(name, postal, link),
        )

    # ── what the database knows ──

    @staticmethod
    async def _contact(
        session: AsyncSession, message: EmailMessage, address: str
    ) -> Contact | None:
        if message.contact_id is not None:
            found = await session.get(Contact, message.contact_id)
            if found is not None and found.workspace_id == message.workspace_id:
                return found
        return (
            await session.execute(
                select(Contact)
                .where(
                    Contact.workspace_id == message.workspace_id,
                    func.lower(Contact.email) == address,
                )
                .order_by(Contact.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _campaign(session: AsyncSession, message: EmailMessage) -> Campaign | None:
        if message.campaign_id is None:
            return None
        found = await session.get(Campaign, message.campaign_id)
        return found if found is not None and found.workspace_id == message.workspace_id else None

    @staticmethod
    async def _campaign_settings(
        session: AsyncSession, campaign: Campaign | None
    ) -> CampaignSettings | None:
        if campaign is None:
            return None
        return (
            await session.execute(
                select(CampaignSettings).where(CampaignSettings.campaign_id == campaign.id)
            )
        ).scalar_one_or_none()

    async def _recipient(
        self, session: AsyncSession, message: EmailMessage, contact: Contact | None, address: str
    ) -> Recipient:
        lead = await session.get(Lead, message.lead_id) if message.lead_id is not None else None
        if lead is not None and lead.workspace_id != message.workspace_id:
            lead = None
        company = (
            await session.get(Company, contact.company_id)
            if contact is not None and contact.company_id is not None
            else None
        )
        last = (
            await session.execute(
                select(
                    func.max(func.coalesce(EmailMessage.sent_at, EmailMessage.claimed_at))
                ).where(
                    EmailMessage.workspace_id == message.workspace_id,
                    func.lower(EmailMessage.to_email) == address,
                    EmailMessage.status.in_(_COUNTS_AS_CONTACT),
                    EmailMessage.id != message.id,
                )
            )
        ).scalar_one()
        return Recipient(
            email=address,
            origin=contact.email_origin if contact else EmailOrigin.MANUAL,
            status=contact.verification_status if contact else VerificationStatus.UNVERIFIED,
            kind=contact.email_kind if contact else EmailKind.UNKNOWN,
            subscriber=contact.subscriber_type if contact else SubscriberType.UNKNOWN,
            basis=contact.lawful_basis if contact else LawfulBasis.NONE,
            country=(company.country if company is not None else None)
            or (contact.country if contact else None),
            source_url=contact.source_url if contact else None,
            last_contacted_at=last,
            lead_stage=lead.stage if lead is not None else None,
        )

    async def _profile(
        self, session: AsyncSession, workspace_id: uuid.UUID, mode: CampaignMode
    ) -> SenderProfile | None:
        return (
            await session.execute(
                select(SenderProfile).where(
                    SenderProfile.workspace_id == workspace_id, SenderProfile.mode == mode
                )
            )
        ).scalar_one_or_none()

    async def _sender(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        mode: CampaignMode,
        account: ConnectedEmailAccount,
        link: str,
    ) -> Sender:
        profile = await self._profile(session, workspace_id, mode)
        return Sender(
            display_name=(profile.display_name if profile else None) or account.display_name,
            postal_address=profile.postal_address if profile else None,
            # RFC 8058 one-click needs an https URL; a plain-http base (development) cannot honour the requirement.
            unsubscribe_ready=link.startswith("https://"),
        )

    @staticmethod
    async def _policies(session: AsyncSession, workspace_id: uuid.UUID) -> dict[str, Policy]:
        rows = (
            await session.execute(
                select(JurisdictionPolicy).where(JurisdictionPolicy.workspace_id == workspace_id)
            )
        ).scalars()
        return {
            row.country.upper(): Policy(
                country=row.country.upper(),
                cold_b2b_marketing=row.cold_b2b_marketing,
                cold_to_individuals=row.cold_to_individuals,
                requires_unsubscribe=row.requires_unsubscribe,
                requires_postal_address=row.requires_postal_address,
                requires_sender_identification=row.requires_sender_identification,
            )
            for row in rows
        }

    @staticmethod
    async def _suppressed(
        session: AsyncSession, workspace_id: uuid.UUID, address: str
    ) -> tuple[Suppressed, ...]:
        """Entries that could apply to this address: any at its mail domain (which includes every spelling of a Gmail
        mailbox), plus every domain-wide entry."""
        domain = email_domain(address)
        rows = (
            await session.execute(
                select(SuppressionEntry).where(
                    SuppressionEntry.workspace_id == workspace_id,
                    or_(
                        SuppressionEntry.domain.is_not(None),
                        func.split_part(func.lower(SuppressionEntry.email), "@", 2) == domain,
                    ),
                )
            )
        ).scalars()
        return tuple(
            Suppressed(email=r.email, domain=r.domain, expires_at=r.expires_at) for r in rows
        )


def _footer_text(name: str, postal: str | None, link: str) -> str:
    lines = ["--", name] if name else ["--"]
    if postal and postal.strip():
        lines.append(postal.strip())
    lines += ["", f"If you would rather not hear from me again, unsubscribe here: {link}"]
    return "\n".join(lines)


def _footer_html(name: str, postal: str | None, link: str) -> str:
    parts = [f"<strong>{escape(name)}</strong>"] if name else []
    if postal and postal.strip():
        parts.append(escape(postal.strip()).replace("\n", "<br>"))
    identity = "<br>".join(parts)
    return (
        '<div style="margin-top:24px;color:#666;font-size:12px;line-height:1.5">'
        f"{identity}<br><br>"
        f'If you would rather not hear from me again, <a href="{escape(link, quote=True)}">unsubscribe here</a>.'
        "</div>"
    )
