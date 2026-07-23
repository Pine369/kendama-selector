from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest import mock

from scripts.capture_mercari_profile import (
    FixtureSanitizer,
    _collect_fixture_sensitive_values,
    _feature_scan,
    _fixture_audit,
)
from seller_monitor.platforms.mercari import MercariParseError, parse_items_response


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "seller_monitor"
    / "mercari"
    / "items_page_1_sanitized.json"
)


class MercariResponseParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_text = FIXTURE_PATH.read_text(encoding="utf-8")
        cls.fixture = json.loads(cls.fixture_text)

    def test_fixture_parses_thirty_items(self):
        page = parse_items_response(self.fixture_text)
        self.assertEqual(30, len(page.items))
        self.assertEqual(30, len({item.item_id for item in page.items}))

    def test_first_page_with_has_next_is_never_complete(self):
        page = parse_items_response(self.fixture)
        self.assertTrue(page.has_next)
        self.assertFalse(page.complete)
        self.assertIsNone(page.next_cursor)
        self.assertTrue(any("no explicit next cursor" in warning for warning in page.warnings))

    def test_last_page_can_be_complete(self):
        payload = copy.deepcopy(self.fixture)
        payload["meta"]["has_next"] = False
        page = parse_items_response(payload)
        self.assertTrue(page.complete)
        self.assertEqual((), page.errors)

    def test_item_identity_and_urls_are_stable(self):
        item = parse_items_response(self.fixture).items[0]
        self.assertRegex(item.item_id, r"^m[0-9]+$")
        self.assertEqual(f"https://jp.mercari.com/item/{item.item_id}", item.item_url)
        self.assertEqual("example_seller_id", item.seller_id)

    def test_status_values_are_normalized(self):
        page = parse_items_response(self.fixture)
        counts = {}
        for item in page.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        self.assertEqual({"active": 3, "sold": 26, "trading": 1}, counts)

    def test_captured_response_does_not_guess_listing_type(self):
        page = parse_items_response(self.fixture)
        self.assertEqual({"unknown"}, {item.listing_type for item in page.items})
        self.assertTrue(all(item.auction_current_bid is None for item in page.items))

    def test_explicit_auction_fields_are_mapped(self):
        payload = {"result": "OK", "meta": {"has_next": False}, "data": [copy.deepcopy(self.fixture["data"][0])]}
        item = payload["data"][0]
        item.update(
            {
                "id": "m9000000999",
                "is_auction": True,
                "current_bid": 4500,
                "auction_start_price": 3000,
                "buyout_price": 9000,
            }
        )
        parsed = parse_items_response(payload).items[0]
        self.assertEqual("auction", parsed.listing_type)
        self.assertEqual(4500, parsed.auction_current_bid)
        self.assertEqual(3000, parsed.auction_start_price)
        self.assertEqual(9000, parsed.auction_buyout_price)

    def test_explicit_non_auction_boolean_maps_to_fixed(self):
        payload = {"result": "OK", "meta": {"has_next": False}, "data": [copy.deepcopy(self.fixture["data"][0])]}
        payload["data"][0].update({"id": "m9000000998", "is_auction": False})
        self.assertEqual("fixed", parse_items_response(payload).items[0].listing_type)

    def test_duplicate_item_ids_are_deduplicated_and_incomplete(self):
        payload = {"result": "OK", "meta": {"has_next": False}, "data": [
            copy.deepcopy(self.fixture["data"][0]),
            copy.deepcopy(self.fixture["data"][0]),
        ]}
        page = parse_items_response(payload)
        self.assertEqual(1, len(page.items))
        self.assertFalse(page.complete)
        self.assertTrue(any("duplicate item id" in error for error in page.errors))

    def test_missing_item_id_is_skipped_and_incomplete(self):
        payload = {"result": "OK", "meta": {"has_next": False}, "data": [copy.deepcopy(self.fixture["data"][0])]}
        payload["data"][0].pop("id")
        page = parse_items_response(payload)
        self.assertEqual(0, len(page.items))
        self.assertFalse(page.complete)
        self.assertTrue(any("stable Mercari item id" in error for error in page.errors))

    def test_missing_optional_fields_are_warnings(self):
        payload = {"result": "OK", "meta": {"has_next": False}, "data": [copy.deepcopy(self.fixture["data"][0])]}
        item = payload["data"][0]
        item["id"] = "m9000000997"
        item.pop("name", None)
        item.pop("price", None)
        item.pop("thumbnails", None)
        page = parse_items_response(payload)
        self.assertTrue(page.complete)
        self.assertEqual(3, len(page.warnings))

    def test_explicit_cursor_and_total_count_are_preserved(self):
        payload = copy.deepcopy(self.fixture)
        payload["meta"].update({"next_cursor": "fixture-cursor", "total_count": 420})
        page = parse_items_response(payload)
        self.assertEqual("fixture-cursor", page.next_cursor)
        self.assertEqual(420, page.total_count)
        self.assertFalse(page.complete)

    def test_empty_and_malformed_responses_are_not_usable(self):
        with self.assertRaises(MercariParseError):
            parse_items_response("")
        with self.assertRaises(MercariParseError):
            parse_items_response("not json")
        page = parse_items_response({"result": "OK", "meta": {"has_next": False}, "data": []})
        self.assertFalse(page.complete)
        self.assertIn("empty item list", page.errors)

    def test_non_ok_and_wrong_shape_raise(self):
        with self.assertRaises(MercariParseError):
            parse_items_response({"result": "ERROR", "data": []})
        with self.assertRaises(MercariParseError):
            parse_items_response({"result": "OK", "data": {}})

    def test_parser_is_strictly_offline(self):
        with mock.patch("requests.sessions.Session.request") as request:
            page = parse_items_response(self.fixture_text)
            request.assert_not_called()
        self.assertEqual(30, len(page.items))

    def test_fixture_contains_no_real_capture_identifiers(self):
        forbidden = (
            "static.mercdn.net",
            "mercdn.net",
            "LEGAXIS",
            "Kendama",
        )
        self.assertTrue(all(value not in self.fixture_text for value in forbidden))
        self.assertIn("example_seller_id", self.fixture_text)
        self.assertIn("https://example.com/images/", self.fixture_text)

    def test_capture_scoring_recognizes_fixture_as_item_list(self):
        score, features = _feature_scan(self.fixture, self.fixture_text)
        self.assertGreaterEqual(score, 45)
        self.assertEqual(30, features["item_like_object_count"])
        self.assertTrue(features["has_multiple_items"])

    def test_sanitizer_rejects_no_real_identifiers_or_image_hosts(self):
        raw = {
            "result": "OK",
            "meta": {"has_next": True},
            "data": [{
                "id": "m123456789",
                "seller": {"id": 9876543210, "name": "private seller"},
                "name": "private title",
                "price": 9876,
                "thumbnails": ["https://static.mercdn.net/private.jpg"],
                "status": "on_sale",
            }],
        }
        sensitive = _collect_fixture_sensitive_values(raw, "9876543210")
        sanitized = FixtureSanitizer("9876543210", sensitive).sanitize(raw)
        audit = _fixture_audit(sanitized, sensitive, "9876543210")
        self.assertTrue(audit["passed"])
        serialized = json.dumps(sanitized, ensure_ascii=False)
        self.assertNotIn("private seller", serialized)
        self.assertNotIn("static.mercdn.net", serialized)


if __name__ == "__main__":
    unittest.main()
