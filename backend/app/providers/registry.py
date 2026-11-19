"""Provider adapters and the registry that lets business logic ask for a *capability*, never for a vendor."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.enums import Capability
from app.providers.spec import ProviderSpec


class Provider(ABC):
    """Base class of every adapter. Concrete adapters add the capability's methods (e.g. ``complete`` for LLMs)
    and translate vendor failures into ``app.providers.errors`` types."""

    spec: ProviderSpec

    @property
    @abstractmethod
    def configured(self) -> bool:
        """True when this provider has what it needs (API key etc.). Never raises, never calls the network."""


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[tuple[Capability, str], Provider] = {}

    def register(self, provider: Provider) -> None:
        ident = (provider.spec.capability, provider.spec.key)
        if ident in self._providers:
            raise ValueError(
                f"provider {provider.spec.key!r} already registered for {provider.spec.capability}"
            )
        self._providers[ident] = provider

    def for_capability(self, capability: Capability) -> dict[str, Provider]:
        return {key: p for (cap, key), p in self._providers.items() if cap == capability}

    def get(self, capability: Capability, key: str) -> Provider | None:
        return self._providers.get((capability, key))

    def all(self) -> list[Provider]:
        return list(self._providers.values())
