"""SQLite persistence: listings, price/favorites history and events."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    first_seen TEXT,
    last_seen TEXT,
    current_price TEXT,
    current_favorites INTEGER,
    status TEXT,
    sheet_row_idx INTEGER,
    title TEXT,
    brand TEXT,
    sold_date TEXT
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    ts TEXT,
    price TEXT,
    favorites INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    ts TEXT,
    event_type TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_price_history_listing ON price_history(listing_id, ts);
"""

# Columns added after the first schema version; created on startup if missing.
MIGRATIONS = {
    "listings": {"current_price_eur": "REAL", "market_price_eur": "REAL"},
    "price_history": {"price_eur": "REAL"},
}

UPDATABLE_COLUMNS = {
    "last_seen",
    "current_price",
    "current_price_eur",
    "market_price_eur",
    "current_favorites",
    "status",
    "sheet_row_idx",
    "title",
    "brand",
    "sold_date",
}

STATUS_AVAILABLE = "available"
STATUS_SOLD = "sold"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, db_path: str | Path = "data/history.db") -> None:
        db_path = Path(db_path)
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(SCHEMA)
            for table, columns in MIGRATIONS.items():
                existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
                for name, sql_type in columns.items():
                    if name not in existing:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

    # -- queries -----------------------------------------------------------
    def get_listing(self, url: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM listings WHERE url = ?", (url,)).fetchone()
        return dict(row) if row else None

    def get_listing_by_id(self, listing_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
        return dict(row) if row else None

    def price_history(self, listing_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM price_history WHERE listing_id = ? ORDER BY ts, id", (listing_id,))
        return [dict(r) for r in rows]

    # -- writes ------------------------------------------------------------
    def create_listing(self, listing: Listing, seen_at: str | None = None) -> int:
        seen_at = seen_at or _now()
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO listings (url, first_seen, last_seen, current_price, current_price_eur,
                                      market_price_eur, current_favorites, status, title, brand)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    listing.url,
                    seen_at,
                    seen_at,
                    listing.price_label,
                    listing.price,
                    listing.market_price,
                    listing.favorites,
                    STATUS_SOLD if listing.sold else STATUS_AVAILABLE,
                    listing.title,
                    listing.brand,
                ),
            )
            listing_id = int(cur.lastrowid)
            self._insert_snapshot(listing_id, listing, seen_at)
            self._insert_event(listing_id, "created", seen_at)
        return listing_id

    def update_listing(self, listing_id: int, **values: Any) -> None:
        unknown = set(values) - UPDATABLE_COLUMNS
        if unknown:
            raise ValueError(f"Cannot update columns: {', '.join(sorted(unknown))}")
        if not values:
            return
        assignments = ", ".join(f"{col} = ?" for col in values)
        with self.conn:
            self.conn.execute(f"UPDATE listings SET {assignments} WHERE id = ?", (*values.values(), listing_id))

    def record_observation(self, listing_id: int, listing: Listing, seen_at: str | None = None) -> bool:
        """Refresh a known listing. Adds a history snapshot only if price or favorites changed.

        Returns ``True`` when a new snapshot was stored.
        """
        seen_at = seen_at or _now()
        current = self.get_listing_by_id(listing_id)
        if current is None:
            raise KeyError(listing_id)
        changed = current["current_price_eur"] != listing.price or current["current_favorites"] != listing.favorites
        with self.conn:
            self.conn.execute(
                """
                UPDATE listings SET last_seen = ?, current_price = ?, current_price_eur = ?,
                    market_price_eur = ?, current_favorites = ?, title = ?, brand = ?
                WHERE id = ?
                """,
                (
                    seen_at,
                    listing.price_label,
                    listing.price,
                    listing.market_price,
                    listing.favorites,
                    listing.title,
                    listing.brand,
                    listing_id,
                ),
            )
            if changed:
                self._insert_snapshot(listing_id, listing, seen_at)
                if current["current_price_eur"] is not None and current["current_price_eur"] != listing.price:
                    self._insert_event(
                        listing_id,
                        "price_change",
                        seen_at,
                        f"{current['current_price_eur']} -> {listing.price}",
                    )
        return changed

    def mark_sold(self, listing_id: int, sold_at: str | None = None) -> None:
        sold_at = sold_at or _now()
        with self.conn:
            cur = self.conn.execute(
                "UPDATE listings SET status = ?, sold_date = ? WHERE id = ? AND status IS NOT ?",
                (STATUS_SOLD, sold_at, listing_id, STATUS_SOLD),
            )
            if cur.rowcount:
                self._insert_event(listing_id, "sold", sold_at)

    def _insert_snapshot(self, listing_id: int, listing: Listing, ts: str) -> None:
        self.conn.execute(
            "INSERT INTO price_history (listing_id, ts, price, price_eur, favorites) VALUES (?, ?, ?, ?, ?)",
            (listing_id, ts, listing.price_label, listing.price, listing.favorites),
        )

    def _insert_event(self, listing_id: int, event_type: str, ts: str, payload: str = "") -> None:
        self.conn.execute(
            "INSERT INTO events (listing_id, ts, event_type, payload) VALUES (?, ?, ?, ?)",
            (listing_id, ts, event_type, payload),
        )
