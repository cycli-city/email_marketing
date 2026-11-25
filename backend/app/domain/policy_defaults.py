"""Conservative default sending policies per country (ADR-010, ADR-043). Pure data.

**These are defaults, not legal advice.** They exist so that a fresh workspace is safe before anyone has reviewed anything,
and they err on the side of *not* sending: where the rule for a country is stricter than the generic case — or simply not
something this project has verified — the default is "requires consent", which the compliance gate treats as "do not send
unless consent is on record". The owner can edit every row in Settings; seeding never overwrites an edit.

How to read a row: ``cold_b2b_marketing`` governs an unsolicited commercial e-mail to an address at a *company*;
``cold_to_individuals`` governs one to a *person* (a personal mailbox, or a sole trader where the law treats them as one).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.compliance import Policy
from app.domain.enums import ConsentPolicy

ALLOWED = ConsentPolicy.ALLOWED
CONSENT = ConsentPolicy.REQUIRES_CONSENT

#: European Union and European Economic Area. Opt-in regimes (ePrivacy + national law) apply to individuals everywhere and to
#: companies in several of these countries; without a verified per-country reading the safe default is consent for both.
EU_EEA = (
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IS", "IE", "IT", "LV", "LI", "LT",
    "LU", "MT", "NL", "NO", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
)  # fmt: skip
#: Countries where business-to-business cold e-mail is generally permitted if the sender is identifiable and offers an
#: opt-out (an opt-out regime), while mail to individuals is stricter or unclear.
B2B_OPT_OUT = (
    "US",
    "CA",
    "GB",
    "AU",
    "NZ",
    "IN",
    "SG",
    "AE",
    "IL",
    "ZA",
    "BR",
    "MX",
    "JP",
    "KR",
    "HK",
    "MY",
    "PH",
    "ID",
    "TH",
    "VN",
)


@dataclass(frozen=True, slots=True)
class DefaultPolicy:
    policy: Policy
    note: str


def _policy(country: str, b2b: ConsentPolicy, individuals: ConsentPolicy) -> Policy:
    return Policy(country=country, cold_b2b_marketing=b2b, cold_to_individuals=individuals)


def default_policies() -> list[DefaultPolicy]:
    """Every default row, one per country, ``ZZ`` (everywhere else) last."""
    rows = [
        DefaultPolicy(
            _policy("US", ALLOWED, ALLOWED),
            "CAN-SPAM: opt-out regime for companies and individuals. Needs an accurate sender, a physical postal address and a working unsubscribe.",
        ),
        DefaultPolicy(
            _policy("CA", ALLOWED, CONSENT),
            "CASL: consent is required, but a business address that is conspicuously published without a 'no unsolicited mail' notice implies it, if the message is relevant to the recipient's role. The gate requires a recorded source page.",
        ),
        DefaultPolicy(
            _policy("GB", ALLOWED, CONSENT),
            "PECR: companies (corporate subscribers) may be mailed with identification and an opt-out; sole traders and partnerships count as individuals and need consent.",
        ),
    ]
    rows += [
        DefaultPolicy(
            _policy(code, ALLOWED, CONSENT),
            "Opt-out regime for companies; individuals need consent. Review before relying on it.",
        )
        for code in B2B_OPT_OUT
        if code not in {"US", "CA", "GB"}
    ]
    rows += [
        DefaultPolicy(
            _policy(code, CONSENT, CONSENT),
            "EU/EEA: opt-in rules apply (to companies as well in several member states, e.g. Germany). Blocked until consent is recorded or the owner reviews and relaxes this row.",
        )
        for code in EU_EEA
    ]
    rows.append(
        DefaultPolicy(
            _policy("ZZ", ALLOWED, CONSENT),
            "Everywhere else: companies may be mailed with identification and an opt-out; individuals need consent.",
        )
    )
    return rows
