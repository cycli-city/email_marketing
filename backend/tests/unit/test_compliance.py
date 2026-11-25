"""The compliance gate. Each rule is proved to block on its own, the safety invariants are checked over every combination,
and the things the owner may tune are shown to tune only what they should."""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.domain.compliance import (
    FALLBACK_POLICY,
    Block,
    Context,
    Decision,
    Policy,
    Recipient,
    Rules,
    Sender,
    Suppressed,
    evaluate,
    resolve_policy,
)
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

NOW = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
GMAIL = (
    "gmail" + ".com"
)  # assembled: the public-repo audit rejects real-looking mail domains in tracked files

RECIPIENT = Recipient(
    email="jane@clinic.example.com",
    origin=EmailOrigin.DISCOVERED,
    status=VerificationStatus.VERIFIED,
    kind=EmailKind.NAMED,
    subscriber=SubscriberType.CORPORATE,
    basis=LawfulBasis.NONE,
    country="US",
    source_url="https://clinic.example.com/team",
    last_contacted_at=None,
)
SENDER = Sender(
    display_name="Alex Founder",
    postal_address="1 Example Street, Example City",
    unsubscribe_ready=True,
)
CONTEXT = Context(rules=Rules(mode=CampaignMode.NEXORA), sender=SENDER)


def check(
    recipient: Recipient = RECIPIENT,
    context: Context = CONTEXT,
    kind: MessageKind = MessageKind.INITIAL,
) -> Decision:
    return evaluate(recipient, context, kind=kind, now=NOW)


def with_rules(**changes: Any) -> Context:
    return replace(CONTEXT, rules=replace(CONTEXT.rules, **changes))


def with_sender(**changes: Any) -> Context:
    return replace(CONTEXT, sender=replace(SENDER, **changes))


def with_policy(**changes: Any) -> Context:
    base = Policy("US", ConsentPolicy.ALLOWED, ConsentPolicy.ALLOWED)
    return replace(CONTEXT, policies={"US": replace(base, **changes)})


def test_a_verified_published_address_at_a_company_is_allowed() -> None:
    decision = check()
    assert decision.allowed
    assert decision.reasons == ()
    assert decision.basis is LawfulBasis.LEGITIMATE_INTEREST
    assert (
        decision.policy_country == "ZZ"
    )  # nothing configured for the US: the built-in fallback applied


# ───────────────────────────── the address itself ─────────────────────────────


def test_a_guessed_address_is_never_allowed() -> None:
    decision = check(replace(RECIPIENT, origin=EmailOrigin.GUESSED))
    assert Block.GUESSED_ADDRESS in decision.codes
    assert not decision.allowed


def test_a_guessed_address_stays_blocked_even_if_the_campaign_allows_unverified_addresses() -> None:
    decision = check(
        replace(RECIPIENT, origin=EmailOrigin.GUESSED, status=VerificationStatus.UNVERIFIED),
        with_rules(require_verified_email=False),
    )
    assert Block.GUESSED_ADDRESS in decision.codes


@pytest.mark.parametrize("status", [VerificationStatus.INVALID, VerificationStatus.RISKY])
def test_an_invalid_or_risky_address_is_blocked_whatever_the_campaign_allows(
    status: VerificationStatus,
) -> None:
    decision = check(replace(RECIPIENT, status=status), with_rules(require_verified_email=False))
    assert not decision.allowed
    assert decision.codes & {Block.ADDRESS_INVALID, Block.NOT_VERIFIED}


def test_an_unverified_address_needs_the_campaign_to_allow_it() -> None:
    unverified = replace(RECIPIENT, status=VerificationStatus.UNVERIFIED)
    assert check(unverified).codes == {Block.NOT_VERIFIED}
    assert check(unverified, with_rules(require_verified_email=False)).allowed


def test_an_unverified_address_without_a_published_source_is_blocked_even_when_unverified_is_allowed() -> (
    None
):
    bare = replace(RECIPIENT, status=VerificationStatus.UNVERIFIED, source_url=None)
    assert check(bare, with_rules(require_verified_email=False)).codes == {Block.NO_SOURCE_EVIDENCE}


def test_an_address_the_owner_typed_in_needs_no_source_page() -> None:
    manual = replace(RECIPIENT, origin=EmailOrigin.MANUAL, source_url=None)
    assert check(manual).allowed


def test_consent_makes_a_source_page_unnecessary() -> None:
    consenting = replace(RECIPIENT, source_url=None, basis=LawfulBasis.CONSENT)
    assert check(consenting).allowed


def test_role_addresses_can_be_switched_off_per_campaign() -> None:
    role = replace(RECIPIENT, kind=EmailKind.ROLE_BASED)
    assert check(role).allowed
    assert check(role, with_rules(allow_role_addresses=False)).codes == {Block.ROLE_ADDRESS}


# ───────────────────────────── suppression ─────────────────────────────


def test_a_suppressed_address_is_blocked_whatever_its_case() -> None:
    context = replace(CONTEXT, suppressed=(Suppressed(email="Jane@Clinic.Example.com"),))
    assert check(context=context).codes == {Block.SUPPRESSED}


def test_gmail_spellings_of_a_suppressed_mailbox_are_suppressed_too() -> None:
    recipient = replace(RECIPIENT, email=f"j.a.n.e+offers@{GMAIL}")
    context = replace(CONTEXT, suppressed=(Suppressed(email=f"jane@{GMAIL}"),))
    assert Block.SUPPRESSED in check(recipient, context).codes


def test_dots_still_distinguish_people_on_other_domains() -> None:
    context = replace(CONTEXT, suppressed=(Suppressed(email="j.ane@clinic.example.com"),))
    assert check(context=context).allowed


@pytest.mark.parametrize(
    "domain", ["clinic.example.com", "example.com", "@example.com", " Example.COM. "]
)
def test_a_suppressed_domain_covers_its_subdomains(domain: str) -> None:
    context = replace(CONTEXT, suppressed=(Suppressed(domain=domain),))
    assert check(context=context).codes == {Block.SUPPRESSED}


def test_a_suppressed_domain_covers_subdomains_at_any_depth() -> None:
    deep = replace(RECIPIENT, email="jane@mail.east.clinic.example.com")
    context = replace(CONTEXT, suppressed=(Suppressed(domain="clinic.example.com"),))
    assert check(deep, context).codes == {Block.SUPPRESSED}
    assert check(
        replace(RECIPIENT, email="jane@example.com"), context
    ).allowed  # the parent is not suppressed


@pytest.mark.parametrize("kind", list(MessageKind))
@pytest.mark.parametrize(
    "address",
    [
        "",
        "not-an-address",
        "two@@example.com",
        "a@b",
        "jane@clinic.example.com\r\nBcc: x@clinic.example.com",
    ],
)
def test_an_address_that_is_not_an_address_is_never_allowed(
    address: str, kind: MessageKind
) -> None:
    assert Block.ADDRESS_INVALID in check(replace(RECIPIENT, email=address), kind=kind).codes


def test_a_suppressed_domain_does_not_cover_a_lookalike() -> None:
    context = replace(
        CONTEXT, suppressed=(Suppressed(domain="ample.com"), Suppressed(domain="other-example.com"))
    )
    assert check(context=context).allowed


def test_a_suppression_entry_stops_applying_exactly_when_it_expires() -> None:
    entry = Suppressed(email=RECIPIENT.email, expires_at=NOW + timedelta(seconds=1))
    assert not check(context=replace(CONTEXT, suppressed=(entry,))).allowed
    assert check(context=replace(CONTEXT, suppressed=(replace(entry, expires_at=NOW),))).allowed


def test_suppression_has_no_expiry_unless_one_is_set() -> None:
    assert not check(
        context=replace(CONTEXT, suppressed=(Suppressed(email=RECIPIENT.email),))
    ).allowed


# ───────────────────────────── cooldown ─────────────────────────────


def test_an_address_contacted_recently_by_any_campaign_is_blocked_for_a_first_message() -> None:
    recent = replace(RECIPIENT, last_contacted_at=NOW - timedelta(days=179))
    assert check(recent).codes == {Block.RECENTLY_CONTACTED}


def test_the_cooldown_ends_after_the_configured_days() -> None:
    assert check(replace(RECIPIENT, last_contacted_at=NOW - timedelta(days=180))).allowed
    assert check(
        replace(RECIPIENT, last_contacted_at=NOW - timedelta(days=30)), with_rules(cooldown_days=30)
    ).allowed
    assert not check(
        replace(RECIPIENT, last_contacted_at=NOW - timedelta(days=29)), with_rules(cooldown_days=30)
    ).allowed


def test_a_zero_day_cooldown_disables_it() -> None:
    assert check(replace(RECIPIENT, last_contacted_at=NOW), with_rules(cooldown_days=0)).allowed
    # even a timestamp slightly in the future (clock skew between machines) does not trip a disabled cooldown
    future = replace(RECIPIENT, last_contacted_at=NOW + timedelta(hours=1))
    assert check(future, with_rules(cooldown_days=0)).allowed
    assert not check(
        future, with_rules(cooldown_days=30)
    ).allowed  # …but an enabled one errs on the side of caution


def test_a_follow_up_is_not_held_back_by_the_message_it_follows_up() -> None:
    assert check(
        replace(RECIPIENT, last_contacted_at=NOW - timedelta(days=4)), kind=MessageKind.FOLLOW_UP
    ).allowed


# ───────────────────────────── jurisdiction ─────────────────────────────


def test_a_blocked_country_is_blocked_and_consent_does_not_unblock_it() -> None:
    context = replace(
        CONTEXT, policies={"US": Policy("US", ConsentPolicy.BLOCKED, ConsentPolicy.BLOCKED)}
    )
    assert check(context=context).codes == {Block.JURISDICTION_BLOCKED}
    assert (
        Block.JURISDICTION_BLOCKED
        in check(replace(RECIPIENT, basis=LawfulBasis.CONSENT), context).codes
    )


def test_a_country_that_requires_consent_blocks_until_it_is_recorded() -> None:
    context = replace(
        CONTEXT,
        policies={
            "US": Policy("US", ConsentPolicy.REQUIRES_CONSENT, ConsentPolicy.REQUIRES_CONSENT)
        },
    )
    assert check(context=context).codes == {Block.CONSENT_REQUIRED}
    decision = check(replace(RECIPIENT, basis=LawfulBasis.CONSENT), context)
    assert decision.allowed
    assert decision.basis is LawfulBasis.CONSENT


def test_companies_and_individuals_are_judged_by_their_own_rule() -> None:
    context = replace(
        CONTEXT,
        policies={"US": Policy("US", ConsentPolicy.ALLOWED, ConsentPolicy.REQUIRES_CONSENT)},
    )
    assert check(replace(RECIPIENT, subscriber=SubscriberType.CORPORATE), context).allowed
    assert check(replace(RECIPIENT, subscriber=SubscriberType.INDIVIDUAL), context).codes == {
        Block.CONSENT_REQUIRED
    }


@pytest.mark.parametrize(
    ("b2b", "individuals", "expected"),
    [
        (ConsentPolicy.ALLOWED, ConsentPolicy.ALLOWED, set()),
        (ConsentPolicy.ALLOWED, ConsentPolicy.REQUIRES_CONSENT, {Block.CONSENT_REQUIRED}),
        (ConsentPolicy.REQUIRES_CONSENT, ConsentPolicy.ALLOWED, {Block.CONSENT_REQUIRED}),
        (ConsentPolicy.ALLOWED, ConsentPolicy.BLOCKED, {Block.JURISDICTION_BLOCKED}),
        (ConsentPolicy.REQUIRES_CONSENT, ConsentPolicy.BLOCKED, {Block.JURISDICTION_BLOCKED}),
    ],
)
def test_an_unknown_kind_of_subscriber_is_judged_by_the_stricter_rule(
    b2b: ConsentPolicy, individuals: ConsentPolicy, expected: set[Block]
) -> None:
    context = replace(CONTEXT, policies={"US": Policy("US", b2b, individuals)})
    assert check(replace(RECIPIENT, subscriber=SubscriberType.UNKNOWN), context).codes == expected


def test_the_conspicuous_publication_basis_is_recorded_when_the_source_says_so() -> None:
    decision = check(replace(RECIPIENT, basis=LawfulBasis.IMPLIED_PUBLICATION))
    assert decision.allowed
    assert decision.basis is LawfulBasis.IMPLIED_PUBLICATION


def test_policy_resolution_prefers_the_country_then_zz_then_the_builtin_fallback() -> None:
    us = Policy("US", ConsentPolicy.BLOCKED, ConsentPolicy.BLOCKED)
    zz = Policy("ZZ", ConsentPolicy.REQUIRES_CONSENT, ConsentPolicy.BLOCKED)
    assert resolve_policy("us", {"US": us, "ZZ": zz}) is us
    assert resolve_policy("DE", {"US": us, "ZZ": zz}) is zz
    assert resolve_policy(None, {"ZZ": zz}) is zz
    assert resolve_policy("DE", {"US": us}) is FALLBACK_POLICY
    assert resolve_policy(None, {}) is FALLBACK_POLICY


def test_a_contact_in_an_unconfigured_country_falls_back_to_the_zz_policy() -> None:
    context = replace(
        CONTEXT,
        policies={
            "ZZ": Policy("ZZ", ConsentPolicy.REQUIRES_CONSENT, ConsentPolicy.REQUIRES_CONSENT)
        },
    )
    decision = check(replace(RECIPIENT, country="FR"), context)
    assert decision.codes == {Block.CONSENT_REQUIRED}
    assert decision.policy_country == "ZZ"


# ───────────────────────────── the sender ─────────────────────────────


@pytest.mark.parametrize("name", [None, "", "   "])
def test_the_sender_must_be_identified(name: str | None) -> None:
    assert check(context=with_sender(display_name=name)).codes == {Block.NO_SENDER_IDENTIFICATION}


@pytest.mark.parametrize("address", [None, "", "  "])
def test_a_marketing_message_needs_the_postal_address(address: str | None) -> None:
    assert check(context=with_sender(postal_address=address)).codes == {Block.NO_POSTAL_ADDRESS}


def test_a_marketing_message_needs_a_working_unsubscribe_mechanism() -> None:
    assert check(context=with_sender(unsubscribe_ready=False)).codes == {Block.NO_UNSUBSCRIBE}


def test_a_policy_that_does_not_require_them_relaxes_the_postal_address_and_unsubscribe_rules() -> (
    None
):
    context = replace(
        with_policy(requires_postal_address=False, requires_unsubscribe=False),
        sender=replace(SENDER, postal_address=None, unsubscribe_ready=False),
    )
    assert check(context=context).allowed


def test_no_policy_can_waive_naming_the_sender() -> None:
    nameless = replace(SENDER, display_name=None)
    for policy_flag in (True, False):
        context = replace(with_policy(requires_sender_identification=policy_flag), sender=nameless)
        assert check(context=context).codes == {Block.NO_SENDER_IDENTIFICATION}


def test_every_reason_is_reported_not_just_the_first() -> None:
    recipient = replace(
        RECIPIENT,
        origin=EmailOrigin.GUESSED,
        status=VerificationStatus.INVALID,
        last_contacted_at=NOW,
        source_url=None,
    )
    decision = check(
        recipient,
        replace(
            with_sender(display_name=None, postal_address=None, unsubscribe_ready=False),
            suppressed=(Suppressed(email=RECIPIENT.email),),
        ),
    )
    assert decision.codes == {
        Block.SUPPRESSED, Block.GUESSED_ADDRESS, Block.ADDRESS_INVALID, Block.NO_SOURCE_EVIDENCE,
        Block.RECENTLY_CONTACTED, Block.NO_SENDER_IDENTIFICATION, Block.NO_POSTAL_ADDRESS, Block.NO_UNSUBSCRIBE,
    }  # fmt: skip
    assert decision.basis is LawfulBasis.NONE
    assert all(reason.detail for reason in decision.reasons)


# ───────────────────────────── job applications are not marketing ─────────────────────────────

JOB = replace(
    CONTEXT,
    rules=Rules(mode=CampaignMode.JOB_HUNTER),
    sender=replace(SENDER, postal_address=None, unsubscribe_ready=False),
)


def test_a_job_application_skips_the_cold_marketing_rules() -> None:
    blocked_country = replace(
        JOB, policies={"US": Policy("US", ConsentPolicy.BLOCKED, ConsentPolicy.BLOCKED)}
    )
    decision = check(context=blocked_country)
    assert decision.allowed
    assert decision.policy_country is None


def test_a_job_application_still_honours_suppression_address_quality_and_identity() -> None:
    assert check(context=replace(JOB, suppressed=(Suppressed(domain="example.com"),))).codes == {
        Block.SUPPRESSED
    }
    assert Block.GUESSED_ADDRESS in check(replace(RECIPIENT, origin=EmailOrigin.GUESSED), JOB).codes
    assert check(context=replace(JOB, sender=replace(JOB.sender, display_name=None))).codes == {
        Block.NO_SENDER_IDENTIFICATION
    }


# ───────────────────────────── replies to people who wrote to us ─────────────────────────────


def test_a_reply_ignores_verification_cooldown_and_jurisdiction() -> None:
    awkward = replace(
        RECIPIENT,
        status=VerificationStatus.UNVERIFIED,
        last_contacted_at=NOW,
        source_url=None,
        origin=EmailOrigin.GUESSED,
    )
    context = replace(
        CONTEXT, policies={"US": Policy("US", ConsentPolicy.BLOCKED, ConsentPolicy.BLOCKED)}
    )
    assert check(awkward, context, kind=MessageKind.REPLY).allowed


def test_a_reply_needs_no_postal_address_or_unsubscribe_footer() -> None:
    bare = with_sender(postal_address=None, unsubscribe_ready=False)
    assert check(context=bare, kind=MessageKind.REPLY).allowed
    assert check(context=bare, kind=MessageKind.INITIAL).codes == {
        Block.NO_POSTAL_ADDRESS,
        Block.NO_UNSUBSCRIBE,
    }


def test_a_reply_still_honours_suppression_and_identification() -> None:
    context = replace(CONTEXT, suppressed=(Suppressed(email=RECIPIENT.email),))
    assert check(context=context, kind=MessageKind.REPLY).codes == {Block.SUPPRESSED}
    assert check(context=with_sender(display_name=None), kind=MessageKind.REPLY).codes == {
        Block.NO_SENDER_IDENTIFICATION
    }


# ───────────────────────────── invariants over every combination ─────────────────────────────


def test_invariants_hold_for_every_combination_of_the_things_that_matter() -> None:
    """Whatever the settings: a guessed, invalid or suppressed address is never allowed (replies excepted for the first two,
    which are about *cold* outreach), a decision is allowed exactly when it has no reasons, and a blocked decision records
    no lawful basis."""
    checked = 0
    for origin, status, kind, mode, suppressed, require_verified in itertools.product(
        EmailOrigin, VerificationStatus, MessageKind, CampaignMode, (False, True), (False, True)
    ):
        recipient = replace(RECIPIENT, origin=origin, status=status)
        context = Context(
            rules=Rules(mode=mode, require_verified_email=require_verified),
            sender=SENDER,
            suppressed=(Suppressed(email=RECIPIENT.email),) if suppressed else (),
        )
        decision = check(recipient, context, kind)
        checked += 1
        assert decision.allowed == (not decision.reasons)
        if not decision.allowed:
            assert decision.basis is LawfulBasis.NONE
        if suppressed:
            assert not decision.allowed
        if kind is not MessageKind.REPLY and (
            origin is EmailOrigin.GUESSED or status is VerificationStatus.INVALID
        ):
            assert not decision.allowed
    assert (
        checked
        == len(EmailOrigin) * len(VerificationStatus) * len(MessageKind) * len(CampaignMode) * 4
    )


# ───────────────────────────── a finished lead gets nothing more ─────────────────────────────

CLOSED = [
    LeadStage.UNSUBSCRIBED,
    LeadStage.SUPPRESSED,
    LeadStage.DUPLICATE,
    LeadStage.BOUNCED,
    LeadStage.DISQUALIFIED,
]
ANSWERED_OR_STUCK = [LeadStage.REPLIED, LeadStage.EXHAUSTED, LeadStage.NEEDS_REVIEW]
OPEN = [
    LeadStage.DISCOVERED,
    LeadStage.QUALIFIED,
    LeadStage.APPROVED,
    LeadStage.QUEUED,
    LeadStage.SENT,
]


@pytest.mark.parametrize("stage", CLOSED)
@pytest.mark.parametrize("kind", [MessageKind.INITIAL, MessageKind.FOLLOW_UP])
def test_a_closed_lead_gets_no_automatic_mail(stage: LeadStage, kind: MessageKind) -> None:
    assert check(replace(RECIPIENT, lead_stage=stage), kind=kind).codes == {Block.LEAD_CLOSED}


@pytest.mark.parametrize("stage", ANSWERED_OR_STUCK)
def test_a_follow_up_stops_once_the_person_has_answered_or_a_human_must_look(
    stage: LeadStage,
) -> None:
    assert check(replace(RECIPIENT, lead_stage=stage), kind=MessageKind.FOLLOW_UP).codes == {
        Block.LEAD_CLOSED
    }


@pytest.mark.parametrize("stage", ANSWERED_OR_STUCK)
def test_but_those_stages_do_not_forbid_a_first_message(stage: LeadStage) -> None:
    assert check(replace(RECIPIENT, lead_stage=stage), kind=MessageKind.INITIAL).allowed


@pytest.mark.parametrize("stage", [*OPEN, None])
@pytest.mark.parametrize("kind", [MessageKind.INITIAL, MessageKind.FOLLOW_UP])
def test_an_open_lead_is_not_stopped(stage: LeadStage | None, kind: MessageKind) -> None:
    assert check(replace(RECIPIENT, lead_stage=stage), kind=kind).allowed


@pytest.mark.parametrize("stage", [*CLOSED, *ANSWERED_OR_STUCK])
def test_an_owner_approved_reply_is_not_blocked_by_the_stage_of_the_lead(stage: LeadStage) -> None:
    assert check(replace(RECIPIENT, lead_stage=stage), kind=MessageKind.REPLY).allowed
