"""The owner-facing compliance document must describe the policies the code actually uses."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.policy_defaults import B2B_OPT_OUT, EU_EEA, default_policies

DOC = (Path(__file__).resolve().parents[3] / "docs" / "COMPLIANCE.md").read_text(encoding="utf-8")


def test_the_eu_and_eea_list_in_the_document_is_the_one_in_the_code() -> None:
    assert ", ".join(EU_EEA) in DOC


def test_the_other_opt_out_countries_in_the_document_are_the_ones_in_the_code() -> None:
    others = [code for code in B2B_OPT_OUT if code not in {"US", "CA", "GB"}]
    assert ", ".join(others) in DOC


@pytest.mark.parametrize("code", ["US", "CA", "GB"])
def test_the_named_countries_have_their_own_row(code: str) -> None:
    assert f"| {code} |" in DOC


def test_every_country_the_code_seeds_is_covered_by_a_row_or_a_list() -> None:
    named = {"US", "CA", "GB", "ZZ", *EU_EEA, *B2B_OPT_OUT}
    assert {row.policy.country for row in default_policies()} <= named


def test_the_document_says_what_it_is_not() -> None:
    assert "not legal advice" in DOC.lower()


def test_the_document_states_the_defaults_that_matter_most() -> None:
    assert "nothing is cold-mailed to the EU/EEA" in DOC
    assert "180 days" in DOC
