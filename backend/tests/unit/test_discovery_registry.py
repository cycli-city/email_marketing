"""Settings → providers, and the guarantee that HTTP-client logging cannot leak a key."""

from __future__ import annotations

import logging

import httpx
import pytest

from app.core.clock import FakeClock
from app.core.logging import configure_logging
from app.domain.enums import Capability
from app.providers.discovery_registry import build_discovery_registry
from tests.helpers import make_settings

KEYS = {
    "geoapify_api_key": "g" * 20,
    "hunter_api_key": "h" * 20,
    "verifalia_username": "user-name",
    "verifalia_password": "pass-word-123",
    "mailboxvalidator_api_key": "m" * 20,
    "tavily_api_key": "tvly-" + "t" * 20,
    "brave_search_api_key": "BSA" + "b" * 20,
}


def registry(**overrides: object):  # type: ignore[no-untyped-def]
    return build_discovery_registry(make_settings(**overrides), httpx.AsyncClient(), FakeClock())


def test_every_capability_has_at_least_one_provider() -> None:
    reg = registry(**KEYS)
    for capability in (
        Capability.COMPANY_DISCOVERY,
        Capability.CONTACT_DISCOVERY,
        Capability.EMAIL_VERIFICATION,
        Capability.SEARCH,
    ):
        assert reg.for_capability(capability), capability


def test_with_all_keys_everything_is_configured() -> None:
    assert all(p.configured for p in registry(**KEYS).all())


def test_without_keys_providers_are_listed_but_not_configured() -> None:
    providers = registry().all()
    assert len(providers) == 6
    assert not any(p.configured for p in providers)


def test_a_provider_is_configured_only_with_its_own_key() -> None:
    reg = registry(geoapify_api_key=KEYS["geoapify_api_key"])
    assert {p.spec.key for p in reg.all() if p.configured} == {"geoapify"}


def test_verification_needs_both_verifalia_credentials() -> None:
    reg = registry(verifalia_username="only-the-user")
    verifalia = reg.get(Capability.EMAIL_VERIFICATION, "verifalia")
    assert verifalia is not None
    assert verifalia.configured is False


def test_only_verifiers_may_receive_a_prospects_address() -> None:
    cleared = {p.spec.key for p in registry(**KEYS).all() if p.spec.may_receive_personal_data}
    assert cleared == {"verifalia", "mailboxvalidator"}


def test_provider_keys_are_unique_per_capability() -> None:
    reg = registry(**KEYS)
    assert len({(p.spec.capability, p.spec.key) for p in reg.all()}) == len(reg.all())


@pytest.mark.parametrize("name", ["httpx", "httpcore"])
def test_http_client_loggers_are_pinned_so_a_key_in_a_url_cannot_be_logged(name: str) -> None:
    logging.getLogger(name).setLevel(logging.DEBUG)
    configure_logging("DEBUG")
    assert logging.getLogger(name).level == logging.WARNING
