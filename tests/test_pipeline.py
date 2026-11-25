from __future__ import annotations

from datetime import date

import pytest
import requests

from bike_scout.pipeline import COL_LINK, COL_STATUS, HEADERS, Pipeline, listing_to_row
from bike_scout.scraper import BlockedError
from bike_scout.sheets import MemorySheet, column_letter, row_from_append_response
from bike_scout.storage import Storage

BASE = "https://buycycle.com/it-it/product/"


class FakeScraper:
    def __init__(self, listings=None, urls=None, availability=None, errors=None):
        self.listings = listings or {}
        self.urls = urls if urls is not None else list(self.listings)
        self.availability = availability or {}
        self.errors = errors or {}
        self.checked: list[str] = []

    def get_listing_urls(self, search_url):
        return self.urls

    def fetch_listing(self, url):
        if url in self.errors:
            raise self.errors[url]
        return self.listings.get(url)

    def check_available(self, url):
        self.checked.append(url)
        return self.availability.get(url, True)


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, listing):
        self.sent.append(listing.url)
        return True


@pytest.fixture
def storage():
    with Storage(":memory:") as s:
        yield s


def make_pipeline(settings, scraper, storage, sheet=None, notifier=None):
    return Pipeline(
        settings,
        scraper,
        storage,
        sheet or MemorySheet(),
        notifier or RecordingNotifier(),
        sleep=lambda _: None,
        today=lambda: date(2026, 11, 25),
    )


def sheet_with(*urls_and_status):
    rows = [HEADERS]
    for url, status in urls_and_status:
        row = [""] * len(HEADERS)
        row[COL_LINK], row[COL_STATUS] = url, status
        rows.append(row)
    return MemorySheet(rows)


def test_new_listings_are_stored_appended_and_notified(settings, storage, listing_factory):
    a, b = listing_factory(url=BASE + "a"), listing_factory(url=BASE + "b", title="Other")
    sheet, notifier = MemorySheet(), RecordingNotifier()
    stats = make_pipeline(settings, FakeScraper({a.url: a, b.url: b}), storage, sheet, notifier).run()

    assert (stats.new, stats.updated, stats.alerts) == (2, 0, 2)
    assert sheet.rows[0] == HEADERS
    assert [r[COL_LINK] for r in sheet.rows[1:]] == [a.url, b.url]
    assert storage.get_listing(b.url)["sheet_row_idx"] == 3
    assert notifier.sent == [a.url, b.url]


def test_second_run_updates_in_place_without_new_alerts(settings, storage, listing_factory):
    a = listing_factory(url=BASE + "a")
    sheet, notifier = MemorySheet(), RecordingNotifier()
    make_pipeline(settings, FakeScraper({a.url: a}), storage, sheet, notifier).run()

    cheaper = listing_factory(url=BASE + "a", price=1200.0, price_label="€ 1.200")
    stats = make_pipeline(settings, FakeScraper({a.url: cheaper}), storage, sheet, notifier).run()

    assert (stats.new, stats.updated) == (0, 1)
    assert len(sheet.rows) == 2
    assert sheet.rows[1][HEADERS.index("Price")] == "€ 1.200"
    assert notifier.sent == [a.url]
    assert [h["price_eur"] for h in storage.price_history(storage.get_listing(a.url)["id"])] == [1500.0, 1200.0]


def test_limit_is_respected(settings, storage, listing_factory):
    listings = {BASE + str(i): listing_factory(url=BASE + str(i)) for i in range(5)}
    stats = make_pipeline(settings, FakeScraper(listings), storage).run(limit=2)
    assert stats.new == 2


def test_existing_rows_marked_sold_only_when_definitely_gone(settings, storage, listing_factory):
    storage.create_listing(listing_factory(url=BASE + "gone"))
    sheet = sheet_with(
        (BASE + "gone", "Available"),
        (BASE + "flaky", "Available"),
        (BASE + "ok", "Available"),
        (BASE + "old", "Venduta"),  # legacy label: skipped, not re-checked
    )
    scraper = FakeScraper(urls=[], availability={BASE + "gone": False, BASE + "flaky": None})
    stats = make_pipeline(settings, scraper, storage, sheet).run()

    assert stats.sold == 1
    assert [r[COL_STATUS] for r in sheet.rows[1:]] == ["Sold", "Available", "Available", "Venduta"]
    assert storage.get_listing(BASE + "gone")["status"] == "sold"
    assert BASE + "old" not in scraper.checked


def test_listing_sold_on_site_is_marked_in_db(settings, storage, listing_factory):
    a = listing_factory(url=BASE + "a")
    make_pipeline(settings, FakeScraper({a.url: a}), storage).run()
    sold = listing_factory(url=BASE + "a", sold=True)
    make_pipeline(settings, FakeScraper({a.url: sold}), storage).run()
    assert storage.get_listing(a.url)["status"] == "sold"


def test_http_errors_are_counted_and_skipped(settings, storage, listing_factory):
    ok = listing_factory(url=BASE + "ok")
    scraper = FakeScraper(
        {ok.url: ok},
        urls=[BASE + "err", BASE + "none", ok.url],
        errors={BASE + "err": requests.HTTPError("500")},
    )
    stats = make_pipeline(settings, scraper, storage).run()
    assert (stats.new, stats.failed) == (1, 2)


def test_blocked_stops_the_run(settings, storage, listing_factory):
    ok = listing_factory(url=BASE + "ok")
    scraper = FakeScraper({ok.url: ok}, urls=[BASE + "x", ok.url], errors={BASE + "x": BlockedError("cf")})
    stats = make_pipeline(settings, scraper, storage).run()
    assert stats.blocked and stats.new == 0


def test_listing_to_row_neutralises_formulas(listing_factory):
    row = listing_to_row(listing_factory(title='=HYPERLINK("evil")', image_url='https://i/"x".jpg'), "2026-11-25")
    assert row[HEADERS.index("Model")].startswith("'=")
    assert row[HEADERS.index("Preview")] == '=IMAGE("https://i/%22x%22.jpg")'
    assert row[HEADERS.index("Market Price")] == "€ 2.500"
    assert len(row) == len(HEADERS)


def test_sheet_helpers():
    assert [column_letter(n) for n in (1, 14, 26, 27, 52)] == ["A", "N", "Z", "AA", "AZ"]
    assert row_from_append_response({"updates": {"updatedRange": "Sheet1!A75:N75"}}) == 75
    assert row_from_append_response({}) is None
