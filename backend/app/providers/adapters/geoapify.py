"""Geoapify Places — business discovery by category around a point (company discovery capability).

Verified on the owner's key on 2026-11-20 (see tests/unit/test_discovery_adapters.py fixtures for the observed shape):
``GET /v2/places?categories=…&filter=circle:lon,lat,radius&limit=…&apiKey=…`` answers a GeoJSON ``FeatureCollection``;
each ``properties`` has ``name``, ``website``, ``contact.{phone,email}``, ``address_line2``, ``city``, ``country_code``,
``categories``, ``lat``/``lon`` and ``place_id``. The ``contact.email`` is whatever the business published in OpenStreetMap,
so it counts as a *published* address (provenance), not a guess. An invalid key answers ``401 {"message":"Invalid apiKey"}``.

Free tier (docs/PROVIDERS.md §5): 3,000 credits/day, commercial use allowed with attribution.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import httpx
from pydantic import SecretStr

from app.core.clock import Clock
from app.domain.enums import Capability
from app.providers.adapters import _parse as p
from app.providers.discovery import CompanyDiscoveryProvider, CompanyHit, PlaceQuery
from app.providers.errors import InvalidResponseError, PermanentProviderError
from app.providers.http_base import HttpProvider
from app.providers.spec import ProviderResult, ProviderSpec

BASE_URL = "https://api.geoapify.com/v2/places"
MAX_LIMIT = 100
_CATEGORY = re.compile(r"^[a-z_]+(\.[a-z_]+)*$")

#: Vendor-neutral names the campaign layer uses → Geoapify's category tree.
CATEGORY_MAP: dict[str, str] = {
    "dentist": "healthcare.dentist",
    "clinic": "healthcare.clinic_or_praxis",
    "pharmacy": "healthcare.pharmacy",
    "restaurant": "catering.restaurant",
    "cafe": "catering.cafe",
    "hotel": "accommodation.hotel",
    "gym": "sport.fitness",
    "salon": "service.beauty.hairdresser",
    "real_estate": "office.estate_agent",
    "law_firm": "office.lawyer",
    "accountant": "office.accountant",
    "car_repair": "service.vehicle.repair",
    "school": "education.school",
    "veterinary": "pet.veterinary",
}


class GeoapifyProvider(HttpProvider, CompanyDiscoveryProvider):
    def __init__(
        self, *, api_key: SecretStr | None, client: httpx.AsyncClient, clock: Clock
    ) -> None:
        super().__init__(
            client=client, clock=clock, secrets=[api_key.get_secret_value()] if api_key else []
        )
        self._api_key = api_key
        self.spec = ProviderSpec(
            "geoapify",
            Capability.COMPANY_DISCOVERY,
            "Geoapify Places",
            free_tier=True,
            default_priority=10,
            daily_limit=3000,
            rate_limit_per_minute=300,
        )

    @property
    def configured(self) -> bool:
        return self._api_key is not None and bool(self._api_key.get_secret_value())

    async def search_places(self, query: PlaceQuery) -> ProviderResult[list[CompanyHit]]:
        if self._api_key is None:
            raise self.auth_problem("GEOAPIFY_API_KEY is not configured")
        categories = ",".join(self._categories(query.categories))
        response = await self.request(
            "GET",
            BASE_URL,
            params={
                "categories": categories,
                "filter": f"circle:{query.longitude:.6f},{query.latitude:.6f},{int(query.radius_m)}",
                "limit": max(1, min(query.limit, MAX_LIMIT)),
                "offset": max(0, query.offset),
                "conditions": "named",
                "apiKey": self._api_key.get_secret_value(),
            },
        )
        return ProviderResult(self._parse(self.json_body(response)), units_used=Decimal(1))

    @staticmethod
    def _categories(requested: tuple[str, ...]) -> list[str]:
        resolved = [CATEGORY_MAP.get(c, c) for c in requested]
        bad = [c for c in resolved if not _CATEGORY.fullmatch(c)]
        if not resolved or bad:
            raise PermanentProviderError(f"unusable place categories: {bad or 'none given'}")
        return resolved

    @staticmethod
    def _parse(body: Any) -> list[CompanyHit]:
        features = body.get("features") if isinstance(body, dict) else None
        if not isinstance(features, list):
            raise InvalidResponseError("Geoapify answered without a feature list")
        hits: list[CompanyHit] = []
        for feature in features:
            props = feature.get("properties") if isinstance(feature, dict) else None
            if not isinstance(props, dict) or not str(props.get("name") or "").strip():
                continue
            contact = p.mapping(props.get("contact"))
            hits.append(
                CompanyHit(
                    name=str(props["name"]).strip()[:300],
                    website=p.text(props.get("website")),
                    phone=p.text(contact.get("phone")),
                    email=p.email(contact.get("email")),
                    address=p.text(props.get("address_line2") or props.get("formatted")),
                    city=p.text(props.get("city")),
                    country_code=p.text(props.get("country_code")),
                    latitude=p.number(props.get("lat")),
                    longitude=p.number(props.get("lon")),
                    categories=tuple(
                        str(c) for c in props.get("categories") or [] if isinstance(c, str)
                    ),
                    source_id=str(props.get("place_id") or ""),
                )
            )
        return hits
