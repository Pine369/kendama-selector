from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import yaml

from seller_monitor.config import MonitorConfig
from seller_monitor.main import add_seller_interactive, check_config, show_status
from seller_monitor.models import FetchResult, ListingSnapshot, MonitoredSeller, NotificationResult
from seller_monitor.monitor import SellerMonitorService
from seller_monitor.repository import SellerMonitorRepository


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "seller_monitor" / "snapshots.json"
FIXTURES = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def make_seller(key="seller_a", platform="mercari", enabled=True):
    if platform == "mercari":
        seller_id = "123" if key == "seller_a" else key
        url = f"https://jp.mercari.com/user/profile/{seller_id}"
    else:
        url = "https://auctions.yahoo.co.jp/seller/test_seller"
        seller_id = "test_seller"
    return MonitoredSeller(
        seller_key=key,
        seller_id=seller_id,
        seller_identity_source="url_native_id",
        seller_name=key,
        platform=platform,
        seller_url=url,
        enabled=enabled,
    )


class FakeAdapter:
    def __init__(self, seller, sequence):
        self.seller = seller
        self.sequence = list(sequence)
        self.calls = 0

    def fetch_seller(self, seller):
        index = min(self.calls, len(self.sequence) - 1)
        self.calls += 1
        value = self.sequence[index]
        if isinstance(value, Exception):
            raise value
        snapshots = [
            ListingSnapshot(
                platform=seller.platform,
                seller_key=seller.seller_key,
                seller_name=seller.seller_name,
                seller_url=seller.seller_url,
                observed_at=f"2026-07-22T00:{self.calls:02d}:00+00:00",
                **raw,
            )
            for raw in value
        ]
        return FetchResult(snapshots=snapshots, complete=True)


class FakeNotifier:
    def __init__(self, statuses=None):
        self.statuses = list(statuses or ["accepted"])
        self.calls = []

    def send(self, payload):
        self.calls.append(payload)
        status = self.statuses[min(len(self.calls) - 1, len(self.statuses) - 1)]
        return NotificationResult(status=status, error=None if status == "accepted" else status)


class SellerMonitorTestCase(unittest.TestCase):
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

    def service(self, adapters, notifier=None):
        return SellerMonitorService(
            SellerMonitorRepository(self.config.database_path), adapters, notifier
        )

    def event_count(self):
        return SellerMonitorRepository(self.config.database_path).scalar(
            "SELECT COUNT(*) FROM notification_events"
        )

    def test_01_first_baseline_creates_zero_events(self):
        adapter = FakeAdapter(self.seller, [FIXTURES["fixed_baseline"]])
        summary = self.service({"mercari": adapter}).run(self.config, mode="bootstrap")
        self.assertEqual(0, summary.event_count)
        self.assertEqual(0, self.event_count())

    def test_02_identical_snapshot_after_baseline_creates_zero_events(self):
        adapter = FakeAdapter(self.seller, [FIXTURES["fixed_baseline"], FIXTURES["fixed_baseline"]])
        service = self.service({"mercari": adapter})
        service.run(self.config, mode="bootstrap")
        summary = service.run(self.config)
        self.assertEqual(0, summary.event_count)

    def test_03_new_item_creates_one_notification(self):
        adapter = FakeAdapter(self.seller, [FIXTURES["fixed_baseline"], FIXTURES["fixed_with_new"]])
        notifier = FakeNotifier()
        service = self.service({"mercari": adapter}, notifier)
        service.run(self.config, mode="bootstrap")
        summary = service.run(self.config)
        self.assertEqual(1, summary.event_count)
        self.assertEqual(1, len(notifier.calls))

    def test_04_same_new_item_is_not_repeated(self):
        adapter = FakeAdapter(
            self.seller,
            [FIXTURES["fixed_baseline"], FIXTURES["fixed_with_new"], FIXTURES["fixed_with_new"]],
        )
        notifier = FakeNotifier()
        service = self.service({"mercari": adapter}, notifier)
        service.run(self.config, mode="bootstrap")
        service.run(self.config)
        summary = service.run(self.config)
        self.assertEqual(0, summary.event_count)
        self.assertEqual(1, len(notifier.calls))

    def test_05_fixed_price_drop_creates_one_notification(self):
        adapter = FakeAdapter(self.seller, [FIXTURES["fixed_baseline"], FIXTURES["fixed_drop"]])
        notifier = FakeNotifier()
        service = self.service({"mercari": adapter}, notifier)
        service.run(self.config, mode="bootstrap")
        summary = service.run(self.config)
        self.assertEqual(1, summary.event_count)
        self.assertEqual("fixed_price_drop", notifier.calls[0]["event_type"])

    def test_06_same_drop_price_is_not_repeated(self):
        adapter = FakeAdapter(
            self.seller, [FIXTURES["fixed_baseline"], FIXTURES["fixed_drop"], FIXTURES["fixed_drop"]]
        )
        notifier = FakeNotifier()
        service = self.service({"mercari": adapter}, notifier)
        service.run(self.config, mode="bootstrap")
        service.run(self.config)
        summary = service.run(self.config)
        self.assertEqual(0, summary.event_count)
        self.assertEqual(1, len(notifier.calls))

    def test_07_price_increase_is_recorded_without_notification(self):
        adapter = FakeAdapter(self.seller, [FIXTURES["fixed_baseline"], FIXTURES["fixed_raise"]])
        notifier = FakeNotifier()
        service = self.service({"mercari": adapter}, notifier)
        service.run(self.config, mode="bootstrap")
        summary = service.run(self.config)
        repo = SellerMonitorRepository(self.config.database_path)
        self.assertEqual(0, summary.event_count)
        self.assertEqual(2, repo.scalar("SELECT COUNT(*) FROM price_history"))
        self.assertEqual(0, len(notifier.calls))

    def test_08_auction_bid_increase_is_not_notified(self):
        seller = make_seller("auction_seller", "yahoo_auctions")
        config = replace(self.config, sellers=[seller])
        adapter = FakeAdapter(seller, [FIXTURES["auction_baseline"], FIXTURES["auction_bid_up"]])
        notifier = FakeNotifier()
        service = self.service({"yahoo_auctions": adapter}, notifier)
        service.run(config, mode="bootstrap")
        summary = service.run(config)
        self.assertEqual(0, summary.event_count)
        self.assertEqual(0, len(notifier.calls))

    def test_09_new_auction_is_notified(self):
        seller = make_seller("auction_seller", "yahoo_auctions")
        config = replace(self.config, sellers=[seller])
        adapter = FakeAdapter(seller, [FIXTURES["auction_baseline"], FIXTURES["auction_with_new"]])
        notifier = FakeNotifier()
        service = self.service({"yahoo_auctions": adapter}, notifier)
        service.run(config, mode="bootstrap")
        summary = service.run(config)
        self.assertEqual(1, summary.event_count)
        self.assertEqual("new_listing", notifier.calls[0]["event_type"])

    def test_10_one_seller_failure_does_not_stop_other_sellers(self):
        other = make_seller("seller_b")
        config = replace(self.config, sellers=[self.seller, other])
        adapters = {
            "mercari": MultiSellerAdapter(
                {"seller_a": RuntimeError("fixture failure"), "seller_b": FIXTURES["fixed_baseline"]}
            )
        }
        summary = self.service(adapters).run(config)
        self.assertEqual("partial_failure", summary.status)
        self.assertEqual(1, summary.seller_succeeded)
        self.assertEqual(1, summary.seller_failed)

    def test_11_failed_notification_is_not_marked_successful(self):
        adapter = FakeAdapter(self.seller, [FIXTURES["fixed_baseline"], FIXTURES["fixed_with_new"]])
        notifier = FakeNotifier(["rejected"])
        service = self.service({"mercari": adapter}, notifier)
        service.run(self.config, mode="bootstrap")
        service.run(self.config)
        repo = SellerMonitorRepository(self.config.database_path)
        self.assertEqual("rejected", repo.scalar("SELECT status FROM notification_events"))
        self.assertEqual(0, repo.scalar("SELECT COUNT(*) FROM notification_events WHERE accepted_at IS NOT NULL"))

    def test_12_delivery_unknown_is_not_automatically_retried(self):
        adapter = FakeAdapter(
            self.seller,
            [FIXTURES["fixed_baseline"], FIXTURES["fixed_with_new"], FIXTURES["fixed_with_new"]],
        )
        notifier = FakeNotifier(["delivery_unknown", "accepted"])
        service = self.service({"mercari": adapter}, notifier)
        service.run(self.config, mode="bootstrap")
        service.run(self.config)
        service.run(self.config)
        self.assertEqual(1, len(notifier.calls))
        self.assertEqual(
            "delivery_unknown",
            SellerMonitorRepository(self.config.database_path).scalar("SELECT status FROM notification_events"),
        )

    def test_13_restart_does_not_reclassify_history_as_new(self):
        first = FakeAdapter(self.seller, [FIXTURES["fixed_baseline"]])
        self.service({"mercari": first}).run(self.config, mode="bootstrap")
        restarted = FakeAdapter(self.seller, [FIXTURES["fixed_baseline"]])
        summary = self.service({"mercari": restarted}).run(self.config)
        self.assertEqual(0, summary.event_count)

    def test_14_soft_deleted_seller_is_not_monitored(self):
        adapter = FakeAdapter(self.seller, [FIXTURES["fixed_baseline"]])
        service = self.service({"mercari": adapter})
        service.run(self.config, mode="bootstrap")
        SellerMonitorRepository(self.config.database_path).soft_delete_seller(self.seller.seller_key)
        service.run(self.config)
        self.assertEqual(1, adapter.calls)

    def test_15_reenabled_seller_keeps_existing_baseline(self):
        adapter = FakeAdapter(self.seller, [FIXTURES["fixed_baseline"], FIXTURES["fixed_baseline"]])
        service = self.service({"mercari": adapter})
        service.run(self.config, mode="bootstrap")
        repo = SellerMonitorRepository(self.config.database_path)
        repo.soft_delete_seller(self.seller.seller_key)
        repo.restore_seller(self.seller.seller_key)
        summary = service.run(self.config)
        self.assertEqual(0, summary.event_count)

    def test_16_check_config_performs_zero_network_calls(self):
        document = {
            "version": 1,
            "settings": {"database_path": "never-created.db"},
            "sellers": [{
                "seller_name": "test", "platform": "mercari",
                "seller_url": "https://jp.mercari.com/user/profile/123", "enabled": True,
            }],
        }
        path = self.root / "check.yaml"
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        with mock.patch("requests.sessions.Session.request") as request:
            self.assertEqual(0, check_config(str(path)))
            request.assert_not_called()
        self.assertFalse((self.root / "never-created.db").exists())

    def test_17_status_does_not_create_database(self):
        document = {"version": 1, "settings": {"database_path": "absent.db"}, "sellers": []}
        path = self.root / "status.yaml"
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        self.assertEqual(0, show_status(str(path)))
        self.assertFalse((self.root / "absent.db").exists())

    def test_18_add_seller_rejects_unknown_platform(self):
        config_path = self.root / "new.yaml"
        with self.assertRaisesRegex(ValueError, "未知|不支持"):
            add_seller_interactive("https://unknown.example/seller/123", str(config_path))
        self.assertFalse(config_path.exists())

    def test_19_two_identical_scans_have_zero_new_events(self):
        adapter = FakeAdapter(
            self.seller,
            [FIXTURES["fixed_baseline"], FIXTURES["fixed_with_new"], FIXTURES["fixed_with_new"]],
        )
        service = self.service({"mercari": adapter}, FakeNotifier())
        service.run(self.config, mode="bootstrap")
        second = service.run(self.config)
        third = service.run(self.config)
        self.assertEqual(1, second.event_count)
        self.assertEqual(0, third.event_count)

    def test_20_auction_start_and_buyout_changes_are_distinct_events(self):
        seller = make_seller("auction_seller", "yahoo_auctions")
        config = replace(self.config, sellers=[seller])
        adapter = FakeAdapter(
            seller, [FIXTURES["auction_baseline"], FIXTURES["auction_terms_change"]]
        )
        notifier = FakeNotifier()
        service = self.service({"yahoo_auctions": adapter}, notifier)
        service.run(config, mode="bootstrap")
        summary = service.run(config)
        self.assertEqual(2, summary.event_count)
        self.assertEqual({"start_price", "buyout_price"}, {call["term_type"] for call in notifier.calls})

    def test_21_seller_removed_from_yaml_is_not_monitored_but_history_remains(self):
        adapter = FakeAdapter(self.seller, [FIXTURES["fixed_baseline"]])
        service = self.service({"mercari": adapter})
        service.run(self.config, mode="bootstrap")
        summary = service.run(replace(self.config, sellers=[]))
        repo = SellerMonitorRepository(self.config.database_path)
        self.assertEqual(1, adapter.calls)
        self.assertEqual(0, summary.seller_total)
        self.assertEqual(1, repo.scalar("SELECT COUNT(*) FROM monitored_sellers"))

    def test_22_price_increase_can_be_explicitly_enabled(self):
        adapter = FakeAdapter(self.seller, [FIXTURES["fixed_baseline"], FIXTURES["fixed_raise"]])
        notifier = FakeNotifier()
        service = self.service({"mercari": adapter}, notifier)
        service.run(self.config, mode="bootstrap")
        summary = service.run(replace(self.config, notify_price_increase=True))
        self.assertEqual(1, summary.event_count)
        self.assertEqual("fixed_price_increase", notifier.calls[0]["event_type"])


class MultiSellerAdapter:
    def __init__(self, values):
        self.values = values

    def fetch_seller(self, seller):
        value = self.values[seller.seller_key]
        if isinstance(value, Exception):
            raise value
        snapshots = [
            ListingSnapshot(
                platform=seller.platform,
                seller_key=seller.seller_key,
                seller_name=seller.seller_name,
                seller_url=seller.seller_url,
                observed_at="2026-07-22T00:00:00+00:00",
                **raw,
            )
            for raw in value
        ]
        return FetchResult(snapshots=snapshots)


if __name__ == "__main__":
    unittest.main()
