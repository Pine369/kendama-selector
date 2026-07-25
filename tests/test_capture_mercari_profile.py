from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scripts.capture_mercari_profile as capture_module
from scripts.capture_mercari_profile import (
    CaptureCheckpoint,
    _atomic_write_json,
)


SELLER_ID = "9876543210"


def make_payload(item_numbers, statuses, *, has_next):
    return {
        "result": "OK",
        "meta": {"has_next": has_next},
        "data": [
            {
                "id": f"m12345{number:04d}",
                "seller": {"id": SELLER_ID, "name": "private seller"},
                "name": f"private title {number}",
                "price": 5000 + number,
                "thumbnails": [f"https://private-images.example/item-{number}.jpg"],
                "status": status,
                "pager_id": 800000 + number,
            }
            for number, status in zip(item_numbers, statuses)
        ],
    }


def record_response(
    checkpoint: CaptureCheckpoint,
    phase: str,
    payload,
    *,
    status=200,
    url_suffix="",
    error=None,
):
    url = (
        "https://api.mercari.jp/items/get_items"
        f"?seller_id={SELLER_ID}&limit=2&status=on_sale"
        "&with_auction=true&exclude_archived_item=false"
        f"{url_suffix}&token=do-not-save"
    )
    checkpoint.record_request("xhr", get_items=True)
    observation = checkpoint.add_get_items_request(url, "GET", phase)
    checkpoint.record_response(status, get_items=True)
    checkpoint.finish_get_items_response(
        observation,
        status=status,
        payload=payload,
        error=error,
    )
    return observation


class CaptureCheckpointTests(unittest.TestCase):
    def new_checkpoint(self, root: str) -> CaptureCheckpoint:
        checkpoint = CaptureCheckpoint(Path(root) / "run", SELLER_ID)
        checkpoint.browser_name = "synthetic-browser"
        checkpoint.mark_stage("browser_started")
        checkpoint.counters.navigation_count = 1
        checkpoint.record_request("document")
        checkpoint.document_status = 200
        checkpoint.mark_stage("profile_loaded")
        return checkpoint

    def test_01_initial_payload_survives_later_failure(self):
        with tempfile.TemporaryDirectory() as root:
            checkpoint = self.new_checkpoint(root)
            record_response(
                checkpoint,
                "initial",
                make_payload([1, 2], ["on_sale", "sold_out"], has_next=True),
            )
            checkpoint.mark_stage("initial_items_captured")
            before_failure = (checkpoint.run_dir / "initial_items_sanitized.json").read_bytes()
            checkpoint.fail("filter_clicked", RuntimeError("toggle failed"))

            self.assertTrue((checkpoint.run_dir / "initial_items_sanitized.json").exists())
            self.assertEqual(
                before_failure,
                (checkpoint.run_dir / "initial_items_sanitized.json").read_bytes(),
            )
            self.assertTrue((checkpoint.run_dir / "error_summary.json").exists())
            manifest = json.loads((checkpoint.run_dir / "run_manifest.json").read_text("utf-8"))
            self.assertEqual("failed", manifest["current_stage"])
            self.assertEqual("initial_items_captured", manifest["last_successful_stage"])

    def test_02_filtered_payload_survives_pagination_failure(self):
        with tempfile.TemporaryDirectory() as root:
            checkpoint = self.new_checkpoint(root)
            record_response(checkpoint, "initial", make_payload([1, 2], ["on_sale"] * 2, has_next=True))
            record_response(checkpoint, "filter", make_payload([1, 3], ["on_sale"] * 2, has_next=True))
            checkpoint.filter_clicked = True
            checkpoint.mark_stage("filtered_items_captured")
            checkpoint.record_scroll()
            checkpoint.fail("pagination_trigger_attempted", RuntimeError("no next request"))

            self.assertTrue((checkpoint.run_dir / "initial_items_sanitized.json").exists())
            self.assertTrue((checkpoint.run_dir / "filtered_items_sanitized.json").exists())
            self.assertFalse((checkpoint.run_dir / "next_page_items_sanitized.json").exists())

    def test_03_has_next_false_is_distinct_from_scroll_failure(self):
        with tempfile.TemporaryDirectory() as root:
            checkpoint = self.new_checkpoint(root)
            record_response(checkpoint, "filter", make_payload([1], ["on_sale"], has_next=False))
            checkpoint.fail("pagination_trigger_attempted", RuntimeError("terminal filter"))
            error = json.loads((checkpoint.run_dir / "error_summary.json").read_text("utf-8"))
            self.assertEqual("filtered_has_next_false", error["failure_kind"])

    def test_04_has_next_true_without_next_request_is_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            checkpoint = self.new_checkpoint(root)
            record_response(checkpoint, "filter", make_payload([1], ["on_sale"], has_next=True))
            checkpoint.record_scroll()
            checkpoint.fail("pagination_trigger_attempted", RuntimeError("no request"))
            error = json.loads((checkpoint.run_dir / "error_summary.json").read_text("utf-8"))
            self.assertEqual("filtered_has_next_true_but_no_next_request", error["failure_kind"])

    def test_05_successful_two_page_capture_keeps_all_payloads(self):
        with tempfile.TemporaryDirectory() as root:
            checkpoint = self.new_checkpoint(root)
            record_response(checkpoint, "initial", make_payload([8, 9], ["sold_out"] * 2, has_next=True))
            record_response(checkpoint, "filter", make_payload([1, 2], ["on_sale"] * 2, has_next=True))
            record_response(
                checkpoint,
                "pagination",
                make_payload([2, 3], ["on_sale"] * 2, has_next=False),
                url_suffix="&pager_id=800002",
            )
            checkpoint.mark_stage("next_page_captured")
            checkpoint.outcome = "completed_with_next_page"
            checkpoint.mark_stage("completed")

            for name in (
                "initial_items_sanitized.json",
                "filtered_items_sanitized.json",
                "next_page_items_sanitized.json",
            ):
                self.assertTrue((checkpoint.run_dir / name).exists(), name)
            summary = json.loads(
                (checkpoint.run_dir / "request_summary_sanitized.json").read_text("utf-8")
            )
            self.assertEqual(1, summary["responses"][2]["duplicate_with_previous"])
            self.assertEqual([1, 2, 3], [row["sequence"] for row in summary["responses"]])

    def test_06_next_page_parse_failure_keeps_previous_checkpoints(self):
        with tempfile.TemporaryDirectory() as root:
            checkpoint = self.new_checkpoint(root)
            record_response(checkpoint, "initial", make_payload([1], ["on_sale"], has_next=True))
            record_response(checkpoint, "filter", make_payload([1], ["on_sale"], has_next=True))
            record_response(checkpoint, "pagination", None, error="invalid JSON token=secret-value")
            checkpoint.fail("next_page_captured", RuntimeError("next page invalid"))

            self.assertTrue((checkpoint.run_dir / "filtered_items_sanitized.json").exists())
            self.assertFalse((checkpoint.run_dir / "next_page_items_sanitized.json").exists())
            error = json.loads((checkpoint.run_dir / "error_summary.json").read_text("utf-8"))
            self.assertEqual("next_page_parse_failed", error["failure_kind"])

    def test_07_failure_guard_persists_error_in_finally_path(self):
        with tempfile.TemporaryDirectory() as root:
            checkpoint = CaptureCheckpoint(Path(root) / "run", SELLER_ID)
            with self.assertRaisesRegex(RuntimeError, "page exploded"):
                with checkpoint.failure_guard():
                    checkpoint.set_action("profile_loaded")
                    raise RuntimeError("page exploded")
            self.assertTrue((checkpoint.run_dir / "run_manifest.json").exists())
            self.assertTrue((checkpoint.run_dir / "error_summary.json").exists())

    def test_08_atomic_writer_flushes_and_produces_readable_json(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "atomic.json"
            with mock.patch.object(capture_module.os, "fsync", wraps=capture_module.os.fsync) as fsync:
                _atomic_write_json(path, {"ok": True})
            self.assertEqual({"ok": True}, json.loads(path.read_text("utf-8")))
            self.assertTrue(fsync.called)
            self.assertEqual([], list(Path(root).glob("*.tmp")))

    def test_09_payload_identity_title_and_image_are_sanitized(self):
        with tempfile.TemporaryDirectory() as root:
            checkpoint = self.new_checkpoint(root)
            record_response(checkpoint, "initial", make_payload([1], ["on_sale"], has_next=False))
            serialized = (checkpoint.run_dir / "initial_items_sanitized.json").read_text("utf-8")
            for forbidden in (
                SELLER_ID,
                "m123450001",
                "private title 1",
                "private-images.example",
                "private seller",
            ):
                self.assertNotIn(forbidden, serialized)
            self.assertIn("example_seller_id", serialized)
            self.assertIn("https://example.com/images/", serialized)

    def test_10_credentials_and_storage_values_are_never_written(self):
        with tempfile.TemporaryDirectory() as root:
            checkpoint = self.new_checkpoint(root)
            observation = record_response(
                checkpoint,
                "initial",
                make_payload([1], ["on_sale"], has_next=False),
            )
            observation.error = (
                "Cookie=cookie-value Authorization=BearerValue DPoP=dpop-value "
                "token=token-value"
            )
            checkpoint.checkpoint()
            serialized = "\n".join(
                path.read_text("utf-8") for path in checkpoint.run_dir.glob("*.json")
            )
            for secret in (
                "cookie-value",
                "BearerValue",
                "dpop-value",
                "token-value",
                "do-not-save",
            ):
                self.assertNotIn(secret, serialized)
            self.assertNotIn("localStorage", serialized)
            self.assertNotIn("sessionStorage", serialized)

    def test_11_request_counters_use_one_mutually_exclusive_bucket(self):
        counters = capture_module.RequestCounters()
        for resource_type in ("document", "script", "stylesheet", "fetch", "xhr", "image"):
            counters.record_request(resource_type, get_items=resource_type == "xhr")
        values = counters.as_dict()
        self.assertEqual(6, values["request_count"])
        self.assertEqual(1, values["get_items_request_count"])
        self.assertEqual(1, values["xhr_request_count"])
        self.assertEqual(1, values["other_request_count"])

    def test_12_duplicate_item_counts_are_correct(self):
        with tempfile.TemporaryDirectory() as root:
            checkpoint = self.new_checkpoint(root)
            record_response(checkpoint, "initial", make_payload([1, 2], ["on_sale"] * 2, has_next=True))
            record_response(checkpoint, "filter", make_payload([2, 3], ["on_sale"] * 2, has_next=True))
            summary = json.loads(
                (checkpoint.run_dir / "request_summary_sanitized.json").read_text("utf-8")
            )
            self.assertEqual(1, summary["responses"][1]["duplicate_with_previous"])
            self.assertEqual(1, summary["responses"][1]["duplicate_with_first"])

    def test_13_status_distribution_is_preserved_without_titles(self):
        with tempfile.TemporaryDirectory() as root:
            checkpoint = self.new_checkpoint(root)
            record_response(
                checkpoint,
                "initial",
                make_payload([1, 2, 3], ["on_sale", "sold_out", "on_sale"], has_next=False),
            )
            summary = json.loads(
                (checkpoint.run_dir / "request_summary_sanitized.json").read_text("utf-8")
            )
            self.assertEqual(
                {"on_sale": 2, "sold_out": 1},
                summary["responses"][0]["status_distribution"],
            )

    def test_14_checkpoint_tests_do_not_call_browser_or_network(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            capture_module, "sync_playwright"
        ) as playwright, mock.patch("requests.sessions.Session.request") as network:
            checkpoint = self.new_checkpoint(root)
            record_response(checkpoint, "initial", make_payload([1], ["on_sale"], has_next=False))
            playwright.assert_not_called()
            network.assert_not_called()

    def test_15_import_has_no_browser_network_or_write_side_effect(self):
        with tempfile.TemporaryDirectory() as root, mock.patch(
            "playwright.sync_api.sync_playwright"
        ) as playwright, mock.patch("requests.sessions.Session.request") as network:
            before = set(Path(root).iterdir())
            importlib.reload(capture_module)
            after = set(Path(root).iterdir())
            playwright.assert_not_called()
            network.assert_not_called()
            self.assertEqual(before, after)
        importlib.reload(capture_module)

    def test_16_terminal_filtered_page_completes_without_scroll_or_error(self):
        with tempfile.TemporaryDirectory() as root:
            checkpoint = self.new_checkpoint(root)
            record_response(checkpoint, "filter", make_payload([1], ["on_sale"], has_next=False))
            checkpoint.filter_clicked = True
            checkpoint.mark_stage("filtered_items_captured")
            checkpoint.outcome = "filtered_has_next_false"
            checkpoint.mark_stage("completed")

            manifest = json.loads((checkpoint.run_dir / "run_manifest.json").read_text("utf-8"))
            self.assertEqual("filtered_has_next_false", manifest["outcome"])
            self.assertFalse(manifest["pagination_trigger_attempted"])
            self.assertEqual(0, manifest["scroll_count"])
            self.assertFalse((checkpoint.run_dir / "error_summary.json").exists())


if __name__ == "__main__":
    unittest.main()
