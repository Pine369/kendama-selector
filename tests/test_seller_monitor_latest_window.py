from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from seller_monitor.config import MonitorConfig
from seller_monitor.models import FetchResult, ListingSnapshot, MonitoredSeller, NotificationResult
from seller_monitor.monitor import SellerMonitorService
from seller_monitor.repository import SellerMonitorRepository


def make_seller() -> MonitoredSeller:
    return MonitoredSeller(
        seller_key="mercari_window_seller",
        seller_id="example_seller_id",
        seller_identity_source="url_native_id",
        seller_name="测试卖家",
        platform="mercari",
        seller_url="https://jp.mercari.com/user/profile/example_seller_id",
    )


def make_snapshot(
    seller: MonitoredSeller,
    item_id: str,
    price: int = 8_000,
    *,
    title: str | None = None,
    image_url: str | None = None,
) -> ListingSnapshot:
    return ListingSnapshot(
        platform=seller.platform,
        seller_key=seller.seller_key,
        seller_name=seller.seller_name,
        seller_url=seller.seller_url,
        item_id=item_id,
        item_url=f"https://jp.mercari.com/item/{item_id}",
        title=title or f"测试商品 {item_id}",
        image_url=image_url or f"https://example.com/images/{item_id}.jpg",
        listing_type="unknown",
        current_price=price,
        status="on_sale",
        observed_at="2026-07-25T00:00:00+00:00",
    )


def latest_result(
    seller: MonitoredSeller,
    item_ids: list[str],
    *,
    prices: dict[str, int] | None = None,
    has_next: bool = True,
    valid: bool = True,
) -> FetchResult:
    prices = prices or {}
    snapshots = [make_snapshot(seller, item_id, prices.get(item_id, 8_000)) for item_id in item_ids]
    return FetchResult(
        snapshots=snapshots,
        complete=False,
        coverage="latest_window",
        window_complete=valid,
        has_next=has_next if valid else None,
        window_limit=30,
        list_page_request_count=1,
        network_request_count=1,
    )


class SequenceAdapter:
    def __init__(self, results: list[FetchResult]):
        self.results = results
        self.calls = 0

    def fetch_seller(self, seller: MonitoredSeller) -> FetchResult:
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


class AcceptingNotifier:
    def __init__(self):
        self.calls: list[dict] = []

    def send(self, payload: dict) -> NotificationResult:
        self.calls.append(payload)
        return NotificationResult(status="accepted")


class SellerLatestWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.seller = make_seller()
        self.config = MonitorConfig(
            config_path=self.root / "seller_monitor.yaml",
            database_path=self.root / "seller_monitor.db",
            state_path=self.root / "seller_monitor_state.json",
            log_path=self.root / "seller_monitor.log",
            notify_price_increase=False,
            sellers=[self.seller],
        )

    def tearDown(self):
        self.temp.cleanup()

    def repository(self) -> SellerMonitorRepository:
        return SellerMonitorRepository(self.config.database_path)

    def service(self, adapter: SequenceAdapter, notifier=None) -> SellerMonitorService:
        return SellerMonitorService(self.repository(), {"mercari": adapter}, notifier)

    def test_first_30_item_window_is_silent_and_persists_order(self):
        item_ids = [f"m{i:03d}" for i in range(30)]
        adapter = SequenceAdapter([latest_result(self.seller, item_ids, has_next=True)])
        summary = self.service(adapter).run(self.config, mode="bootstrap")

        repository = self.repository()
        window = repository.latest_window(self.seller.seller_key)
        self.assertEqual(0, summary.event_count)
        self.assertEqual(30, repository.scalar("SELECT COUNT(*) FROM items"))
        self.assertEqual(0, repository.scalar("SELECT COUNT(*) FROM notification_events"))
        self.assertIsNotNone(
            repository.scalar(
                "SELECT baseline_completed_at FROM monitored_sellers WHERE seller_key=?",
                (self.seller.seller_key,),
            )
        )
        self.assertEqual(tuple(f"native:{item_id}" for item_id in item_ids), window.ordered_identity_keys)
        self.assertEqual(30, window.window_limit)
        self.assertTrue(window.has_next)
        self.assertEqual("latest_window", window.coverage)

    def test_identical_window_is_silent(self):
        ids = ["mA", "mB", "mC"]
        adapter = SequenceAdapter([latest_result(self.seller, ids), latest_result(self.seller, ids)])
        service = self.service(adapter)
        service.run(self.config, mode="bootstrap")
        self.assertEqual(0, service.run(self.config).event_count)

    def test_one_item_before_first_overlap_is_new(self):
        adapter = SequenceAdapter(
            [latest_result(self.seller, ["mA", "mB"]), latest_result(self.seller, ["mX", "mA"])]
        )
        notifier = AcceptingNotifier()
        service = self.service(adapter, notifier)
        service.run(self.config, mode="bootstrap")
        summary = service.run(self.config)
        self.assertEqual(1, summary.event_count)
        self.assertEqual(["mX"], [payload["item_id"] for payload in notifier.calls])

    def test_two_items_before_first_overlap_are_new(self):
        adapter = SequenceAdapter(
            [
                latest_result(self.seller, ["mA", "mB", "mC"]),
                latest_result(self.seller, ["mX", "mY", "mA", "mB"]),
            ]
        )
        notifier = AcceptingNotifier()
        service = self.service(adapter, notifier)
        service.run(self.config, mode="bootstrap")
        self.assertEqual(2, service.run(self.config).event_count)
        self.assertEqual(["mX", "mY"], [payload["item_id"] for payload in notifier.calls])

    def test_unknown_items_after_first_overlap_are_silent_backfill(self):
        adapter = SequenceAdapter(
            [
                latest_result(self.seller, ["mA", "mB", "mC"]),
                latest_result(self.seller, ["mA", "mB", "mC", "mF", "mG"]),
            ]
        )
        service = self.service(adapter)
        service.run(self.config, mode="bootstrap")
        self.assertEqual(0, service.run(self.config).event_count)
        self.assertEqual(5, self.repository().scalar("SELECT COUNT(*) FROM items"))

    def test_old_31st_item_entering_window_is_not_new(self):
        first = [f"m{i:03d}" for i in range(1, 31)]
        second = first[:29] + ["m031"]
        adapter = SequenceAdapter([latest_result(self.seller, first), latest_result(self.seller, second)])
        service = self.service(adapter)
        service.run(self.config, mode="bootstrap")
        self.assertEqual(0, service.run(self.config).event_count)
        self.assertEqual(31, self.repository().scalar("SELECT COUNT(*) FROM items"))

    def test_no_overlap_is_silent_rebases_and_records_status(self):
        adapter = SequenceAdapter(
            [
                latest_result(self.seller, ["mA", "mB"]),
                latest_result(self.seller, ["mX", "mY"]),
                latest_result(self.seller, ["mZ", "mX", "mY"]),
            ]
        )
        notifier = AcceptingNotifier()
        service = self.service(adapter, notifier)
        service.run(self.config, mode="bootstrap")
        no_overlap = service.run(self.config)
        self.assertEqual(0, no_overlap.event_count)
        self.assertEqual(
            "no_overlap",
            self.repository().scalar("SELECT status FROM seller_checks ORDER BY check_id DESC LIMIT 1"),
        )
        self.assertEqual(
            ("native:mX", "native:mY"),
            self.repository().latest_window(self.seller.seller_key).ordered_identity_keys,
        )
        self.assertEqual(1, service.run(self.config).event_count)
        self.assertEqual(["mZ"], [payload["item_id"] for payload in notifier.calls])

    def test_restart_uses_persisted_previous_window(self):
        first = SequenceAdapter([latest_result(self.seller, ["mA", "mB"])])
        self.service(first).run(self.config, mode="bootstrap")

        notifier = AcceptingNotifier()
        restarted = SequenceAdapter([latest_result(self.seller, ["mX", "mA", "mB"])])
        summary = self.service(restarted, notifier).run(self.config)
        self.assertEqual(1, summary.event_count)
        self.assertEqual("mX", notifier.calls[0]["item_id"])

    def test_same_item_can_never_create_a_second_new_listing_event(self):
        adapter = SequenceAdapter(
            [
                latest_result(self.seller, ["mA", "mB"]),
                latest_result(self.seller, ["mX", "mA"], prices={"mX": 8_000}),
                latest_result(self.seller, ["mA", "mB"]),
                latest_result(self.seller, ["mX", "mA"], prices={"mX": 7_000}),
            ]
        )
        notifier = AcceptingNotifier()
        service = self.service(adapter, notifier)
        service.run(self.config, mode="bootstrap")
        self.assertEqual(1, service.run(self.config).event_count)
        self.assertEqual(0, service.run(self.config).event_count)
        self.assertEqual(0, service.run(self.config).event_count)
        self.assertEqual(1, self.repository().scalar("SELECT COUNT(*) FROM notification_events"))

    def test_baseline_item_returning_at_window_head_is_not_new(self):
        adapter = SequenceAdapter(
            [
                latest_result(self.seller, ["mA", "mB"], prices={"mA": 8_000}),
                latest_result(self.seller, ["mB", "mC"]),
                latest_result(self.seller, ["mA", "mB"], prices={"mA": 7_000}),
            ]
        )
        notifier = AcceptingNotifier()
        service = self.service(adapter, notifier)
        service.run(self.config, mode="bootstrap")
        self.assertEqual(0, service.run(self.config).event_count)

        returned = service.run(self.config)
        repository = self.repository()
        self.assertEqual(0, returned.event_count)
        self.assertEqual([], notifier.calls)
        self.assertEqual(
            2,
            repository.scalar(
                "SELECT COUNT(*) FROM price_history ph "
                "JOIN items i ON i.item_row_id=ph.item_row_id WHERE i.item_id='mA'"
            ),
        )
        self.assertEqual(
            7_000,
            repository.scalar("SELECT current_price FROM items WHERE item_id='mA'"),
        )
        self.assertEqual(0, repository.scalar("SELECT COUNT(*) FROM notification_events"))

    def test_relisted_item_with_new_id_is_new_before_overlap(self):
        shared_title = "相同标题测试商品"
        shared_image = "https://example.com/images/shared.jpg"
        old_item = make_snapshot(
            self.seller,
            "mOLD",
            title=shared_title,
            image_url=shared_image,
        )
        overlap = make_snapshot(self.seller, "mB")
        new_item = make_snapshot(
            self.seller,
            "mNEW",
            title=shared_title,
            image_url=shared_image,
        )
        adapter = SequenceAdapter(
            [
                FetchResult(
                    snapshots=[old_item, overlap],
                    complete=False,
                    coverage="latest_window",
                    window_complete=True,
                    has_next=True,
                    window_limit=30,
                ),
                latest_result(self.seller, ["mB"]),
                FetchResult(
                    snapshots=[new_item, overlap],
                    complete=False,
                    coverage="latest_window",
                    window_complete=True,
                    has_next=True,
                    window_limit=30,
                ),
            ]
        )
        notifier = AcceptingNotifier()
        service = self.service(adapter, notifier)
        service.run(self.config, mode="bootstrap")
        service.run(self.config)

        relisted = service.run(self.config)
        self.assertEqual(1, relisted.event_count)
        self.assertEqual(["mNEW"], [payload["item_id"] for payload in notifier.calls])

    def test_has_next_true_window_remains_usable(self):
        adapter = SequenceAdapter(
            [
                latest_result(self.seller, ["mA", "mB"], has_next=True),
                latest_result(self.seller, ["mX", "mA"], has_next=True),
            ]
        )
        service = self.service(adapter)
        first = service.run(self.config, mode="bootstrap")
        second = service.run(self.config)
        self.assertEqual((1, 0), (first.seller_succeeded, first.seller_failed))
        self.assertEqual(1, second.event_count)
        self.assertTrue(self.repository().latest_window(self.seller.seller_key).has_next)

    def test_latest_window_never_calls_mark_missing(self):
        adapter = SequenceAdapter(
            [latest_result(self.seller, ["mA", "mB"]), latest_result(self.seller, ["mA"])]
        )
        repository = self.repository()
        service = SellerMonitorService(repository, {"mercari": adapter})
        with mock.patch.object(repository, "mark_missing", wraps=repository.mark_missing) as mark_missing:
            service.run(self.config, mode="bootstrap")
            service.run(self.config)
        mark_missing.assert_not_called()

    def test_window_outside_items_stay_active(self):
        adapter = SequenceAdapter(
            [latest_result(self.seller, ["mA", "mB", "mC"]), latest_result(self.seller, ["mA"])]
        )
        service = self.service(adapter)
        service.run(self.config, mode="bootstrap")
        service.run(self.config)
        self.assertEqual(3, self.repository().scalar("SELECT COUNT(*) FROM items WHERE status='on_sale'"))
        self.assertEqual(0, self.repository().scalar("SELECT COUNT(*) FROM items WHERE status='missing'"))

    def test_unknown_price_change_inside_window_is_history_only(self):
        adapter = SequenceAdapter(
            [
                latest_result(self.seller, ["mA"], prices={"mA": 8_000}),
                latest_result(self.seller, ["mA"], prices={"mA": 7_000}),
            ]
        )
        service = self.service(adapter, AcceptingNotifier())
        service.run(self.config, mode="bootstrap")
        summary = service.run(self.config)
        self.assertEqual(0, summary.event_count)
        self.assertEqual(2, self.repository().scalar("SELECT COUNT(*) FROM price_history"))

    def test_item_outside_window_has_no_claimed_price_refresh(self):
        adapter = SequenceAdapter(
            [
                latest_result(self.seller, ["mA", "mB"], prices={"mB": 5_000}),
                latest_result(self.seller, ["mA"]),
            ]
        )
        service = self.service(adapter)
        service.run(self.config, mode="bootstrap")
        service.run(self.config)
        repository = self.repository()
        self.assertEqual(
            5_000,
            repository.scalar("SELECT current_price FROM items WHERE item_id='mB'"),
        )
        self.assertEqual(
            1,
            repository.scalar(
                "SELECT COUNT(*) FROM price_history ph JOIN items i ON i.item_row_id=ph.item_row_id "
                "WHERE i.item_id='mB'"
            ),
        )

    def test_invalid_transport_does_not_replace_previous_window(self):
        adapter = SequenceAdapter(
            [
                latest_result(self.seller, ["mA", "mB"]),
                latest_result(self.seller, [], valid=False),
            ]
        )
        service = self.service(adapter)
        service.run(self.config, mode="bootstrap")
        before = self.repository().latest_window(self.seller.seller_key)
        failed = service.run(self.config)
        after = self.repository().latest_window(self.seller.seller_key)
        self.assertEqual("partial_failure", failed.status)
        self.assertEqual((0, 1), (failed.seller_succeeded, failed.seller_failed))
        self.assertEqual(before.scan_run_id, after.scan_run_id)
        self.assertEqual(1, self.repository().scalar("SELECT COUNT(*) FROM seller_latest_windows"))


if __name__ == "__main__":
    unittest.main()
