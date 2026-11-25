"""Command-line entry point: ``python -m bike_scout``."""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from .config import ConfigError, load_settings
from .notifier import EmailNotifier, should_notify
from .pipeline import Pipeline
from .scraper import BuycycleScraper
from .sheets import MemorySheet, SheetsClient
from .storage import Storage

log = logging.getLogger("bike_scout")


class DryRunNotifier(EmailNotifier):
    """Logs which alerts would be sent instead of sending them."""

    def notify(self, listing):  # type: ignore[override]
        if should_notify(listing, self.settings):
            log.info("  [dry-run] would send alert for %s", listing.title)
        return False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bike_scout",
        description="Track Buycycle listings in Google Sheets and get e-mail alerts for good deals.",
    )
    parser.add_argument("-c", "--config", default="config/settings.json", help="path to settings JSON")
    parser.add_argument("-n", "--limit", type=int, help="max listings to process (overrides config)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scrape for real but write nothing: in-memory DB and sheet, no e-mails",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv()

    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2

    scraper = BuycycleScraper(settings.cookie)
    if args.dry_run:
        log.info("Dry run: nothing will be written or sent")
        sheet, storage = MemorySheet(), Storage(":memory:")
        notifier: EmailNotifier = DryRunNotifier(settings.email)
    else:
        try:
            sheet = SheetsClient(settings.sheet_name, settings.credentials_path)
        except Exception as exc:  # gspread/google-auth raise a variety of types
            log.error("Could not open Google Sheet '%s': %s", settings.sheet_name, exc)
            return 1
        storage = Storage(settings.db_path)
        notifier = EmailNotifier(settings.email)

    with storage:
        stats = Pipeline(settings, scraper, storage, sheet, notifier).run(limit=args.limit)
    log.info("Done: %s", stats)
    return 3 if stats.blocked else 0


if __name__ == "__main__":
    sys.exit(main())
