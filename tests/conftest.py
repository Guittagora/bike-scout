from __future__ import annotations

import json
from pathlib import Path

import pytest

from bike_scout.config import EmailSettings, Settings
from bike_scout.models import Listing

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def flight_payload() -> str:
    """Synthetic Next.js flight data containing a product object."""
    return (FIXTURES / "flight_payload.txt").read_text(encoding="utf-8")


@pytest.fixture
def product_page(flight_payload) -> str:
    """A product page that inlines the flight data the way Next.js does, split across chunks."""
    middle = flight_payload.index('"brand_name"')
    chunks = [flight_payload[:middle], flight_payload[middle:]]
    scripts = "".join(f"<script>self.__next_f.push([1,{json.dumps(chunk)}])</script>" for chunk in chunks)
    return f"<html><head><title>Bike</title></head><body><main>...</main>{scripts}</body></html>"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        search_url="https://buycycle.com/it-it/shop/gravel",
        delay_seconds=0,
        email=EmailSettings(enabled=True, sender="me@example.com", recipient="me@example.com", app_password="x"),
    )


def make_listing(**overrides) -> Listing:
    data = dict(
        url="https://buycycle.com/it-it/product/bike-1",
        title="Canyon Grizl",
        brand="Canyon",
        price=1500.0,
        price_label="€ 1.500",
        market_price=2500.0,
        size="M",
        favorites=3,
    )
    data.update(overrides)
    return Listing(**data)


@pytest.fixture
def listing_factory():
    return make_listing
