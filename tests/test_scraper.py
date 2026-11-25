from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from bike_scout.models import parse_price
from bike_scout.scraper import (
    BlockedError,
    BuycycleScraper,
    check_allowed,
    embedded_payload,
    extract_product_from_page,
    extract_product_json,
    parse_product,
)

URL = "https://buycycle.com/it-it/product/grizl-4242"


class FakeSession:
    """Minimal stand-in for requests.Session returning canned responses by URL."""

    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict]] = []

    def _respond(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.responses[(method, url)] if (method, url) in self.responses else self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result

    def get(self, url, **kwargs):
        return self._respond("GET", url, **kwargs)

    def head(self, url, **kwargs):
        return self._respond("HEAD", url, **kwargs)


def response(status: int = 200, body: str = "", url: str = URL):
    def raise_for_status():
        if status >= 400:
            raise requests.HTTPError(f"{status}", response=SimpleNamespace(status_code=status))

    return SimpleNamespace(status_code=status, content=body.encode(), url=url, raise_for_status=raise_for_status)


def make_scraper(responses) -> tuple[BuycycleScraper, FakeSession]:
    session = FakeSession(responses)
    return BuycycleScraper("a=1", session=session, sleep=lambda _: None), session


# -- pure parsing ------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (1850, 1850.0),
        ("€ 1.500", 1500.0),
        ("€ 599.99", 599.99),
        ("1.234,56 €", 1234.56),
        ("1,234.56", 1234.56),
        ("CHF 2'300", 2300.0),
        ("€ 1.500.000", 1_500_000.0),
        ("n/a", None),
        (None, None),
        (True, None),
    ],
)
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


def test_extract_product_json_skips_nested_objects_and_string_braces(flight_payload):
    data = extract_product_json(flight_payload)
    assert data is not None
    assert data["product_id"] == 4242
    assert data["meta"] == {"currency": "EUR"}  # nested object *before* the anchor is kept
    assert "Curly {braces}" in data["information"]["data"]["description"]["translated_value"]


def test_extract_product_json_missing_anchor():
    assert extract_product_json('{"foo": 1}') is None


def test_parse_product_maps_all_fields(flight_payload):
    listing = parse_product(extract_product_json(flight_payload), URL)
    assert listing.title == "Canyon Grizl CF SL 7"
    assert listing.brand == "Canyon"
    assert listing.price == 1850.0
    assert listing.price_label == "€ 1.850"
    assert listing.market_price == 2600.0
    assert listing.size == "M"
    assert listing.groupset == "Shimano GRX 820"
    assert listing.country == "DE"
    assert listing.favorites == 17
    assert listing.hot_deal is True
    assert listing.sold is False
    assert listing.image_url == "https://img.example.com/4242_350.jpg"
    assert listing.description.startswith("Great bike & well kept.")
    assert "<p>" not in listing.description
    assert listing.published == "2026-10-03"
    assert listing.discount == pytest.approx(1 - 1850 / 2600)


def test_parse_product_handles_sparse_data():
    listing = parse_product({"product_id": 1, "status": "SOLD", "dataUpdatedAt": 1784137010563}, URL)
    assert listing.title == "Unknown"
    assert listing.price is None
    assert listing.sold is True
    assert listing.hot_deal is False
    assert listing.published == "2026-07-15"


def test_parse_product_truncates_long_description():
    data = {"information": {"data": {"description": {"translated_value": "x" * 1000}}}}
    assert len(parse_product(data, URL).description) == 251


# -- HTTP behaviour ----------------------------------------------------------


def test_get_listing_urls_dedupes_and_normalises():
    html = """
      <a href="/it-it/product/bike-1">1</a>
      <a href="/it-it/product/bike-1?ref=grid">dup</a>
      <a href="https://buycycle.com/it-it/product/bike-2">2</a>
      <a href="/it-it/product/bike-3?_rsc=abc">rsc</a>
      <a href="/it-it/shop/gravel">not a product</a>
    """
    scraper, _ = make_scraper({"https://s": response(body=html)})
    assert scraper.get_listing_urls("https://s") == [
        "https://buycycle.com/it-it/product/bike-1",
        "https://buycycle.com/it-it/product/bike-2",
    ]


def test_get_listing_urls_detects_cloudflare():
    scraper, _ = make_scraper({"https://s": response(body="<title>Just a moment...</title>")})
    with pytest.raises(BlockedError):
        scraper.get_listing_urls("https://s")


def test_get_listing_urls_raises_on_http_error():
    scraper, _ = make_scraper({"https://s": response(status=500)})
    with pytest.raises(requests.HTTPError):
        scraper.get_listing_urls("https://s")


def test_fetch_listing_reads_the_product_page_only(product_page):
    scraper, session = make_scraper({URL: response(body=product_page)})
    listing = scraper.fetch_listing(URL)
    assert listing is not None and listing.title == "Canyon Grizl CF SL 7"
    assert [(method, url) for method, url, _ in session.calls] == [("GET", URL)]
    assert "RSC" not in session.calls[0][2].get("headers", {})


def test_extract_product_from_page_reassembles_flight_chunks(product_page):
    assert "__next_f" not in embedded_payload(product_page)
    assert extract_product_from_page(product_page)["product_id"] == 4242


def test_extract_product_from_page_falls_back_to_inline_json():
    page = '<script id="__NEXT_DATA__">{"props":{"product":{"product_id":7,"name":"Tarmac"}}}</script>'
    assert extract_product_from_page(page) == {"product_id": 7, "name": "Tarmac"}


@pytest.mark.parametrize(
    "url",
    [
        "https://buycycle.com/it-it/product/bike?_rsc=abc",
        "https://buycycle.com/it-it/shop/gravel?page=2&rsc=1",
    ],
)
def test_robots_disallowed_urls_are_never_fetched(url):
    scraper, session = make_scraper({})
    with pytest.raises(ValueError, match="robots.txt"):
        scraper.fetch_listing(url)
    with pytest.raises(ValueError, match="robots.txt"):
        scraper.get_listing_urls(url)
    assert session.calls == []
    check_allowed("https://buycycle.com/it-it/product/bike")  # allowed: no exception


def test_fetch_listing_returns_none_without_product_data():
    scraper, _ = make_scraper({URL: response(body="<html>nothing</html>")})
    assert scraper.fetch_listing(URL) is None


@pytest.mark.parametrize(
    "head, expected",
    [
        (response(200), True),
        (response(404), False),
        (response(410), False),
        (response(200, url="https://buycycle.com/it-it/shop/gravel"), False),  # redirected away
        (response(429), None),
        (response(503), None),
        (requests.ConnectionError("boom"), None),  # network errors are *not* "sold"
    ],
)
def test_check_available(head, expected):
    scraper, _ = make_scraper({("HEAD", URL): head})
    assert scraper.check_available(URL) is expected


def test_check_available_falls_back_to_get_on_405():
    scraper, session = make_scraper({("HEAD", URL): response(405), ("GET", URL): response(200)})
    assert scraper.check_available(URL) is True
    assert [c[0] for c in session.calls] == ["HEAD", "GET"]


def test_forbidden_is_reported_as_blocked():
    scraper, _ = make_scraper({"https://s": response(status=403)})
    with pytest.raises(BlockedError, match="bot protection"):
        scraper.get_listing_urls("https://s")
