"""Repository boundary and SQLite implementation for seller-monitor state."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Iterable

from seller_monitor.models import ItemChange, ListingSnapshot, MonitoredSeller, NotificationResult
from seller_monitor.storage import connect_database, initialize_database
from seller_monitor.utils import item_identity, utc_now


class SellerMonitorRepository:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        initialize_database(self.database_path)

    def sync_sellers(self, sellers: Iterable[MonitoredSeller]) -> None:
        now = utc_now()
        with connect_database(self.database_path) as db:
            for seller in sellers:
                db.execute(
                    """
                    INSERT INTO monitored_sellers (
                        seller_key, seller_id, seller_identity_source, seller_name,
                        platform, seller_url, enabled, deleted_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(seller_key) DO UPDATE SET
                        seller_id=excluded.seller_id,
                        seller_identity_source=excluded.seller_identity_source,
                        seller_name=excluded.seller_name,
                        platform=excluded.platform,
                        seller_url=excluded.seller_url,
                        enabled=excluded.enabled,
                        deleted_at=COALESCE(excluded.deleted_at, monitored_sellers.deleted_at),
                        updated_at=excluded.updated_at
                    """,
                    (
                        seller.seller_key,
                        seller.seller_id,
                        seller.seller_identity_source,
                        seller.seller_name,
                        seller.platform,
                        seller.seller_url,
                        int(seller.enabled),
                        seller.deleted_at,
                        now,
                        now,
                    ),
                )

    @staticmethod
    def _seller(row) -> MonitoredSeller:
        return MonitoredSeller(
            seller_key=row["seller_key"],
            seller_id=row["seller_id"],
            seller_identity_source=row["seller_identity_source"],
            seller_name=row["seller_name"],
            platform=row["platform"],
            seller_url=row["seller_url"],
            enabled=bool(row["enabled"]),
            deleted_at=row["deleted_at"],
            baseline_completed_at=row["baseline_completed_at"],
            last_success_at=row["last_success_at"],
            last_error=row["last_error"],
        )

    def active_sellers(self, configured_keys: Iterable[str] | None = None) -> list[MonitoredSeller]:
        with connect_database(self.database_path) as db:
            if configured_keys is None:
                rows = db.execute(
                    "SELECT * FROM monitored_sellers WHERE enabled=1 AND deleted_at IS NULL ORDER BY seller_key"
                ).fetchall()
            else:
                keys = sorted(set(configured_keys))
                if not keys:
                    return []
                placeholders = ",".join("?" for _ in keys)
                rows = db.execute(
                    f"SELECT * FROM monitored_sellers WHERE enabled=1 AND deleted_at IS NULL "
                    f"AND seller_key IN ({placeholders}) ORDER BY seller_key",
                    keys,
                ).fetchall()
        return [self._seller(row) for row in rows]

    def soft_delete_seller(self, seller_key: str) -> None:
        now = utc_now()
        with connect_database(self.database_path) as db:
            db.execute(
                "UPDATE monitored_sellers SET deleted_at=?, enabled=0, updated_at=? WHERE seller_key=?",
                (now, now, seller_key),
            )

    def restore_seller(self, seller_key: str) -> None:
        now = utc_now()
        with connect_database(self.database_path) as db:
            db.execute(
                "UPDATE monitored_sellers SET deleted_at=NULL, enabled=1, updated_at=? WHERE seller_key=?",
                (now, seller_key),
            )

    def start_run(self, mode: str, seller_total: int) -> str:
        run_id = f"run_{uuid.uuid4().hex}"
        with connect_database(self.database_path) as db:
            db.execute(
                "INSERT INTO scan_runs(run_id, mode, started_at, status, seller_total) VALUES (?, ?, ?, 'running', ?)",
                (run_id, mode, utc_now(), seller_total),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        status: str,
        succeeded: int,
        failed: int,
        event_count: int,
        accepted_count: int,
        error: str | None = None,
    ) -> None:
        with connect_database(self.database_path) as db:
            db.execute(
                """UPDATE scan_runs SET finished_at=?, status=?, seller_succeeded=?, seller_failed=?,
                   event_count=?, accepted_count=?, error=? WHERE run_id=?""",
                (utc_now(), status, succeeded, failed, event_count, accepted_count, error, run_id),
            )

    def start_check(self, run_id: str, seller_key: str) -> int:
        with connect_database(self.database_path) as db:
            cursor = db.execute(
                "INSERT INTO seller_checks(run_id, seller_key, started_at, status) VALUES (?, ?, ?, 'running')",
                (run_id, seller_key, utc_now()),
            )
            return int(cursor.lastrowid)

    def finish_check(self, check_id: int, status: str, result=None, event_count: int = 0, error=None) -> None:
        result = result or object()
        with connect_database(self.database_path) as db:
            db.execute(
                """UPDATE seller_checks SET finished_at=?, status=?, item_count=?, event_count=?,
                   list_page_request_count=?, detail_page_request_count=?, network_request_count=?, error=?
                   WHERE check_id=?""",
                (
                    utc_now(),
                    status,
                    len(getattr(result, "snapshots", [])),
                    event_count,
                    getattr(result, "list_page_request_count", 0),
                    getattr(result, "detail_page_request_count", 0),
                    getattr(result, "network_request_count", 0),
                    error,
                    check_id,
                ),
            )

    def mark_seller_success(self, seller_key: str, *, complete_baseline: bool) -> None:
        now = utc_now()
        with connect_database(self.database_path) as db:
            if complete_baseline:
                db.execute(
                    """UPDATE monitored_sellers SET last_success_at=?, last_error=NULL,
                       baseline_completed_at=COALESCE(baseline_completed_at, ?), updated_at=? WHERE seller_key=?""",
                    (now, now, now, seller_key),
                )
            else:
                db.execute(
                    "UPDATE monitored_sellers SET last_success_at=?, last_error=NULL, updated_at=? WHERE seller_key=?",
                    (now, now, seller_key),
                )

    def mark_seller_error(self, seller_key: str, error: str) -> None:
        with connect_database(self.database_path) as db:
            db.execute(
                "UPDATE monitored_sellers SET last_error=?, updated_at=? WHERE seller_key=?",
                (error, utc_now(), seller_key),
            )

    def upsert_snapshot(self, run_id: str, snapshot: ListingSnapshot) -> ItemChange:
        identity_key, identity_source = item_identity(snapshot)
        observed_at = snapshot.observed_at or utc_now()
        raw_json = json.dumps(snapshot.raw, ensure_ascii=False, sort_keys=True)
        with connect_database(self.database_path) as db:
            previous = db.execute(
                "SELECT * FROM items WHERE platform=? AND identity_key=?",
                (snapshot.platform, identity_key),
            ).fetchone()
            if previous is None:
                cursor = db.execute(
                    """INSERT INTO items(
                       platform, seller_key, identity_key, identity_source, item_id, item_url, title,
                       image_url, listing_type, current_price, previous_price, auction_start_price,
                       auction_buyout_price, first_seen_at, last_seen_at, status, raw_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot.platform, snapshot.seller_key, identity_key, identity_source,
                        snapshot.item_id, snapshot.item_url, snapshot.title, snapshot.image_url,
                        snapshot.listing_type, snapshot.current_price, snapshot.auction_start_price,
                        snapshot.auction_buyout_price, observed_at, observed_at, snapshot.status, raw_json,
                    ),
                )
                item_row_id = int(cursor.lastrowid)
                change = ItemChange(item_row_id, identity_key, True, None, None, None, None)
            else:
                item_row_id = int(previous["item_row_id"])
                change = ItemChange(
                    item_row_id,
                    identity_key,
                    False,
                    previous["current_price"],
                    previous["listing_type"],
                    previous["auction_start_price"],
                    previous["auction_buyout_price"],
                )
                db.execute(
                    """UPDATE items SET seller_key=?, item_id=?, item_url=?, title=?, image_url=?,
                       listing_type=?, previous_price=?, current_price=?, auction_start_price=?,
                       auction_buyout_price=?, last_seen_at=?, status=?, raw_json=? WHERE item_row_id=?""",
                    (
                        snapshot.seller_key, snapshot.item_id, snapshot.item_url, snapshot.title,
                        snapshot.image_url, snapshot.listing_type, previous["current_price"],
                        snapshot.current_price, snapshot.auction_start_price,
                        snapshot.auction_buyout_price, observed_at, snapshot.status, raw_json, item_row_id,
                    ),
                )
            changed = previous is None or any(
                previous[column] != value
                for column, value in (
                    ("current_price", snapshot.current_price),
                    ("auction_start_price", snapshot.auction_start_price),
                    ("auction_buyout_price", snapshot.auction_buyout_price),
                    ("listing_type", snapshot.listing_type),
                )
            )
            if changed:
                db.execute(
                    """INSERT OR IGNORE INTO price_history(
                       item_row_id, run_id, observed_at, current_price, previous_price,
                       auction_start_price, auction_buyout_price, listing_type
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item_row_id, run_id, observed_at, snapshot.current_price,
                        None if previous is None else previous["current_price"],
                        snapshot.auction_start_price, snapshot.auction_buyout_price,
                        snapshot.listing_type,
                    ),
                )
        return change

    def mark_missing(self, seller_key: str, seen_identity_keys: set[str], observed_at: str) -> None:
        with connect_database(self.database_path) as db:
            rows = db.execute(
                "SELECT item_row_id, identity_key FROM items WHERE seller_key=? AND status='active'",
                (seller_key,),
            ).fetchall()
            missing_ids = [row["item_row_id"] for row in rows if row["identity_key"] not in seen_identity_keys]
            db.executemany(
                "UPDATE items SET status='missing' WHERE item_row_id=?",
                [(item_id,) for item_id in missing_ids],
            )

    def create_event(
        self,
        *,
        event_key: str,
        run_id: str,
        item_row_id: int,
        event_type: str,
        term_type: str | None,
        old_price: int | None,
        new_price: int | None,
        payload: dict,
    ) -> bool:
        with connect_database(self.database_path) as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO notification_events(
                   event_key, run_id, item_row_id, event_type, term_type, old_price,
                   new_price, payload_json, status, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    event_key, run_id, item_row_id, event_type, term_type, old_price,
                    new_price, json.dumps(payload, ensure_ascii=False), utc_now(),
                ),
            )
            return cursor.rowcount == 1

    def pending_events(self) -> list[dict]:
        with connect_database(self.database_path) as db:
            rows = db.execute(
                "SELECT * FROM notification_events WHERE status IN ('pending', 'retryable_failure') ORDER BY created_at"
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def claim_event(self, event_key: str) -> int | None:
        now = utc_now()
        with connect_database(self.database_path) as db:
            cursor = db.execute(
                """UPDATE notification_events SET status='sending', claimed_at=?, last_error=NULL
                   WHERE event_key=? AND status IN ('pending', 'retryable_failure')""",
                (now, event_key),
            )
            if cursor.rowcount != 1:
                return None
            number = db.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM notification_attempts WHERE event_key=?",
                (event_key,),
            ).fetchone()[0]
            db.execute(
                """INSERT INTO notification_attempts(event_key, attempt_number, started_at, result_status)
                   VALUES (?, ?, ?, 'sending')""",
                (event_key, number, now),
            )
            return int(number)

    def finish_attempt(self, event_key: str, attempt_number: int, result: NotificationResult) -> None:
        now = utc_now()
        with connect_database(self.database_path) as db:
            db.execute(
                """UPDATE notification_attempts SET finished_at=?, result_status=?, http_status=?,
                   provider_code=?, provider_message=?, error=?
                   WHERE event_key=? AND attempt_number=?""",
                (
                    now, result.status, result.http_status, result.provider_code,
                    result.provider_message, result.error, event_key, attempt_number,
                ),
            )
            accepted_at = now if result.status == "accepted" else None
            db.execute(
                """UPDATE notification_events SET status=?, accepted_at=?, provider_message_id=?, last_error=?
                   WHERE event_key=?""",
                (result.status, accepted_at, result.provider_message_id, result.error, event_key),
            )
            if result.status == "accepted":
                db.execute(
                    """UPDATE items SET last_notified_price=(
                       SELECT new_price FROM notification_events WHERE event_key=?
                       ) WHERE item_row_id=(SELECT item_row_id FROM notification_events WHERE event_key=?)""",
                    (event_key, event_key),
                )

    def scalar(self, sql: str, parameters=()):
        with connect_database(self.database_path) as db:
            return db.execute(sql, parameters).fetchone()[0]

    def latest_run(self) -> dict | None:
        with connect_database(self.database_path, read_only=True) as db:
            row = db.execute("SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None
