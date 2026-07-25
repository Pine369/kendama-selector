from __future__ import annotations

import json
import unittest
from dataclasses import asdict
from unittest import mock

from seller_monitor.models import MonitoredSeller
from seller_monitor.platforms.mercari import MercariAdapter, parse_items_response


PROFILE_URL = "https://jp.mercari.com/user/profile/example_seller_id"
INITIAL_URL = (
    "https://api.mercari.jp/items/get_items?"
    "seller_id=example_seller_id&limit=30&with_auction=true"
)
FILTERED_URL = (
    "https://api.mercari.jp/items/get_items?"
    "seller_id=example_seller_id&limit=30&status=on_sale&"
    "with_auction=true&exclude_archived_item=true"
)


def make_payload(
    count=6,
    *,
    statuses=None,
    has_next=False,
    duplicate=False,
    missing_identity=False,
    explicit_auction=False,
):
    statuses = statuses or ["on_sale"] * count
    data = []
    for index in range(count):
        item_id = "m9000000001" if duplicate else f"m{9_000_000_001 + index}"
        item = {
            "id": item_id,
            "seller": {"id": "example_seller_id"},
            "name": f"测试商品 {index + 1}",
            "price": 1000 + index * 500,
            "thumbnails": [f"https://example.com/images/item-{index + 1}.jpg"],
            "status": statuses[index],
            "is_archived": False,
        }
        if explicit_auction:
            item.update({"is_auction": True, "current_bid": item["price"]})
        if missing_identity:
            item.pop("id")
        data.append(item)
    meta = {} if has_next is None else {"has_next": has_next}
    return {"result": "OK", "meta": meta, "data": data}


def make_seller():
    return MonitoredSeller(
        seller_key="seller_example",
        seller_id="example_seller_id",
        seller_identity_source="url_native_id",
        seller_name="测试卖家",
        platform="mercari",
        seller_url=PROFILE_URL,
    )


class HeaderTrap:
    @property
    def headers(self):
        raise AssertionError("transport must not read request or response headers")

    def all_headers(self):
        raise AssertionError("transport must not read request or response headers")


class FakeRequest(HeaderTrap):
    def __init__(self, url, resource_type):
        self.url = url
        self.resource_type = resource_type
        self.method = "GET"


class FakeResponse(HeaderTrap):
    def __init__(self, request, status, payload=None):
        self.request = request
        self.url = request.url
        self.status = status
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeRoute:
    def __init__(self, request):
        self.request = request
        self.aborted = False

    def abort(self):
        self.aborted = True

    def continue_(self):
        return None


class FakeLocator:
    def __init__(self, page, kind, count=1):
        self.page = page
        self.kind = kind
        self._count = count

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def nth(self, _index):
        return self

    def is_visible(self):
        return True

    def click(self, timeout=None):
        self.page.click_timeouts.append(timeout)
        self.page.toggle_click_count += 1
        if self.page.scenario.get("click_error"):
            raise RuntimeError("synthetic click error")
        if self.page.scenario.get("emit_filtered", True):
            if self.page.scenario.get("emit_stale_unfiltered_after_click"):
                self.page.emit(INITIAL_URL, "xhr", 200, make_payload(has_next=True))
            self.page.emit(
                self.page.scenario.get("filtered_url", FILTERED_URL),
                "xhr",
                self.page.scenario.get("filtered_status", 200),
                self.page.scenario.get("filtered_payload", make_payload()),
            )

    def inner_text(self, timeout=None):
        self.page.body_timeouts.append(timeout)
        return self.page.scenario.get("body_text", "Mercari seller items")


class FakePage:
    def __init__(self, scenario):
        self.scenario = scenario
        self.url = PROFILE_URL
        self.handlers = {}
        self.route_handler = None
        self.goto_calls = []
        self.natural_request_urls = []
        self.toggle_click_count = 0
        self.click_timeouts = []
        self.body_timeouts = []

    def route(self, _pattern, handler):
        self.route_handler = handler

    def on(self, event, handler):
        self.handlers[event] = handler

    def emit(self, url, resource_type, status, payload=None):
        request = FakeRequest(url, resource_type)
        route = FakeRoute(request)
        if self.route_handler:
            self.route_handler(route)
        if route.aborted:
            return None
        self.natural_request_urls.append(url)
        if handler := self.handlers.get("request"):
            handler(request)
        response = FakeResponse(request, status, payload)
        if handler := self.handlers.get("response"):
            handler(response)
        return response

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        self.url = self.scenario.get("page_url", url)
        main = self.emit(url, "document", self.scenario.get("main_status", 200), None)
        if self.scenario.get("emit_initial", True):
            self.emit(
                INITIAL_URL,
                "xhr",
                self.scenario.get("initial_status", 200),
                self.scenario.get("initial_payload", make_payload(has_next=True)),
            )
        return main

    def get_by_text(self, _pattern):
        return FakeLocator(self, "toggle", count=self.scenario.get("toggle_count", 1))

    def locator(self, selector):
        if selector == "body":
            return FakeLocator(self, "body")
        if selector == 'li[data-testid="item-cell"]':
            return FakeLocator(self, "items", count=self.scenario.get("item_cell_count", 6))
        if selector == 'input[type="checkbox"]':
            return FakeLocator(self, "checkbox", count=1)
        return FakeLocator(self, "other", count=0)

    def wait_for_timeout(self, _milliseconds):
        return None


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.close_count = 0

    def new_page(self):
        return self.page

    def close(self):
        self.close_count += 1


class FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.context = FakeContext(page)
        self.context_options = None
        self.close_count = 0

    def new_context(self, **kwargs):
        self.context_options = kwargs
        return self.context

    def close(self):
        self.close_count += 1


class FakeChromium:
    def __init__(self, browser):
        self.browser = browser
        self.launch_options = None

    def launch(self, **kwargs):
        self.launch_options = kwargs
        return self.browser


class FakePlaywright:
    def __init__(self, scenario):
        self.page = FakePage(scenario)
        self.browser = FakeBrowser(self.page)
        self.chromium = FakeChromium(self.browser)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def run_transport(**scenario):
    fake = FakePlaywright(scenario)
    adapter = MercariAdapter(
        playwright_factory=lambda: fake,
        navigation_timeout_ms=10,
        response_timeout_ms=1,
    )
    result = adapter.fetch_seller(make_seller())
    return adapter, result, fake


class MercariTransportOfflineTests(unittest.TestCase):
    def test_01_six_on_sale_items_and_terminal_page_are_complete(self):
        adapter, result, fake = run_transport(filtered_payload=make_payload())
        self.assertTrue(result.complete)
        self.assertEqual(6, len(result.snapshots))
        self.assertEqual(1, adapter.last_diagnostics.navigation_count)
        self.assertEqual(2, adapter.last_diagnostics.get_items_request_count)
        self.assertEqual(2, adapter.last_diagnostics.get_items_response_count)
        self.assertEqual(6, adapter.last_diagnostics.item_count)
        self.assertFalse(adapter.last_diagnostics.has_next)
        self.assertEqual(
            {
                "limit": "30",
                "status": "on_sale",
                "with_auction": "true",
                "exclude_archived_item": "true",
            },
            adapter.last_diagnostics.filtered_query,
        )
        self.assertEqual("chrome", fake.chromium.launch_options["channel"])
        self.assertEqual(1, fake.page.toggle_click_count)
        self.assertEqual("on_sale", result.snapshots[0].raw["platform_status"])
        self.assertFalse(result.snapshots[0].raw["is_archived"])
        self.assertEqual("example_seller_id", result.snapshots[0].raw["seller_id"])

    def test_02_has_next_true_is_incomplete_and_returns_no_snapshots(self):
        adapter, result, _ = run_transport(filtered_payload=make_payload(has_next=True))
        self.assertFalse(result.complete)
        self.assertEqual([], result.snapshots)
        self.assertEqual("filtered_has_next_true", adapter.last_diagnostics.error)

    def test_03_mixed_sold_out_item_is_incomplete(self):
        statuses = ["on_sale"] * 5 + ["sold_out"]
        adapter, result, _ = run_transport(
            filtered_payload=make_payload(statuses=statuses)
        )
        self.assertFalse(result.complete)
        self.assertEqual([], result.snapshots)
        self.assertEqual(
            "filtered_response_contains_non_on_sale_items",
            adapter.last_diagnostics.error,
        )

    def test_04_missing_filtered_response_is_incomplete(self):
        adapter, result, _ = run_transport(emit_filtered=False)
        self.assertFalse(result.complete)
        self.assertEqual([], result.snapshots)
        self.assertIn("waiting_for_filtered_items_failed", adapter.last_diagnostics.error)

    def test_05_http_429_is_incomplete(self):
        adapter, result, _ = run_transport(filtered_status=429)
        self.assertFalse(result.complete)
        self.assertEqual(1, adapter.last_diagnostics.http_429_count)
        self.assertEqual("filtered_get_items_http_429", adapter.last_diagnostics.error)

    def test_06_captcha_page_is_incomplete(self):
        adapter, result, _ = run_transport(
            body_text="Verify you are human CAPTCHA",
            item_cell_count=0,
        )
        self.assertFalse(result.complete)
        self.assertTrue(adapter.last_diagnostics.captcha_detected)
        self.assertEqual("captcha_detected", adapter.last_diagnostics.error)

    def test_07_missing_item_id_and_url_is_incomplete(self):
        adapter, result, _ = run_transport(
            filtered_payload=make_payload(count=1, missing_identity=True)
        )
        self.assertFalse(result.complete)
        self.assertEqual([], result.snapshots)
        self.assertEqual(
            "filtered_response_has_identity_or_shape_errors",
            adapter.last_diagnostics.error,
        )

    def test_08_duplicate_item_ids_are_deduplicated(self):
        payload = make_payload(count=2, duplicate=True)
        parsed = parse_items_response(payload)
        self.assertEqual(1, len(parsed.items))
        self.assertFalse(parsed.complete)
        adapter, result, _ = run_transport(filtered_payload=payload)
        self.assertFalse(result.complete)
        self.assertEqual([], result.snapshots)

    def test_09_transport_forces_all_list_items_to_unknown(self):
        _, result, _ = run_transport(
            filtered_payload=make_payload(explicit_auction=True)
        )
        self.assertTrue(result.complete)
        self.assertEqual({"unknown"}, {item.listing_type for item in result.snapshots})

    def test_10_headers_and_secrets_are_never_read_or_recorded(self):
        adapter, result, _ = run_transport()
        self.assertTrue(result.complete)
        serialized = json.dumps(asdict(adapter.last_diagnostics)).lower()
        for forbidden in ("dpop", "cookie", "authorization", "set-cookie", "token"):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn("seller_id", serialized)

    def test_11_context_and_browser_close_after_exception(self):
        adapter, result, fake = run_transport(click_error=True)
        self.assertFalse(result.complete)
        self.assertTrue(adapter.last_diagnostics.context_closed)
        self.assertTrue(adapter.last_diagnostics.browser_closed)
        self.assertEqual(1, fake.browser.context.close_count)
        self.assertEqual(1, fake.browser.close_count)

    def test_12_transport_opens_no_item_detail_page(self):
        _, result, fake = run_transport()
        self.assertTrue(result.complete)
        self.assertEqual([PROFILE_URL], [call[0] for call in fake.page.goto_calls])
        self.assertFalse(any("/item/" in url for url in fake.page.natural_request_urls))
        self.assertEqual(0, result.detail_page_request_count)

    def test_13_transport_does_not_replay_get_items_with_http_client(self):
        with mock.patch("requests.sessions.Session.request") as request:
            _, result, fake = run_transport()
            request.assert_not_called()
        self.assertTrue(result.complete)
        self.assertEqual(1, len(fake.page.goto_calls))

    def test_14_missing_has_next_is_incomplete(self):
        adapter, result, _ = run_transport(filtered_payload=make_payload(has_next=None))
        self.assertFalse(result.complete)
        self.assertEqual("filtered_has_next_missing", adapter.last_diagnostics.error)

    def test_15_status_query_must_be_on_sale(self):
        filtered_url = FILTERED_URL.replace("status=on_sale", "status=sold_out")
        adapter, result, _ = run_transport(filtered_url=filtered_url)
        self.assertFalse(result.complete)
        self.assertEqual("filtered_status_is_not_on_sale", adapter.last_diagnostics.error)

    def test_16_login_wall_is_incomplete(self):
        adapter, result, _ = run_transport(
            body_text="ログインしてください",
            item_cell_count=0,
            page_url="https://jp.mercari.com/login",
        )
        self.assertFalse(result.complete)
        self.assertTrue(adapter.last_diagnostics.login_wall_detected)
        self.assertEqual("login_wall_detected", adapter.last_diagnostics.error)

    def test_17_main_document_403_is_incomplete(self):
        adapter, result, _ = run_transport(main_status=403, emit_initial=False)
        self.assertFalse(result.complete)
        self.assertEqual("profile_document_http_403", adapter.last_diagnostics.error)

    def test_18_delayed_unfiltered_response_is_not_mistaken_for_filtered(self):
        adapter, result, _ = run_transport(emit_stale_unfiltered_after_click=True)
        self.assertTrue(result.complete)
        self.assertEqual("on_sale", adapter.last_diagnostics.filtered_query["status"])
        self.assertEqual(3, adapter.last_diagnostics.get_items_response_count)

    def test_19_with_auction_must_remain_enabled(self):
        filtered_url = FILTERED_URL.replace("with_auction=true", "with_auction=false")
        adapter, result, _ = run_transport(filtered_url=filtered_url)
        self.assertFalse(result.complete)
        self.assertEqual("filtered_with_auction_is_not_true", adapter.last_diagnostics.error)

    def test_20_archived_items_must_be_excluded(self):
        filtered_url = FILTERED_URL.replace(
            "exclude_archived_item=true", "exclude_archived_item=false"
        )
        adapter, result, _ = run_transport(filtered_url=filtered_url)
        self.assertFalse(result.complete)
        self.assertEqual(
            "filtered_exclude_archived_item_is_not_true",
            adapter.last_diagnostics.error,
        )


if __name__ == "__main__":
    unittest.main()
