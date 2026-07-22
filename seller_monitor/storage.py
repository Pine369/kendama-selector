"""SQLite schema for the independent seller-monitor database."""

from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS monitored_sellers (
    seller_key TEXT PRIMARY KEY,
    seller_id TEXT,
    seller_identity_source TEXT NOT NULL,
    seller_name TEXT NOT NULL,
    platform TEXT NOT NULL,
    seller_url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    deleted_at TEXT,
    baseline_completed_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_monitored_sellers_native
    ON monitored_sellers(platform, seller_id) WHERE seller_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_monitored_sellers_url
    ON monitored_sellers(platform, seller_url);

CREATE TABLE IF NOT EXISTS scan_runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    seller_total INTEGER NOT NULL DEFAULT 0,
    seller_succeeded INTEGER NOT NULL DEFAULT 0,
    seller_failed INTEGER NOT NULL DEFAULT 0,
    event_count INTEGER NOT NULL DEFAULT 0,
    accepted_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS seller_checks (
    check_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES scan_runs(run_id),
    seller_key TEXT NOT NULL REFERENCES monitored_sellers(seller_key),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    event_count INTEGER NOT NULL DEFAULT 0,
    list_page_request_count INTEGER NOT NULL DEFAULT 0,
    detail_page_request_count INTEGER NOT NULL DEFAULT 0,
    network_request_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    UNIQUE(run_id, seller_key)
);

CREATE TABLE IF NOT EXISTS items (
    item_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    seller_key TEXT NOT NULL REFERENCES monitored_sellers(seller_key),
    identity_key TEXT NOT NULL,
    identity_source TEXT NOT NULL,
    item_id TEXT,
    item_url TEXT NOT NULL,
    title TEXT NOT NULL,
    image_url TEXT NOT NULL,
    listing_type TEXT NOT NULL CHECK (listing_type IN ('fixed', 'auction')),
    current_price INTEGER,
    previous_price INTEGER,
    auction_start_price INTEGER,
    auction_buyout_price INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_notified_price INTEGER,
    status TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(platform, identity_key)
);
CREATE INDEX IF NOT EXISTS ix_items_seller ON items(seller_key, last_seen_at);

CREATE TABLE IF NOT EXISTS price_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_row_id INTEGER NOT NULL REFERENCES items(item_row_id),
    run_id TEXT NOT NULL REFERENCES scan_runs(run_id),
    observed_at TEXT NOT NULL,
    current_price INTEGER,
    previous_price INTEGER,
    auction_start_price INTEGER,
    auction_buyout_price INTEGER,
    listing_type TEXT NOT NULL,
    UNIQUE(item_row_id, run_id)
);

CREATE TABLE IF NOT EXISTS notification_events (
    event_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scan_runs(run_id),
    item_row_id INTEGER NOT NULL REFERENCES items(item_row_id),
    event_type TEXT NOT NULL,
    term_type TEXT,
    old_price INTEGER,
    new_price INTEGER,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    accepted_at TEXT,
    provider_message_id TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS ix_notification_events_status
    ON notification_events(status, created_at);

CREATE TABLE IF NOT EXISTS notification_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL REFERENCES notification_events(event_key),
    attempt_number INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    result_status TEXT NOT NULL,
    http_status INTEGER,
    provider_code TEXT,
    provider_message TEXT,
    error TEXT,
    UNIQUE(event_key, attempt_number)
);
"""


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback like sqlite3.Connection, then always release the handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect_database(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    database_path = Path(path)
    if read_only:
        connection = sqlite3.connect(
            f"file:{database_path.resolve()}?mode=ro", uri=True, factory=ClosingConnection
        )
    else:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if not read_only:
        connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize_database(path: str | Path) -> None:
    with connect_database(path) as connection:
        connection.executescript(SCHEMA)
        # A process that died after handing a message to PushPlus cannot know
        # whether WeChat accepted it. Never auto-resend such an event.
        connection.execute(
            "UPDATE notification_events SET status = 'delivery_unknown', "
            "last_error = COALESCE(last_error, 'process stopped while notification was in flight') "
            "WHERE status = 'sending'"
        )
