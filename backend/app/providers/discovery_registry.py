"""Assembles the lead-discovery providers from settings. Providers without a key are still registered (so the settings page
can list them as "not configured"); the manager simply never routes to an unconfigured one."""

from __future__ import annotations

import httpx

from app.core.clock import Clock
from app.core.config import Settings
from app.providers.adapters.geoapify import GeoapifyProvider
from app.providers.adapters.hunter import HunterProvider
from app.providers.adapters.mailboxvalidator import MailboxValidatorProvider
from app.providers.adapters.search import BraveSearchProvider, TavilyProvider
from app.providers.adapters.verifalia import VerifaliaProvider
from app.providers.registry import ProviderRegistry


def build_discovery_registry(
    settings: Settings, client: httpx.AsyncClient, clock: Clock
) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(
        GeoapifyProvider(api_key=settings.geoapify_api_key, client=client, clock=clock)
    )
    registry.register(HunterProvider(api_key=settings.hunter_api_key, client=client, clock=clock))
    registry.register(
        VerifaliaProvider(
            username=settings.verifalia_username,
            password=settings.verifalia_password,
            client=client,
            clock=clock,
        )
    )
    registry.register(
        MailboxValidatorProvider(
            api_key=settings.mailboxvalidator_api_key, client=client, clock=clock
        )
    )
    registry.register(TavilyProvider(api_key=settings.tavily_api_key, client=client, clock=clock))
    registry.register(
        BraveSearchProvider(api_key=settings.brave_search_api_key, client=client, clock=clock)
    )
    return registry
