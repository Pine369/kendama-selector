from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
import yaml

from seller_monitor.main import add_seller_interactive
from seller_monitor.config import MonitorConfig
from seller_monitor.models import FetchResult, ListingSnapshot, MonitoredSeller, NotificationResult
from seller_monitor.monitor import SellerMonitorService
from seller_monitor.notifier import PushPlusNotifier, render_notification_html, write_preview
from seller_monitor.platforms import default_adapters
from seller_monitor.repository import SellerMonitorRepository


PAYLOAD = {
    "event_type": "fixed_price_drop",
    "platform": "mercari",
    "seller_name": "测试卖家",
    "listing_type": "fixed",
    "title": "Su Lab 测试剑玉",
    "image_url": "https://img.example/item.jpg",
    "item_url": "https://jp.mercari.com/item/m123",
    "old_price": 5000,
    "new_price": 4500,
    "observed_at": "2026-07-22T14:30:00+08:00",
}


class FakeResponse:
    status_code = 200

    def json(self):
        return {"code": 200, "msg": "请求成功", "data": "provider-123"}


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


class NotifierOfflineTests(unittest.TestCase):
    def test_html_contains_embedded_image_and_drop_details(self):
        rendered = render_notification_html(PAYLOAD)
        self.assertIn('<img src="https://img.example/item.jpg"', rendered)
        self.assertIn("¥5,000 → ¥4,500", rendered)
        self.assertIn("¥500（10.00%）", rendered)

    def test_pushplus_200_is_accepted_not_delivered(self):
        session = FakeSession(FakeResponse())
        result = PushPlusNotifier("fake-token", session=session).send(PAYLOAD)
        self.assertEqual("accepted", result.status)
        request_body = session.calls[0][1]["json"]
        self.assertEqual("html", request_body["template"])
        self.assertEqual("wechat", request_body["channel"])

    def test_read_timeout_is_delivery_unknown(self):
        session = FakeSession(error=requests.ReadTimeout("offline timeout"))
        result = PushPlusNotifier("fake-token", session=session).send(PAYLOAD)
        self.assertEqual("delivery_unknown", result.status)

    def test_preview_writes_html_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview.html"
            with mock.patch("requests.sessions.Session.request") as request:
                write_preview(output, PAYLOAD)
                request.assert_not_called()
            self.assertIn("Su Lab 测试剑玉", output.read_text(encoding="utf-8"))

    def test_add_known_seller_writes_safe_yaml_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "seller_monitor.yaml"
            answers = iter(["关注卖家", "y"])
            with mock.patch("requests.sessions.Session.request") as request:
                result = add_seller_interactive(
                    "分享： https://jp.mercari.com/user/profile/abc123?utm_source=share",
                    str(config),
                    input_func=lambda _: next(answers),
                )
                request.assert_not_called()
            self.assertEqual(0, result)
            document = yaml.safe_load(config.read_text(encoding="utf-8"))
            seller = document["sellers"][0]
            self.assertEqual("abc123", seller["seller_id"])
            self.assertEqual("https://jp.mercari.com/user/profile/abc123", seller["seller_url"])

    def test_capabilities_expose_future_management_fields(self):
        for adapter in default_adapters().values():
            capabilities = adapter.capabilities
            self.assertIsInstance(capabilities.supports_native_seller_id, bool)
            self.assertIsInstance(capabilities.supports_share_text, bool)
            self.assertIsInstance(capabilities.supports_seller_search, bool)
            self.assertIsInstance(capabilities.requires_login, bool)
            self.assertIsInstance(capabilities.supports_auction, bool)
            self.assertIsInstance(capabilities.supports_price_drop, bool)

    def test_bootstrap_does_not_dispatch_preexisting_pending_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seller = MonitoredSeller(
                seller_key="seller_bootstrap",
                seller_id="bootstrap",
                seller_identity_source="url_native_id",
                seller_name="bootstrap",
                platform="mercari",
                seller_url="https://jp.mercari.com/user/profile/bootstrap",
            )
            config = MonitorConfig(
                config_path=root / "config.yaml",
                database_path=root / "monitor.db",
                state_path=root / "state.json",
                log_path=root / "monitor.log",
                notify_price_increase=False,
                sellers=[seller],
            )
            adapter = StaticAdapter(seller)
            notifier = CountingNotifier()
            repository = SellerMonitorRepository(config.database_path)
            service = SellerMonitorService(repository, {"mercari": adapter}, notifier)
            repository.initialize()
            repository.sync_sellers([seller])
            run_id = repository.start_run("fixture", 0)
            change = repository.upsert_snapshot(run_id, adapter.snapshot)
            repository.create_event(
                event_key="fixture_pending",
                run_id=run_id,
                item_row_id=change.item_row_id,
                event_type="new_listing",
                term_type=None,
                old_price=None,
                new_price=1000,
                payload=PAYLOAD,
            )
            repository.finish_run(run_id, "success", 0, 0, 1, 0)
            service.run(config, mode="bootstrap")
            self.assertEqual(0, notifier.calls)
            self.assertEqual("pending", repository.scalar("SELECT status FROM notification_events"))


class StaticAdapter:
    def __init__(self, seller):
        self.snapshot = ListingSnapshot(
            platform=seller.platform,
            seller_key=seller.seller_key,
            seller_name=seller.seller_name,
            seller_url=seller.seller_url,
            item_id="bootstrap-item",
            item_url="https://jp.mercari.com/item/bootstrap-item",
            title="bootstrap fixture",
            image_url="https://img.example/bootstrap.jpg",
            listing_type="fixed",
            current_price=1000,
        )

    def fetch_seller(self, seller):
        return FetchResult([self.snapshot])


class CountingNotifier:
    def __init__(self):
        self.calls = 0

    def send(self, payload):
        self.calls += 1
        return NotificationResult(status="accepted")


if __name__ == "__main__":
    unittest.main()
