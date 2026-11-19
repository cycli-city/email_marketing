"""Tests for the pre-public audit. Personal-looking values are assembled at runtime so this file stays clean."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "prepublic_audit.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prepublic_audit", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["prepublic_audit"] = module
    spec.loader.exec_module(module)
    return module


pa = _load()
SHA = "a" * 40


class TestPersonalData:
    def test_real_looking_email_is_flagged_and_masked(self) -> None:
        email = "jane.doe" + "@" + "gmail.com"
        ((rule, preview),) = pa.find_personal_data(f"contact {email} please")
        assert rule == "personal-email"
        assert email not in preview

    @pytest.mark.parametrize(
        "ok",
        [
            "me@example.com",
            "owner@brightsmile.example",
            "x@db.example.com",
            "123+user@users.noreply.github.com",
            "noreply@anthropic.com",
            "support@nexora-ai.co.in",
        ],
    )
    def test_documented_examples_and_noreply_addresses_are_allowed(self, ok: str) -> None:
        assert pa.find_personal_data(f"mail {ok}") == []

    def test_phone_numbers_are_flagged(self) -> None:
        number = "+91 " + "98765" + " " + "43210"
        assert [r for r, _ in pa.find_personal_data(f"call {number}")] == ["phone-number"]

    def test_known_private_values_are_flagged_without_echoing_them(self) -> None:
        secret_address = "Plot 9, Example Nagar, Springfield"
        findings = pa.find_personal_data(
            f"office: {secret_address}", private_values={secret_address}
        )
        assert findings == [("known-private-value", f"<{len(secret_address)} chars, value hidden>")]

    def test_workflow_action_refs_and_versions_are_not_mistaken_for_emails(self) -> None:
        text = "uses: actions/checkout@" + SHA + " # v7.0.1\nnpm i typescript@^5 vitest@5.1.1"
        assert pa.find_personal_data(text) == []

    def test_wording_that_must_not_be_published_is_flagged(self) -> None:
        assert [r for r, _ in pa.find_personal_data("the token was " + "pasted in" + " chat")] == [
            "unpublishable-wording"
        ]


class TestPrivateValueParsing:
    def test_extracts_emails_phones_and_addresses(self) -> None:
        text = (
            "- <A_ADDRESS>  = a" + "@" + "mail.test\n"
            "- <POSTAL_ADDRESS> = 12 Example Road, Springfield  (published on the site)\n"
            "- <PHONE> = +44 " + "20123" + " " + "456789\n"
        )
        values = pa.parse_private_values(text)
        assert "12 Example Road, Springfield" in values
        assert any(v.startswith("+44") for v in values)
        assert "a" + "@" + "mail.test" in values


GOOD = f"""
name: CI
on:
  pull_request:
permissions:
  contents: read
jobs:
  t:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{SHA} # v7
        with:
          persist-credentials: false
      - run: echo hi
"""


class TestWorkflowAudit:
    def test_least_privilege_pinned_workflow_passes(self) -> None:
        assert pa.audit_workflow(GOOD, "ci.yml") == []

    def test_pull_request_target_is_rejected(self) -> None:
        bad = GOOD.replace("pull_request:", "pull_request_target:")
        assert any("pull_request_target" in p for p in pa.audit_workflow(bad, "ci.yml"))

    def test_workflow_run_is_rejected(self) -> None:
        bad = GOOD.replace("pull_request:", "workflow_run:\n    workflows: [x]")
        assert any("workflow_run" in p for p in pa.audit_workflow(bad, "ci.yml"))

    def test_missing_permissions_is_rejected(self) -> None:
        bad = GOOD.replace("permissions:\n  contents: read\n", "")
        assert any("permissions" in p for p in pa.audit_workflow(bad, "ci.yml"))

    def test_unpinned_actions_are_rejected(self) -> None:
        bad = GOOD.replace(f"@{SHA} # v7", "@v4")
        assert any("not pinned" in p for p in pa.audit_workflow(bad, "ci.yml"))

    def test_checkout_without_persist_credentials_false_is_rejected(self) -> None:
        bad = GOOD.replace("        with:\n          persist-credentials: false\n", "")
        assert any("persist-credentials" in p for p in pa.audit_workflow(bad, "ci.yml"))

    def test_repository_secrets_are_rejected_but_github_token_is_fine(self) -> None:
        with_secret = GOOD + "      - run: echo ${{ secrets.MY_API_KEY }}\n"
        assert any("MY_API_KEY" in p for p in pa.audit_workflow(with_secret, "ci.yml"))
        with_token = GOOD + "      - run: echo ${{ secrets.GITHUB_TOKEN }}\n"
        assert pa.audit_workflow(with_token, "ci.yml") == []

    def test_the_real_workflows_in_this_repo_pass(self) -> None:
        workflows = sorted((SCRIPT.parent.parent / ".github" / "workflows").glob("*.yml"))
        assert workflows, "expected workflows to exist"
        problems = [
            p
            for wf in workflows
            for p in pa.audit_workflow(wf.read_text(encoding="utf-8"), wf.name)
        ]
        assert problems == []


class TestFalsePositiveRegressions:
    def test_credentials_in_an_ip_address_url_are_not_emails(self) -> None:
        assert pa.find_personal_data("postgresql://nobody:nopass@127.0.0.1:1/none") == []

    def test_a_script_named_scan_secrets_is_not_a_secret_reference(self) -> None:
        wf = GOOD + "      - run: python3 scripts/scan_secrets.py --tree\n"
        assert pa.audit_workflow(wf, "ci.yml") == []
