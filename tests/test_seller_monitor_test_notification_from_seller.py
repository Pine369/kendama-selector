from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import yaml

from seller_monitor.main import main, safe_print, send_test_notification_from_seller_interactive
from seller_monitor.models import FetchResult, ListingSnapshot, NotificationResult
from seller_monitor.notifier import REAL_ITEM_TEST_DISCLAIMER, REAL_ITEM_TEST_TITLE


def make_snapshot(
    *,
    title="真实展示测试商品",
    image_url="https://images.example.com/items/display.jpg",
    current_price=8000,
    item_url="https://jp.mercari.com/item/m00000000001",
    platform_status="on_sale",
):
    return ListingSnapshot(
        platform="mercari",
        seller_key="seller_test_display",
        seller_name="测试配置卖家",
        seller_url="https://jp.mercari.com/user/profile/example_display_seller",
        item_id="m00000000001",
        item_url=item_url,
        title=title,
        image_url=image_url,
        listing_type="unknown",
        current_price=current_price,
        status=platform_status,
        observed_at="2026-07-25T15:00:00+08:00",
        raw={"platform_status": platform_status},
    )


def make_latest_result(
    snapshots=None,
    *,
    has_next=False,
    window_complete=True,
):
    return FetchResult(
        list(snapshots if snapshots is not None else [make_snapshot()]),
        complete=False,
        coverage="latest_window",
        window_complete=window_complete,
        has_next=has_next if window_complete else None,
        window_limit=30,
    )


class FakeAdapter:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def fetch_seller(self, seller):
        self.calls.append(seller)
        return self.result


class FakeNotifier:
    def __init__(self, token, *, status="accepted"):
        self.token = token
        self.status = status
        self.calls = []

    def send_test_notification(self, payload, **kwargs):
        self.calls.append((payload, kwargs))
        return NotificationResult(status=self.status)


@dataclass
class FixturePaths:
    root: Path
    config: Path
    env: Path
    database: Path
    state: Path


class RealSellerNotificationOfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = FixturePaths(
            root=root,
            config=root / "seller_monitor.yaml",
            env=root / "seller_monitor.env",
            database=root / "seller_monitor.db",
            state=root / "seller_monitor_state.json",
        )
        self.paths.env.write_text("PUSHPLUS_TOKEN=synthetic-secret\n", encoding="utf-8")
        self._write_config()

    def tearDown(self):
        self.temp.cleanup()

    def _write_config(self, *, sellers=None):
        if sellers is None:
            sellers = [
                {
                    "seller_key": "seller_test_display",
                    "seller_id": "example_display_seller",
                    "seller_identity_source": "url_native_id",
                    "seller_name": "测试配置卖家",
                    "platform": "mercari",
                    "seller_url": "https://jp.mercari.com/user/profile/example_display_seller",
                    "enabled": True,
                }
            ]
        document = {
            "version": 1,
            "settings": {
                "database_path": self.paths.database.name,
                "state_path": self.paths.state.name,
                "log_path": "seller_monitor.log",
                "notify_price_increase": False,
            },
            "sellers": sellers,
        }
        self.paths.config.write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    def _run(
        self,
        result,
        *,
        answer="y",
        notifier=None,
        output=None,
    ):
        adapter = FakeAdapter(result)
        notifier = notifier or FakeNotifier("synthetic-secret")
        output = output if output is not None else []
        return_code = send_test_notification_from_seller_interactive(
            str(self.paths.config),
            str(self.paths.env),
            adapters={"mercari": adapter},
            notifier_factory=lambda token: notifier,
            input_func=lambda _: answer,
            output_func=output.append,
        )
        return return_code, adapter, notifier, output

    def test_cli_flag_routes_only_to_real_seller_test_entrypoint(self):
        with mock.patch(
            "seller_monitor.main.send_test_notification_from_seller_interactive", return_value=0
        ) as command:
            result = main(
                [
                    "--test-notification-from-seller",
                    "--config",
                    str(self.paths.config),
                    "--env",
                    str(self.paths.env),
                ]
            )
        self.assertEqual(0, result)
        command.assert_called_once()

    def test_valid_latest_window_selects_first_on_sale_listing(self):
        first_valid = make_snapshot(title="第一件有效商品")
        second_valid = make_snapshot(title="第二件有效商品", item_url="https://jp.mercari.com/item/m00000000002")
        result = make_latest_result([first_valid, second_valid])
        code, adapter, notifier, _ = self._run(result)
        self.assertEqual(0, code)
        self.assertEqual(1, len(adapter.calls))
        self.assertEqual(1, len(notifier.calls))
        self.assertEqual("第一件有效商品", notifier.calls[0][0]["title"])

    def test_invalid_latest_window_does_not_send(self):
        code, _, notifier, _ = self._run(
            make_latest_result(window_complete=False)
        )
        self.assertEqual(1, code)
        self.assertEqual([], notifier.calls)

    def test_has_next_true_valid_latest_window_can_send_once(self):
        code, _, notifier, _ = self._run(make_latest_result(has_next=True))
        self.assertEqual(0, code)
        self.assertEqual(1, len(notifier.calls))

    def test_full_coverage_result_does_not_send(self):
        code, _, notifier, output = self._run(
            FetchResult([make_snapshot()], complete=True)
        )
        self.assertEqual(1, code)
        self.assertEqual([], notifier.calls)
        self.assertIn("不是 latest_window", "\n".join(output))

    def test_empty_latest_window_does_not_send(self):
        code, _, notifier, _ = self._run(make_latest_result([]))
        self.assertEqual(1, code)
        self.assertEqual([], notifier.calls)

    def test_mixed_status_window_does_not_send(self):
        result = make_latest_result(
            [make_snapshot(), make_snapshot(platform_status="sold_out")]
        )
        code, _, notifier, _ = self._run(result)
        self.assertEqual(1, code)
        self.assertEqual([], notifier.calls)

    def test_any_invalid_candidate_rejects_entire_window(self):
        result = make_latest_result([make_snapshot(), make_snapshot(image_url="")])
        code, _, notifier, _ = self._run(result)
        self.assertEqual(1, code)
        self.assertEqual([], notifier.calls)

    def test_missing_image_does_not_send(self):
        code, _, notifier, _ = self._run(make_latest_result([make_snapshot(image_url="")]))
        self.assertEqual(1, code)
        self.assertEqual([], notifier.calls)

    def test_missing_price_does_not_send(self):
        code, _, notifier, _ = self._run(
            make_latest_result([make_snapshot(current_price=None)])
        )
        self.assertEqual(1, code)
        self.assertEqual([], notifier.calls)

    def test_missing_title_does_not_send(self):
        code, _, notifier, _ = self._run(make_latest_result([make_snapshot(title="")]))
        self.assertEqual(1, code)
        self.assertEqual([], notifier.calls)

    def test_missing_item_url_does_not_send(self):
        code, _, notifier, _ = self._run(make_latest_result([make_snapshot(item_url="")]))
        self.assertEqual(1, code)
        self.assertEqual([], notifier.calls)

    def test_multiple_enabled_sellers_stop_before_fetch_or_send(self):
        seller = yaml.safe_load(self.paths.config.read_text(encoding="utf-8"))["sellers"][0]
        second = {**seller, "seller_key": "seller_second", "seller_id": "example_second"}
        second["seller_url"] = "https://jp.mercari.com/user/profile/example_second"
        self._write_config(sellers=[seller, second])
        adapter = FakeAdapter(make_latest_result())
        notifier = FakeNotifier("synthetic-secret")
        code = send_test_notification_from_seller_interactive(
            str(self.paths.config),
            str(self.paths.env),
            adapters={"mercari": adapter},
            notifier_factory=lambda token: notifier,
            input_func=lambda _: "y",
            output_func=lambda _: None,
        )
        self.assertEqual(2, code)
        self.assertEqual([], adapter.calls)
        self.assertEqual([], notifier.calls)

    def test_non_mercari_seller_stops_before_fetch_or_send(self):
        self._write_config(
            sellers=[
                {
                    "seller_key": "seller_yahoo_test",
                    "seller_id": None,
                    "seller_identity_source": "canonical_url",
                    "seller_name": "测试 Yahoo 卖家",
                    "platform": "yahoo_auctions",
                    "seller_url": "https://auctions.yahoo.co.jp/seller/example_seller",
                    "enabled": True,
                }
            ]
        )
        adapter = FakeAdapter(make_latest_result())
        notifier = FakeNotifier("synthetic-secret")
        code = send_test_notification_from_seller_interactive(
            str(self.paths.config),
            str(self.paths.env),
            adapters={"mercari": adapter},
            notifier_factory=lambda token: notifier,
            input_func=lambda _: "y",
            output_func=lambda _: None,
        )
        self.assertEqual(2, code)
        self.assertEqual([], adapter.calls)
        self.assertEqual([], notifier.calls)

    def test_unconfirmed_message_is_not_sent(self):
        code, _, notifier, output = self._run(
            make_latest_result(), answer="n"
        )
        self.assertEqual(1, code)
        self.assertEqual([], notifier.calls)
        self.assertIn("已取消，未发送测试消息。", output)

    def test_confirmed_message_sends_once_with_test_title_and_disclaimer(self):
        code, _, notifier, output = self._run(make_latest_result())
        self.assertEqual(0, code)
        self.assertEqual(1, len(notifier.calls))
        payload, kwargs = notifier.calls[0]
        self.assertEqual(REAL_ITEM_TEST_TITLE, kwargs["title"])
        self.assertEqual(REAL_ITEM_TEST_DISCLAIMER, kwargs["disclaimer"])
        self.assertEqual("测试配置卖家", payload["seller_name"])
        self.assertEqual("真实展示测试商品", payload["title"])
        rendered = "\n".join(output)
        self.assertIn(REAL_ITEM_TEST_TITLE, rendered)
        self.assertIn("图片域名：images.example.com", rendered)
        self.assertIn("不会读取或写入正式事件及商品表", rendered)
        self.assertIn(REAL_ITEM_TEST_DISCLAIMER, rendered)
        self.assertNotIn("synthetic-secret", rendered)

    def test_database_tables_file_and_state_are_unchanged(self):
        with closing(sqlite3.connect(self.paths.database)) as connection:
            connection.executescript(
                "CREATE TABLE items (id INTEGER PRIMARY KEY);"
                "CREATE TABLE price_history (id INTEGER PRIMARY KEY);"
                "CREATE TABLE notification_events (id INTEGER PRIMARY KEY);"
                "CREATE TABLE notification_attempts (id INTEGER PRIMARY KEY);"
            )
        self.paths.state.write_text('{"sentinel":true}', encoding="utf-8")
        database_before = self.paths.database.read_bytes()
        state_before = self.paths.state.read_bytes()
        code, _, notifier, _ = self._run(make_latest_result())
        self.assertEqual(0, code)
        self.assertEqual(1, len(notifier.calls))
        self.assertEqual(database_before, self.paths.database.read_bytes())
        self.assertEqual(state_before, self.paths.state.read_bytes())
        with closing(
            sqlite3.connect(f"file:{self.paths.database.as_posix()}?mode=ro", uri=True)
        ) as connection:
            for table in ("items", "price_history", "notification_events", "notification_attempts"):
                self.assertEqual(0, connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def test_repository_monitor_and_formal_event_paths_are_not_used(self):
        with (
            mock.patch("seller_monitor.main.SellerMonitorRepository") as repository,
            mock.patch("seller_monitor.main.SellerMonitorService") as monitor,
        ):
            code, _, notifier, _ = self._run(make_latest_result())
        self.assertEqual(0, code)
        self.assertEqual(1, len(notifier.calls))
        repository.assert_not_called()
        monitor.assert_not_called()

    def test_missing_token_stops_before_fetch(self):
        self.paths.env.write_text("PUSHPLUS_TOKEN=\n", encoding="utf-8")
        code, adapter, notifier, output = self._run(make_latest_result())
        self.assertEqual(2, code)
        self.assertEqual([], adapter.calls)
        self.assertEqual([], notifier.calls)
        self.assertNotIn("synthetic-secret", "\n".join(output))

    def test_gbk_output_falls_back_without_sending(self):
        snapshot = make_snapshot(title="日本語テスト商品")
        adapter = FakeAdapter(make_latest_result([snapshot]))
        notifier = FakeNotifier("synthetic-secret")
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp936", errors="strict", newline="")
        code = send_test_notification_from_seller_interactive(
            str(self.paths.config),
            str(self.paths.env),
            adapters={"mercari": adapter},
            notifier_factory=lambda token: notifier,
            input_func=lambda _: "n",
            output_func=lambda value: safe_print(value, file=stream),
        )
        stream.flush()
        rendered = buffer.getvalue().decode("cp936")
        stream.detach()
        self.assertEqual(1, code)
        self.assertEqual([], notifier.calls)
        self.assertIn("价格：JPY 8,000", rendered)
        self.assertIn("已取消", rendered)


if __name__ == "__main__":
    unittest.main()
