from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

import requests

from seller_monitor.main import main, safe_print, send_test_notification_interactive
from seller_monitor.models import NotificationResult
from seller_monitor.notifier import (
    PREVIEW_PAYLOAD,
    PREVIEW_TITLE,
    TEST_NOTIFICATION_DISCLAIMER,
    PushPlusNotifier,
)


class FakeResponse:
    def __init__(self, *, status_code=200, code=200, message="accepted"):
        self.status_code = status_code
        self.code = code
        self.message = message

    def json(self):
        return {"code": self.code, "msg": self.message, "data": "synthetic-provider-id"}


class FakeSession:
    def __init__(self, *, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


class NoReconfigureCp936Stream:
    encoding = None

    def __init__(self):
        self.values = []

    def write(self, value):
        value.encode("cp936")
        self.values.append(value)

    def flush(self):
        return None


class BrokenStream:
    encoding = "utf-8"

    def write(self, value):
        raise RuntimeError("ordinary stream failure")


class TestNotificationCliTests(unittest.TestCase):
    def test_safe_print_falls_back_to_jpy_on_cp936_stream(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp936", errors="strict", newline="")
        safe_print("价格：¥8,000", file=stream, flush=True)
        rendered = buffer.getvalue().decode("cp936")
        stream.detach()
        self.assertEqual("价格：JPY 8,000\n", rendered)

    def test_safe_print_preserves_yen_on_utf8_stream(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="utf-8", errors="strict", newline="")
        safe_print("价格：¥8,000", file=stream, flush=True)
        rendered = buffer.getvalue().decode("utf-8")
        stream.detach()
        self.assertEqual("价格：¥8,000\n", rendered)

    def test_safe_print_falls_back_without_reconfigure(self):
        stream = NoReconfigureCp936Stream()
        safe_print("价格：¥8,000", file=stream)
        self.assertEqual(["价格：JPY 8,000\n"], stream.values)

    def test_safe_print_supports_redirected_stdout(self):
        redirected = io.StringIO()
        with redirect_stdout(redirected):
            safe_print("价格：¥8,000")
        self.assertEqual("价格：¥8,000\n", redirected.getvalue())

    def test_safe_print_redacts_token_assignments(self):
        output = io.StringIO()
        safe_print("PUSHPLUS_TOKEN=must-not-appear", file=output)
        self.assertEqual("PUSHPLUS_TOKEN=[REDACTED]\n", output.getvalue())
        self.assertNotIn("must-not-appear", output.getvalue())

    def test_safe_print_does_not_swallow_ordinary_stream_errors(self):
        with self.assertRaisesRegex(RuntimeError, "ordinary stream failure"):
            safe_print("ordinary output", file=BrokenStream())

    def test_missing_env_file_refuses_without_prompt_or_send(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "missing.env"
            with mock.patch("seller_monitor.main.PushPlusNotifier") as notifier:
                result = send_test_notification_interactive(
                    str(env),
                    input_func=lambda _: self.fail("missing token must not prompt"),
                    output_func=lambda _: None,
                )
        self.assertEqual(2, result)
        notifier.assert_not_called()

    def test_empty_token_refuses_without_prompt_or_send(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "seller_monitor.env"
            env.write_text("PUSHPLUS_TOKEN=\n", encoding="utf-8")
            with mock.patch("seller_monitor.main.PushPlusNotifier") as notifier:
                result = send_test_notification_interactive(
                    str(env),
                    input_func=lambda _: self.fail("empty token must not prompt"),
                    output_func=lambda _: None,
                )
        self.assertEqual(2, result)
        notifier.assert_not_called()

    def test_unconfirmed_message_is_not_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "seller_monitor.env"
            env.write_text("PUSHPLUS_TOKEN=synthetic-secret\n", encoding="utf-8")
            output = []
            with mock.patch("seller_monitor.main.PushPlusNotifier") as notifier:
                result = send_test_notification_interactive(
                    str(env), input_func=lambda _: "n", output_func=output.append
                )
        self.assertEqual(1, result)
        notifier.assert_not_called()
        self.assertIn("已取消，未发送测试消息。", output)
        self.assertNotIn("synthetic-secret", "\n".join(output))

    def test_unconfirmed_test_preview_is_safe_on_cp936_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "seller_monitor.env"
            env.write_text("PUSHPLUS_TOKEN=synthetic-secret\n", encoding="utf-8")
            buffer = io.BytesIO()
            stream = io.TextIOWrapper(buffer, encoding="cp936", errors="strict", newline="")
            with mock.patch("seller_monitor.main.PushPlusNotifier") as notifier:
                result = send_test_notification_interactive(
                    str(env),
                    input_func=lambda _: "n",
                    output_func=lambda value: safe_print(value, file=stream),
                )
            stream.flush()
            rendered = buffer.getvalue().decode("cp936")
            stream.detach()
        self.assertEqual(1, result)
        notifier.assert_not_called()
        self.assertIn("价格：JPY 8,000", rendered)
        self.assertIn("已取消，未发送测试消息。", rendered)
        self.assertNotIn("synthetic-secret", rendered)

    def test_confirmed_cli_sends_once_without_monitor_config_or_database_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / "seller_monitor.env"
            config = root / "seller_monitor.yaml"
            database = root / "seller_monitor.db"
            env.write_text("PUSHPLUS_TOKEN=synthetic-secret\n", encoding="utf-8")
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(
                    "CREATE TABLE notification_events (id INTEGER PRIMARY KEY);"
                    "CREATE TABLE notification_attempts (id INTEGER PRIMARY KEY);"
                )
            database_before = database.read_bytes()
            output = []
            notifier_instance = mock.Mock()
            notifier_instance.send_test_notification.return_value = NotificationResult(status="accepted")
            with (
                mock.patch("builtins.input", return_value="y"),
                mock.patch("seller_monitor.main.PushPlusNotifier", return_value=notifier_instance) as notifier,
                mock.patch("seller_monitor.main.load_config") as load_config,
                mock.patch("seller_monitor.main.default_adapters") as adapters,
                mock.patch("seller_monitor.main.run_monitor") as run_monitor,
                mock.patch("seller_monitor.platforms.mercari._default_playwright_factory") as browser,
                mock.patch("seller_monitor.main.safe_print", side_effect=output.append),
            ):
                result = main(
                    [
                        "--test-notification",
                        "--env",
                        str(env),
                        "--config",
                        str(config),
                    ]
                )

            self.assertEqual(0, result)
            notifier.assert_called_once_with("synthetic-secret")
            notifier_instance.send_test_notification.assert_called_once_with(dict(PREVIEW_PAYLOAD))
            load_config.assert_not_called()
            adapters.assert_not_called()
            run_monitor.assert_not_called()
            browser.assert_not_called()
            self.assertFalse(config.exists())
            self.assertEqual(database_before, database.read_bytes())
            with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0])
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM notification_attempts").fetchone()[0])
            rendered_output = "\n".join(str(line) for line in output)
            for expected in (
                PREVIEW_TITLE,
                "事件：新上架",
                "平台：Mercari",
                "卖家：测试卖家",
                "类型：待确认",
                "商品：测试剑玉商品",
                "价格：¥8,000",
                "https://example.com/images/test-kendama.jpg",
                "https://example.com/items/test-item",
                "合成测试时间",
                TEST_NOTIFICATION_DISCLAIMER,
                "accepted（PushPlus 已接受，不代表微信已送达）",
            ):
                self.assertIn(expected, rendered_output)
            self.assertNotIn("synthetic-secret", rendered_output)

    def test_test_notifier_uses_exact_synthetic_html_and_sends_once(self):
        session = FakeSession(response=FakeResponse())
        result = PushPlusNotifier("fake-token", session=session).send_test_notification()
        self.assertEqual("accepted", result.status)
        self.assertEqual(1, len(session.calls))
        body = session.calls[0][1]["json"]
        self.assertEqual(PREVIEW_TITLE, body["title"])
        self.assertEqual("html", body["template"])
        self.assertEqual("wechat", body["channel"])
        for expected in (
            "<strong>事件：</strong>新上架",
            "<strong>平台：</strong>Mercari",
            "<strong>卖家：</strong>测试卖家",
            "<strong>类型：</strong>待确认",
            "<strong>商品：</strong>测试剑玉商品",
            "<strong>价格：</strong>¥8,000",
            'src="https://example.com/images/test-kendama.jpg"',
            'href="https://example.com/items/test-item"',
            TEST_NOTIFICATION_DISCLAIMER,
        ):
            self.assertIn(expected, body["content"])

    def test_test_notifier_read_timeout_is_unknown_without_retry(self):
        session = FakeSession(error=requests.ReadTimeout("offline timeout"))
        result = PushPlusNotifier("fake-token", session=session).send_test_notification()
        self.assertEqual("delivery_unknown", result.status)
        self.assertEqual(1, len(session.calls))

    def test_test_notifier_explicit_rejection_is_not_retried(self):
        session = FakeSession(response=FakeResponse(status_code=400, code=500, message="rejected"))
        result = PushPlusNotifier("fake-token", session=session).send_test_notification()
        self.assertEqual("rejected", result.status)
        self.assertEqual(1, len(session.calls))

    def test_test_notifier_connect_failure_is_retryable_but_not_retried(self):
        session = FakeSession(error=requests.ConnectionError("offline connect failure"))
        result = PushPlusNotifier("fake-token", session=session).send_test_notification()
        self.assertEqual("retryable_failure", result.status)
        self.assertEqual(1, len(session.calls))

    def test_cli_displays_nonaccepted_statuses_without_retry(self):
        expected_messages = {
            "rejected": "rejected（PushPlus 明确拒绝）",
            "delivery_unknown": "delivery_unknown（结果未知，不会自动重试）",
            "retryable_failure": "retryable_failure（连接前失败，本命令不会自动重试）",
        }
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "seller_monitor.env"
            env.write_text("PUSHPLUS_TOKEN=synthetic-secret\n", encoding="utf-8")
            for status, expected in expected_messages.items():
                with self.subTest(status=status):
                    output = []
                    instance = mock.Mock()
                    instance.send_test_notification.return_value = NotificationResult(status=status)
                    with mock.patch("seller_monitor.main.PushPlusNotifier", return_value=instance):
                        result = send_test_notification_interactive(
                            str(env), input_func=lambda _: "yes", output_func=output.append
                        )
                    self.assertEqual(1, result)
                    instance.send_test_notification.assert_called_once_with(dict(PREVIEW_PAYLOAD))
                    self.assertIn(expected, output[-1])
                    self.assertNotIn("synthetic-secret", "\n".join(output))


if __name__ == "__main__":
    unittest.main()
