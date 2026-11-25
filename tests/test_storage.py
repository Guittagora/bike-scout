from __future__ import annotations

import sqlite3

import pytest

from bike_scout.storage import Storage


@pytest.fixture
def storage(tmp_path):
    with Storage(tmp_path / "sub" / "history.db") as s:
        yield s


def test_create_listing_stores_snapshot_and_event(storage, listing_factory):
    listing_id = storage.create_listing(listing_factory())
    row = storage.get_listing(listing_factory().url)
    assert row["id"] == listing_id
    assert row["status"] == "available"
    assert row["current_price_eur"] == 1500.0
    assert row["brand"] == "Canyon"
    assert len(storage.price_history(listing_id)) == 1


def test_record_observation_only_snapshots_changes(storage, listing_factory):
    listing_id = storage.create_listing(listing_factory())

    assert storage.record_observation(listing_id, listing_factory()) is False
    assert storage.record_observation(listing_id, listing_factory(price=1400.0, price_label="€ 1.400")) is True
    assert storage.record_observation(listing_id, listing_factory(price=1400.0, favorites=9)) is True

    history = storage.price_history(listing_id)
    assert [h["price_eur"] for h in history] == [1500.0, 1400.0, 1400.0]
    events = [r[0] for r in storage.conn.execute("SELECT event_type FROM events ORDER BY id")]
    assert events == ["created", "price_change"]


def test_mark_sold_is_idempotent(storage, listing_factory):
    listing_id = storage.create_listing(listing_factory())
    storage.mark_sold(listing_id)
    storage.mark_sold(listing_id)
    row = storage.get_listing_by_id(listing_id)
    assert row["status"] == "sold" and row["sold_date"]
    sold_events = storage.conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'sold'").fetchone()[0]
    assert sold_events == 1


def test_update_listing_rejects_unknown_columns(storage, listing_factory):
    listing_id = storage.create_listing(listing_factory())
    storage.update_listing(listing_id, sheet_row_idx=7)
    assert storage.get_listing_by_id(listing_id)["sheet_row_idx"] == 7
    with pytest.raises(ValueError):
        storage.update_listing(listing_id, **{"url = 'x'; --": 1})


def test_migrates_legacy_schema(tmp_path, listing_factory):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE listings (id INTEGER PRIMARY KEY, url TEXT UNIQUE, first_seen TEXT, last_seen TEXT,
            current_price TEXT, current_favorites INTEGER, status TEXT, sheet_row_idx INTEGER,
            title TEXT, brand TEXT, sold_date TEXT);
        CREATE TABLE price_history (id INTEGER PRIMARY KEY, listing_id INTEGER, ts TEXT, price TEXT, favorites INTEGER);
        INSERT INTO listings (url, current_price, status) VALUES ('https://buycycle.com/en-it/product/bike-1',
            '€ 1.500', 'available');
        """
    )
    conn.commit()
    conn.close()

    with Storage(db) as storage:
        row = storage.get_listing("https://buycycle.com/en-it/product/bike-1")
        assert row["current_price_eur"] is None
        # first observation after migration fills the numeric column
        assert storage.record_observation(row["id"], listing_factory()) is True
        assert storage.get_listing_by_id(row["id"])["current_price_eur"] == 1500.0
