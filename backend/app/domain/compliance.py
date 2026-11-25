"""The compliance gate: may this message go to this person, from this sender, right now? (pure rules, no I/O — ARCHITECTURE §7)

``evaluate`` answers with *every* reason a message is blocked, not just the first, so the owner sees the whole picture at
once. Nothing here can be weakened by configuration:

* an address that was **guessed** is never mailed;
* a suppressed address or domain is never mailed (an unsubscribe, a hard bounce, a complaint, a manual entry);
* an address we could not verify is not mailed unless the campaign explicitly allows unverified addresses **and** there is
  evidence of where the address was published.

What the owner *can* tune is data: per-country policies (``Policy``), the cooldown, role-address handling. The defaults are
conservative and are **not legal advice** (ADR-010); where the law is stricter than a default, edit the policy.

Job-application mail (``CampaignMode.JOB_HUNTER``) is not marketing: it skips the cold-marketing jurisdiction rules but still
honours suppression, address quality and sender identification.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from app.domain.enums import (
    CampaignMode,
    ConsentPolicy,
    EmailKind,
    EmailOrigin,
    LawfulBasis,
    LeadStage,
    MessageKind,
    SubscriberType,
    VerificationStatus,
)
from app.domain.identity import (
    email_domain,
    email_match_key,
    normalise_email,
)


class Block(StrEnum):
    SUPPRESSED = "suppressed"
    GUESSED_ADDRESS = "guessed_address"
    ADDRESS_INVALID = "address_invalid"
    NOT_VERIFIED = "not_verified"
    ROLE_ADDRESS = "role_address_not_allowed"
    NO_SOURCE_EVIDENCE = "no_source_evidence"
    JURISDICTION_BLOCKED = "jurisdiction_blocked"
    CONSENT_REQUIRED = "consent_required"
    NO_SENDER_IDENTIFICATION = "no_sender_identification"
    NO_POSTAL_ADDRESS = "no_postal_address"
    NO_UNSUBSCRIBE = "no_unsubscribe_mechanism"
    RECENTLY_CONTACTED = "recently_contacted"
    LEAD_CLOSED = "lead_closed"


#: A lead in one of these stages is finished: nothing automatic is ever sent for it again.
CLOSED_STAGES = frozenset(
    {
        LeadStage.UNSUBSCRIBED,
        LeadStage.SUPPRESSED,
        LeadStage.DUPLICATE,
        LeadStage.BOUNCED,
        LeadStage.DISQUALIFIED,
    }
)
#: A follow-up additionally stops when the person has answered, or a human has to look at the lead first.
FOLLOW_UP_STOP_STAGES = CLOSED_STAGES | {
    LeadStage.REPLIED,
    LeadStage.EXHAUSTED,
    LeadStage.NEEDS_REVIEW,
}


@dataclass(frozen=True, slots=True)
class Reason:
    code: Block
    detail: str


@dataclass(frozen=True, slots=True)
class Decision:
    reasons: tuple[Reason, ...]
    #: The lawful basis the decision relied on (for the audit record); ``NONE`` whenever the message is blocked.
    basis: LawfulBasis = LawfulBasis.NONE
    #: The country whose policy was applied (``ZZ`` = the fallback policy), ``None`` for a non-marketing message.
    policy_country: str | None = None

    @property
    def allowed(self) -> bool:
        return not self.reasons

    @property
    def codes(self) -> frozenset[Block]:
        return frozenset(r.code for r in self.reasons)


# ───────────────────────────── inputs ─────────────────────────────


@dataclass(frozen=True, slots=True)
class Recipient:
    email: str
    origin: EmailOrigin
    status: VerificationStatus
    kind: EmailKind
    subscriber: SubscriberType
    basis: LawfulBasis
    country: str | None  # where the recipient is, ISO 3166-1 alpha-2
    source_url: str | None  # the public page the address was found on
    last_contacted_at: datetime | None  # the latest message to this address from *any* campaign
    lead_stage: LeadStage | None = (
        None  # where the lead this message belongs to stands, if it belongs to one
    )


@dataclass(frozen=True, slots=True)
class Policy:
    """One country's rules for cold e-mail. Mirrors ``jurisdiction_policies``."""

    country: str
    cold_b2b_marketing: ConsentPolicy
    cold_to_individuals: ConsentPolicy
    requires_unsubscribe: bool = True
    requires_postal_address: bool = True
    requires_sender_identification: bool = True


#: Used when nothing is configured for a country *and* there is no ``ZZ`` row: business-to-business cold e-mail is allowed
#: (with the identification and opt-out requirements), cold e-mail to individuals needs consent.
FALLBACK_POLICY = Policy("ZZ", ConsentPolicy.ALLOWED, ConsentPolicy.REQUIRES_CONSENT)


@dataclass(frozen=True, slots=True)
class Rules:
    """The campaign settings that bear on a single message."""

    mode: CampaignMode
    require_verified_email: bool = True
    allow_role_addresses: bool = True
    cooldown_days: int = 180


@dataclass(frozen=True, slots=True)
class Sender:
    display_name: str | None
    postal_address: str | None
    unsubscribe_ready: bool  # the message will carry a working unsubscribe link and header


@dataclass(frozen=True, slots=True)
class Suppressed:
    """An active or expired entry of the do-not-contact list."""

    email: str | None = None
    domain: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Context:
    rules: Rules
    sender: Sender
    policies: Mapping[str, Policy] = field(default_factory=dict)
    suppressed: tuple[Suppressed, ...] = ()


# ───────────────────────────── the gate ─────────────────────────────

_STRICTNESS = {
    ConsentPolicy.ALLOWED: 0,
    ConsentPolicy.REQUIRES_CONSENT: 1,
    ConsentPolicy.BLOCKED: 2,
}


def resolve_policy(country: str | None, policies: Mapping[str, Policy]) -> Policy:
    """The policy for a country, else the ``ZZ`` row, else the built-in conservative fallback."""
    code = (country or "").strip().upper()
    return policies.get(code) or policies.get("ZZ") or FALLBACK_POLICY


def evaluate(
    recipient: Recipient, context: Context, *, kind: MessageKind, now: datetime
) -> Decision:
    """Every reason this message may not be sent to this recipient, or an empty (allowed) decision.

    ``MessageKind.REPLY`` is an owner-approved answer to someone who wrote to us: it is not cold outreach, so only
    suppression and sender identification apply.
    """
    reasons: list[Reason] = []
    if normalise_email(recipient.email) is None:
        reasons.append(Reason(Block.ADDRESS_INVALID, "this is not a valid e-mail address"))
    reasons += _suppression(recipient, context.suppressed, now)
    marketing = context.rules.mode is not CampaignMode.JOB_HUNTER
    policy = resolve_policy(recipient.country, context.policies) if marketing else None
    # A reply to someone who wrote to us needs no opt-out footer or postal address.
    cold = marketing and kind is not MessageKind.REPLY
    reasons += _sender(context.sender, policy, marketing=cold)
    basis = LawfulBasis.NONE
    if kind is not MessageKind.REPLY:
        reasons += _lead_open(recipient, kind)
        reasons += _address_quality(recipient, context.rules)
        if kind is MessageKind.INITIAL:
            reasons += _cooldown(recipient, context.rules, now)
        if policy is not None:
            jurisdiction, basis = _jurisdiction(recipient, policy)
            reasons += jurisdiction
    return Decision(
        reasons=tuple(reasons),
        basis=basis if not reasons else LawfulBasis.NONE,
        policy_country=policy.country if policy is not None else None,
    )


def _suppression(
    recipient: Recipient, entries: Iterable[Suppressed], now: datetime
) -> list[Reason]:
    key = email_match_key(recipient.email)  # one identity per mailbox: Gmail ignores dots and +tags
    domain = email_domain(recipient.email)
    for entry in entries:
        if entry.expires_at is not None and entry.expires_at <= now:
            continue
        if entry.email is not None and key is not None and email_match_key(entry.email) == key:
            return [Reason(Block.SUPPRESSED, "this address is on the do-not-contact list")]
        if entry.domain is not None and _domain_covered(entry.domain, domain):
            return [
                Reason(
                    Block.SUPPRESSED,
                    f"the domain {entry.domain.strip().lower()} is on the do-not-contact list",
                )
            ]
    return []


def _domain_covered(suppressed: str, domain: str | None) -> bool:
    """A suppressed domain covers itself and every subdomain of it — never a look-alike that merely ends the same way."""
    wanted = suppressed.strip().lower().lstrip("@").rstrip(".")
    if not wanted or domain is None:
        return False
    return domain == wanted or domain.endswith("." + wanted)


def _address_quality(recipient: Recipient, rules: Rules) -> list[Reason]:
    reasons: list[Reason] = []
    if recipient.origin is EmailOrigin.GUESSED:
        reasons.append(
            Reason(Block.GUESSED_ADDRESS, "the address was guessed, never published or verified")
        )
    if recipient.status is VerificationStatus.INVALID:
        reasons.append(Reason(Block.ADDRESS_INVALID, "the address was found undeliverable"))
    elif recipient.status is VerificationStatus.RISKY:
        reasons.append(Reason(Block.NOT_VERIFIED, "verification flagged the address as risky"))
    elif recipient.status is VerificationStatus.UNVERIFIED and rules.require_verified_email:
        reasons.append(Reason(Block.NOT_VERIFIED, "the address has not been verified"))
    if recipient.kind is EmailKind.ROLE_BASED and not rules.allow_role_addresses:
        reasons.append(
            Reason(Block.ROLE_ADDRESS, "this campaign does not write to shared role addresses")
        )
    # Where an address came from must be on record — unless the owner entered it themselves, or the person consented.
    provenance_known = bool(recipient.source_url) or recipient.origin is EmailOrigin.MANUAL
    if not provenance_known and recipient.basis is not LawfulBasis.CONSENT:
        reasons.append(
            Reason(
                Block.NO_SOURCE_EVIDENCE, "there is no record of where the address was published"
            )
        )
    return reasons


def _lead_open(recipient: Recipient, kind: MessageKind) -> list[Reason]:
    stopping = FOLLOW_UP_STOP_STAGES if kind is MessageKind.FOLLOW_UP else CLOSED_STAGES
    if recipient.lead_stage is not None and recipient.lead_stage in stopping:
        return [
            Reason(
                Block.LEAD_CLOSED,
                f"the lead is {recipient.lead_stage.value}; nothing more is sent for it",
            )
        ]
    return []


def _cooldown(recipient: Recipient, rules: Rules, now: datetime) -> list[Reason]:
    last = recipient.last_contacted_at
    if (
        last is not None
        and rules.cooldown_days > 0
        and now - last < timedelta(days=rules.cooldown_days)
    ):
        return [
            Reason(
                Block.RECENTLY_CONTACTED,
                f"already contacted within the last {rules.cooldown_days} days",
            )
        ]
    return []


def _sender(sender: Sender, policy: Policy | None, *, marketing: bool) -> list[Reason]:
    """Every message names its sender, whatever a policy says; the postal address and unsubscribe mechanism are required only
    where the policy says so."""
    reasons: list[Reason] = []
    if not (sender.display_name or "").strip():
        reasons.append(
            Reason(Block.NO_SENDER_IDENTIFICATION, "the sender's name is not configured")
        )
    if marketing and policy is not None:
        if policy.requires_postal_address and not (sender.postal_address or "").strip():
            reasons.append(
                Reason(Block.NO_POSTAL_ADDRESS, "the sender's postal address is not configured")
            )
        if policy.requires_unsubscribe and not sender.unsubscribe_ready:
            reasons.append(
                Reason(Block.NO_UNSUBSCRIBE, "the message has no working unsubscribe mechanism")
            )
    return reasons


def _jurisdiction(recipient: Recipient, policy: Policy) -> tuple[list[Reason], LawfulBasis]:
    """Apply the country's rule for this kind of subscriber. An unknown subscriber is treated under the *stricter* rule."""
    if recipient.subscriber is SubscriberType.CORPORATE:
        rule = policy.cold_b2b_marketing
    elif recipient.subscriber is SubscriberType.INDIVIDUAL:
        rule = policy.cold_to_individuals
    else:
        rule = max(
            policy.cold_b2b_marketing, policy.cold_to_individuals, key=_STRICTNESS.__getitem__
        )
    if recipient.basis is LawfulBasis.CONSENT and rule is not ConsentPolicy.BLOCKED:
        return [], LawfulBasis.CONSENT  # consent on record satisfies a consent requirement
    if rule is ConsentPolicy.BLOCKED:
        return [
            Reason(
                Block.JURISDICTION_BLOCKED,
                f"cold e-mail is blocked by the policy for {policy.country}",
            )
        ], LawfulBasis.NONE
    if rule is ConsentPolicy.REQUIRES_CONSENT:
        return [
            Reason(
                Block.CONSENT_REQUIRED, f"the policy for {policy.country} requires recorded consent"
            )
        ], LawfulBasis.NONE
    # Allowed: legitimate interest, or implied consent from conspicuous publication when the source says so.
    if recipient.basis is LawfulBasis.IMPLIED_PUBLICATION:
        return [], LawfulBasis.IMPLIED_PUBLICATION
    return [], LawfulBasis.LEGITIMATE_INTEREST
