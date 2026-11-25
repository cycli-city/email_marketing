"""The built-in country policies: complete, consistent, and never looser than the law's strictest common reading."""

from __future__ import annotations

from collections import Counter

import pytest

from app.domain.compliance import FALLBACK_POLICY
from app.domain.enums import ConsentPolicy
from app.domain.policy_defaults import B2B_OPT_OUT, EU_EEA, default_policies

ROWS = {row.policy.country: row for row in default_policies()}


def test_every_country_appears_exactly_once() -> None:
    countries = [row.policy.country for row in default_policies()]
    assert [c for c, n in Counter(countries).items() if n > 1] == []


def test_codes_are_two_upper_case_letters() -> None:
    assert all(len(c) == 2 and c.isalpha() and c.isupper() for c in ROWS)


def test_there_is_a_fallback_for_everywhere_else_and_it_matches_the_built_in_one() -> None:
    zz = ROWS["ZZ"].policy
    assert (zz.cold_b2b_marketing, zz.cold_to_individuals) == (
        FALLBACK_POLICY.cold_b2b_marketing,
        FALLBACK_POLICY.cold_to_individuals,
    )


def test_the_fallback_is_listed_last() -> None:
    assert default_policies()[-1].policy.country == "ZZ"


def test_every_row_carries_an_explanation() -> None:
    assert all(row.note.strip() for row in default_policies())


def test_the_two_lists_never_overlap() -> None:
    assert set(EU_EEA).isdisjoint(B2B_OPT_OUT)


@pytest.mark.parametrize("country", EU_EEA)
def test_nobody_in_the_eu_or_eea_is_cold_mailed_until_the_owner_has_reviewed_it(
    country: str,
) -> None:
    policy = ROWS[country].policy
    assert policy.cold_b2b_marketing is ConsentPolicy.REQUIRES_CONSENT
    assert policy.cold_to_individuals is ConsentPolicy.REQUIRES_CONSENT


def test_germany_is_never_looser_than_consent() -> None:
    assert ROWS["DE"].policy.cold_b2b_marketing is ConsentPolicy.REQUIRES_CONSENT


@pytest.mark.parametrize("country", sorted(set(ROWS) - {"US", "ZZ"}))
def test_only_the_us_allows_cold_mail_to_individuals_by_default(country: str) -> None:
    assert ROWS[country].policy.cold_to_individuals is not ConsentPolicy.ALLOWED


def test_no_default_blocks_outright_because_that_is_the_owners_call() -> None:
    """``blocked`` cannot be lifted by consent, so it is never a default; the owner chooses it deliberately."""
    assert all(
        ConsentPolicy.BLOCKED not in (row.policy.cold_b2b_marketing, row.policy.cold_to_individuals)
        for row in default_policies()
    )


def test_every_row_requires_identification_an_opt_out_and_a_postal_address() -> None:
    assert all(
        row.policy.requires_unsubscribe
        and row.policy.requires_postal_address
        and row.policy.requires_sender_identification
        for row in default_policies()
    )
