"""Orchestration: search -> fetch -> persist -> sheet -> alert."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import requests

from .config import Settings
from .models import Listing
from .notifier import EmailNotifier
from .scraper import BlockedError, BuycycleScraper
from .sheets import Sheet
from .storage import STATUS_SOLD, Storage

log = logging.getLogger(__name__)

HEADERS = [
    "Last Checked",
    "Published",
    "Preview",
    "Model",
    "Size",
    "Groupset",
    "Price",
    "Market Price",
    "Hot Deal",
    "Country",
    "Favorites",
    "Status",
    "Description",
    "Link",
]
COL_STATUS = HEADERS.index("Status")
COL_LINK = HEADERS.index("Link")
STATUS_LABEL_AVAILABLE = "Available"
STATUS_LABEL_SOLD = "Sold"
# Labels written by older (Italian) versions of the tool are still recognised.
SOLD_LABELS = {STATUS_LABEL_SOLD.lower(), "venduta"}
_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _safe_text(value: str) -> str:
    """Stop scraped text from being interpreted as a spreadsheet formula."""
    return "'" + value if value.startswith(_FORMULA_PREFIXES) else value


def listing_to_row(listing: Listing, checked_on: str) -> list[object]:
    preview = f'=IMAGE("{listing.image_url.replace(chr(34), "%22")}")' if listing.image_url else ""
    market = f"€ {listing.market_price:,.0f}".replace(",", ".") if listing.market_price else ""
    return [
        checked_on,
        listing.published or "",
        preview,
        _safe_text(listing.title),
        _safe_text(listing.size),
        _safe_text(listing.groupset),
        listing.price_label,
        market,
        "Yes" if listing.hot_deal else "No",
        _safe_text(listing.country),
        listing.favorites,
        STATUS_LABEL_SOLD if listing.sold else STATUS_LABEL_AVAILABLE,
        _safe_text(listing.description),
        listing.url,
    ]


@dataclass
class RunStats:
    new: int = 0
    updated: int = 0
    sold: int = 0
    failed: int = 0
    alerts: int = 0
    blocked: bool = False

    def __str__(self) -> str:
        return (
            f"{self.new} new, {self.updated} updated, {self.sold} marked sold, "
            f"{self.failed} failed, {self.alerts} alerts" + (" (stopped: blocked)" if self.blocked else "")
        )


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        scraper: BuycycleScraper,
        storage: Storage,
        sheet: Sheet,
        notifier: EmailNotifier,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.settings = settings
        self.scraper = scraper
        self.storage = storage
        self.sheet = sheet
        self.notifier = notifier
        self._sleep = sleep
        self._today = today
        self.stats = RunStats()
        self.row_by_url: dict[str, int] = {}

    def run(self, limit: int | None = None) -> RunStats:
        rows = self.sheet.ensure_headers(HEADERS)
        self.row_by_url = {row[COL_LINK]: i + 2 for i, row in enumerate(rows) if len(row) > COL_LINK and row[COL_LINK]}

        if self.settings.check_existing_first:
            self.check_existing(rows)
            if self.stats.blocked:
                return self.stats

        try:
            urls = self.scraper.get_listing_urls(self.settings.search_url)
        except (BlockedError, requests.RequestException) as exc:
            log.error("Could not read search page: %s", exc)
            self.stats.blocked = isinstance(exc, BlockedError)
            return self.stats

        limit = limit or self.settings.max_listings
        log.info("Found %d listings, processing up to %d", len(urls), limit)
        for i, url in enumerate(urls[:limit], start=1):
            log.info("[%d/%d] %s", i, min(limit, len(urls)), url.rsplit("/", 1)[-1])
            try:
                listing = self.scraper.fetch_listing(url)
            except BlockedError as exc:
                log.error("%s — stopping run", exc)
                self.stats.blocked = True
                break
            except requests.RequestException as exc:
                log.warning("Skipping %s: %s", url, exc)
                self.stats.failed += 1
                continue
            if listing is None:
                self.stats.failed += 1
                continue
            self.process(listing)
            self._sleep(self.settings.delay_seconds)
        return self.stats

    # -- steps -------------------------------------------------------------
    def check_existing(self, rows: list[list[str]]) -> None:
        """Re-check listings already in the sheet and flag the ones that disappeared."""
        log.info("Checking %d existing listings (method=%s)", len(self.row_by_url), self.settings.check_method)
        for url, row_idx in self.row_by_url.items():
            row = rows[row_idx - 2]
            if len(row) > COL_STATUS and row[COL_STATUS].strip().lower() in SOLD_LABELS:
                continue
            try:
                available = self._is_available(url)
            except BlockedError as exc:
                log.error("%s — stopping run", exc)
                self.stats.blocked = True
                return
            if available is None:
                log.warning("Availability of %s unknown; leaving it unchanged", url)
            elif not available:
                self._mark_sold(url, row_idx)
            self._sleep(self.settings.delay_seconds)

    def _is_available(self, url: str) -> bool | None:
        if self.settings.check_method == "head":
            return self.scraper.check_available(url)
        try:
            listing = self.scraper.fetch_listing(url)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            return False if status in (404, 410) else None
        except requests.RequestException:
            return None
        return None if listing is None else not listing.sold

    def _mark_sold(self, url: str, row_idx: int) -> None:
        existing = self.storage.get_listing(url)
        if existing:
            self.storage.mark_sold(existing["id"])
        self.sheet.update_cell(row_idx, COL_STATUS + 1, STATUS_LABEL_SOLD)
        self.stats.sold += 1
        log.info("Marked as sold: %s (row %d)", url, row_idx)

    def process(self, listing: Listing) -> None:
        existing = self.storage.get_listing(listing.url)
        if existing:
            listing_id = existing["id"]
            self.storage.record_observation(listing_id, listing)
            if listing.sold and existing["status"] != STATUS_SOLD:
                self.storage.mark_sold(listing_id)
        else:
            listing_id = self.storage.create_listing(listing)

        row = listing_to_row(listing, self._today().isoformat())
        if listing.url in self.row_by_url:
            self.sheet.update_row(self.row_by_url[listing.url], row)
        else:
            self.row_by_url[listing.url] = self.sheet.append_row(row)
            self.storage.update_listing(listing_id, sheet_row_idx=self.row_by_url[listing.url])

        if existing:
            self.stats.updated += 1
            log.info("  updated: %s (%s)", listing.title, listing.price_label)
        else:
            self.stats.new += 1
            log.info("  new: %s (%s)", listing.title, listing.price_label)
            if self.notifier.notify(listing):
                self.stats.alerts += 1
