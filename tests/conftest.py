from __future__ import annotations

from pathlib import Path

import pytest

from bike_scout.config import EmailSettings, Settings
from bike_scout.models import Listing

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def rsc_payload() -> str:
    return (FIXTURES / "product_rsc.txt").read_text(encoding="utf-8")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        search_url="https://buycycle.com/en-it/shop/gravel",
        delay_seconds=0,
        email=EmailSettings(enabled=True, sender="me@example.com", recipient="me@example.com", app_password="x"),
    )


def make_listing(**overrides) -> Listing:
    data = dict(
        url="https://buycycle.com/en-it/product/bike-1",
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
