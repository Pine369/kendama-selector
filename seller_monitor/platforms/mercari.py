from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from seller_monitor.models import FetchResult, ListingSnapshot, MonitoredSeller, PlatformCapabilities
from seller_monitor.platforms.base import PlatformAdapter
from seller_monitor.utils import canonicalize_url, utc_now


ITEM_ID_RE = re.compile(r"^m[0-9]+$", re.IGNORECASE)
GET_ITEMS_HOST = "api.mercari.jp"
GET_ITEMS_PATH = "/items/get_items"
FILTER_QUERY_KEYS = (
    "limit",
    "status",
    "with_auction",
    "exclude_archived_item",
    "page",
    "offset",
    "cursor",
    "pager_id",
)
ANALYTICS_MARKERS = (
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "analytics",
    "segment.io",
    "sentry.io",
    "newrelic",
    "/beacon",
)
CAPTCHA_MARKERS = (
    "captcha",
    "ロボットではありません",
    "セキュリティチェック",
    "verify you are human",
)
LOGIN_WALL_MARKERS = (
    "ログインしてください",
    "please log in to continue",
    "登录后继续",
)

logger = logging.getLogger("seller_monitor.platforms.mercari")


class MercariParseError(ValueError):
    """The captured response is not a usable Mercari item-list payload."""


@dataclass(frozen=True)
class MercariParsedItem:
    item_id: str
    item_url: str
    title: str
    image_url: str
    current_price: int | None
    status: str
    raw_status: str | None
    listing_type: str
    seller_id: str | None
    is_archived: bool | None
    auction_current_bid: int | None = None
    auction_start_price: int | None = None
    auction_buyout_price: int | None = None


@dataclass(frozen=True)
class MercariParsedPage:
    items: tuple[MercariParsedItem, ...]
    has_next: bool | None
    next_cursor: str | None
    total_count: int | None
    complete: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass
class MercariTransportDiagnostics:
    stage: str = "not_started"
    navigation_count: int = 0
    network_request_count: int = 0
    response_count: int = 0
    get_items_request_count: int = 0
    get_items_response_count: int = 0
    filtered_http_status: int | None = None
    filtered_query: dict[str, str] | None = None
    item_count: int = 0
    has_next: bool | None = None
    http_403_count: int = 0
    http_429_count: int = 0
    captcha_detected: bool = False
    login_wall_detected: bool = False
    filter_clicked: bool = False
    context_closed: bool = False
    browser_closed: bool = False
    error: str | None = None


@dataclass
class _CapturedItemsResponse:
    http_status: int
    query: dict[str, str]
    payload: dict[str, Any] | None
    parse_error: str | None = None


def _first_value(mapping: dict[str, Any], names: tuple[str, ...]):
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _listing_type(raw: dict[str, Any]) -> str:
    explicit = _first_value(raw, ("listing_type", "listingType", "sale_type", "saleType", "format"))
    if isinstance(explicit, str):
        normalized = explicit.strip().lower().replace("-", "_")
        if normalized in {"auction", "auction_sale", "bidding"}:
            return "auction"
        if normalized in {"fixed", "fixed_price", "normal", "normal_sale"}:
            return "fixed"
    for key in ("is_auction", "isAuction"):
        if isinstance(raw.get(key), bool):
            return "auction" if raw[key] else "fixed"
    if isinstance(raw.get("auction"), dict):
        return "auction"
    # The captured get_items response has no explicit sale-type field. Neither
    # `price` nor `is_no_price` is sufficient evidence, so keep it unknown.
    return "unknown"


def _normalized_status(raw_status: Any) -> tuple[str, str | None]:
    if not isinstance(raw_status, str) or not raw_status:
        return "unknown", None
    mapping = {
        "on_sale": "active",
        "sold_out": "sold",
        "trading": "trading",
    }
    return mapping.get(raw_status, "unknown"), raw_status


def _thumbnail(raw: dict[str, Any]) -> str:
    thumbnails = raw.get("thumbnails")
    if isinstance(thumbnails, list):
        return next((value for value in thumbnails if isinstance(value, str) and value), "")
    value = _first_value(raw, ("thumbnail", "image_url", "imageUrl"))
    return value if isinstance(value, str) else ""


def _auction_prices(raw: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    auction = raw.get("auction") if isinstance(raw.get("auction"), dict) else {}
    current = _integer(
        _first_value(raw, ("current_bid", "currentBid", "current_bid_price", "currentBidPrice"))
    )
    start = _integer(
        _first_value(raw, ("auction_start_price", "auctionStartPrice", "start_price", "startPrice"))
    )
    buyout = _integer(
        _first_value(raw, ("buyout_price", "buyoutPrice", "auction_buyout_price", "auctionBuyoutPrice"))
    )
    if auction:
        current = current if current is not None else _integer(
            _first_value(auction, ("current_bid", "currentBid", "current_price", "currentPrice"))
        )
        start = start if start is not None else _integer(
            _first_value(auction, ("start_price", "startPrice", "starting_price", "startingPrice"))
        )
        buyout = buyout if buyout is not None else _integer(
            _first_value(auction, ("buyout_price", "buyoutPrice"))
        )
    return current, start, buyout


def _is_get_items_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.hostname == GET_ITEMS_HOST and parsed.path.rstrip("/") == GET_ITEMS_PATH


def _safe_query(url: str) -> dict[str, str]:
    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    return {
        key: values[-1]
        for key in FILTER_QUERY_KEYS
        if (values := query.get(key))
    }


def _wait_until(page, predicate: Callable[[], bool], timeout_ms: int, label: str) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(100)
    raise TimeoutError(f"timed out waiting for {label}")


def _click_on_sale_toggle(page) -> None:
    pattern = re.compile(
        r"販売中のみ表示|販売中の商品|仅显示当前在售商品|只显示当前在售商品|only show.*on sale",
        re.IGNORECASE,
    )
    text_matches = page.get_by_text(pattern)
    for index in range(text_matches.count()):
        candidate = text_matches.nth(index)
        if candidate.is_visible():
            candidate.click(timeout=5000)
            return

    checkboxes = page.locator('input[type="checkbox"]')
    visible = [
        checkboxes.nth(index)
        for index in range(checkboxes.count())
        if checkboxes.nth(index).is_visible()
    ]
    if len(visible) == 1:
        visible[0].click(timeout=5000)
        return
    if checkboxes.count() == 1:
        checkboxes.first.click(timeout=5000)
        return
    raise RuntimeError("could not identify the on-sale-only toggle reliably")


def _detect_access_wall(page) -> tuple[bool, bool]:
    body_text = ""
    item_cell_count = 0
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
        item_cell_count = page.locator('li[data-testid="item-cell"]').count()
    except Exception:
        pass
    body_lower = body_text.lower()
    captcha_detected = any(marker in body_lower for marker in CAPTCHA_MARKERS)
    login_wall_detected = item_cell_count == 0 and (
        "login" in page.url.lower()
        or any(marker in body_lower for marker in LOGIN_WALL_MARKERS)
    )
    return captcha_detected, login_wall_detected


def _default_playwright_factory():
    # Lazy import keeps URL recognition, config checks, and parser-only tests
    # free of browser startup or other import-time side effects.
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def parse_items_response(payload: str | bytes | dict[str, Any]) -> MercariParsedPage:
    """Parse a saved Mercari ``items/get_items`` JSON response without I/O."""

    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        if not payload.strip():
            raise MercariParseError("empty response body")
        try:
            document = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MercariParseError(f"invalid JSON: {exc.msg}") from exc
    elif isinstance(payload, dict):
        document = payload
    else:
        raise MercariParseError("response must be JSON text, bytes, or an object")

    if document.get("result") not in (None, "OK"):
        raise MercariParseError(f"Mercari result is not OK: {document.get('result')!r}")
    raw_items = document.get("data")
    if not isinstance(raw_items, list):
        raise MercariParseError("response data is not a list")
    meta = document.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    has_next = meta.get("has_next")
    if not isinstance(has_next, bool):
        has_next = meta.get("hasNext")
    if not isinstance(has_next, bool):
        has_next = None
    cursor = _first_value(meta, ("next_cursor", "nextCursor", "cursor", "page_token", "nextPageToken"))
    next_cursor = str(cursor) if cursor not in (None, "") else None
    total_count = _integer(_first_value(meta, ("total_count", "totalCount")))

    errors: list[str] = []
    warnings: list[str] = []
    parsed_items: list[MercariParsedItem] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            errors.append(f"data[{index}] is not an object")
            continue
        item_id = raw.get("id")
        if not isinstance(item_id, str) or not ITEM_ID_RE.fullmatch(item_id):
            errors.append(f"data[{index}] lacks a stable Mercari item id")
            continue
        if item_id in seen_ids:
            errors.append(f"duplicate item id at data[{index}]")
            continue
        seen_ids.add(item_id)
        title = raw.get("name") if isinstance(raw.get("name"), str) else ""
        if not title:
            warnings.append(f"{item_id}: missing title")
        image_url = _thumbnail(raw)
        if not image_url:
            warnings.append(f"{item_id}: missing image")
        current_price = _integer(raw.get("price"))
        if current_price is None:
            warnings.append(f"{item_id}: missing or invalid price")
        status, raw_status = _normalized_status(raw.get("status"))
        if status == "unknown":
            warnings.append(f"{item_id}: unknown status")
        listing_type = _listing_type(raw)
        auction_current_bid, auction_start_price, auction_buyout_price = _auction_prices(raw)
        seller = raw.get("seller") if isinstance(raw.get("seller"), dict) else {}
        seller_id = seller.get("id")
        seller_id = str(seller_id) if seller_id not in (None, "") else None
        is_archived = raw.get("is_archived")
        is_archived = is_archived if isinstance(is_archived, bool) else None
        parsed_items.append(
            MercariParsedItem(
                item_id=item_id,
                item_url=f"https://jp.mercari.com/item/{item_id}",
                title=title,
                image_url=image_url,
                current_price=current_price,
                status=status,
                raw_status=raw_status,
                listing_type=listing_type,
                seller_id=seller_id,
                is_archived=is_archived,
                auction_current_bid=auction_current_bid,
                auction_start_price=auction_start_price,
                auction_buyout_price=auction_buyout_price,
            )
        )

    if not raw_items:
        errors.append("empty item list")
    if has_next is None:
        errors.append("pagination completeness is unknown")
    if has_next and next_cursor is None:
        warnings.append("response has another page but exposes no explicit next cursor")
    complete = bool(raw_items) and not errors and has_next is False
    return MercariParsedPage(
        items=tuple(parsed_items),
        has_next=has_next,
        next_cursor=next_cursor,
        total_count=total_count,
        complete=complete,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


class MercariAdapter(PlatformAdapter):
    platform = "mercari"
    hostnames = ("jp.mercari.com",)
    capabilities = PlatformCapabilities(
        supports_native_seller_id=True,
        supports_share_text=True,
        supports_seller_search=False,
        requires_login=False,
        supports_auction=True,
        supports_price_drop=True,
    )
    _seller_pattern = re.compile(r"^/user/profile/([^/?#]+)$", re.IGNORECASE)

    def __init__(
        self,
        *,
        playwright_factory: Callable[[], Any] | None = None,
        navigation_timeout_ms: int = 45000,
        response_timeout_ms: int = 20000,
    ):
        self._playwright_factory = playwright_factory or _default_playwright_factory
        self.navigation_timeout_ms = navigation_timeout_ms
        self.response_timeout_ms = response_timeout_ms
        self.last_diagnostics = MercariTransportDiagnostics()

    def normalize_seller_url(self, url: str) -> str:
        normalized = canonicalize_url(url)
        match = self._seller_pattern.match(urlsplit(normalized).path)
        if not match:
            raise ValueError("Mercari 仅接受形如 /user/profile/<seller_id> 的卖家主页 URL")
        return f"https://jp.mercari.com/user/profile/{match.group(1)}"

    def extract_seller_id(self, seller_url: str) -> str | None:
        match = self._seller_pattern.match(urlsplit(seller_url).path)
        return match.group(1) if match else None

    def _failed_result(self, diagnostics: MercariTransportDiagnostics, reason: str) -> FetchResult:
        diagnostics.error = reason
        logger.warning(
            "Mercari seller fetch incomplete: stage=%s navigation=%d get_items=%d "
            "items=%d has_next=%s error=%s",
            diagnostics.stage,
            diagnostics.navigation_count,
            diagnostics.get_items_response_count,
            diagnostics.item_count,
            diagnostics.has_next,
            reason,
        )
        return FetchResult(
            snapshots=[],
            complete=False,
            list_page_request_count=diagnostics.get_items_request_count,
            detail_page_request_count=0,
            network_request_count=diagnostics.network_request_count,
            coverage="latest_window",
            window_complete=False,
            has_next=diagnostics.has_next,
            window_limit=30,
        )

    def _result_from_filtered_response(
        self,
        seller: MonitoredSeller,
        captured: _CapturedItemsResponse,
        diagnostics: MercariTransportDiagnostics,
    ) -> FetchResult:
        diagnostics.filtered_http_status = captured.http_status
        diagnostics.filtered_query = dict(captured.query)
        if captured.http_status != 200:
            return self._failed_result(
                diagnostics, f"filtered_get_items_http_{captured.http_status}"
            )
        if captured.query.get("status") != "on_sale":
            return self._failed_result(diagnostics, "filtered_status_is_not_on_sale")
        if captured.query.get("with_auction") != "true":
            return self._failed_result(diagnostics, "filtered_with_auction_is_not_true")
        if captured.query.get("exclude_archived_item") != "true":
            return self._failed_result(
                diagnostics, "filtered_exclude_archived_item_is_not_true"
            )
        if captured.query.get("limit") != "30":
            return self._failed_result(diagnostics, "filtered_limit_is_not_30")
        if captured.payload is None:
            return self._failed_result(
                diagnostics, captured.parse_error or "filtered_response_has_no_json_payload"
            )
        try:
            parsed = parse_items_response(captured.payload)
        except MercariParseError:
            return self._failed_result(diagnostics, "filtered_response_parse_error")

        diagnostics.item_count = len(parsed.items)
        diagnostics.has_next = parsed.has_next
        if parsed.has_next is None:
            return self._failed_result(diagnostics, "filtered_has_next_missing")
        if parsed.errors:
            return self._failed_result(diagnostics, "filtered_response_has_identity_or_shape_errors")
        if any(item.raw_status != "on_sale" for item in parsed.items):
            return self._failed_result(diagnostics, "filtered_response_contains_non_on_sale_items")
        if any(not item.item_id or not item.item_url for item in parsed.items):
            return self._failed_result(diagnostics, "filtered_response_has_unstable_item_identity")
        if diagnostics.http_403_count:
            return self._failed_result(diagnostics, "http_403_observed")
        if diagnostics.http_429_count:
            return self._failed_result(diagnostics, "http_429_observed")
        if diagnostics.captcha_detected:
            return self._failed_result(diagnostics, "captcha_detected")
        if diagnostics.login_wall_detected:
            return self._failed_result(diagnostics, "login_wall_detected")

        observed_at = utc_now()
        snapshots = [
            ListingSnapshot(
                platform=self.platform,
                seller_key=seller.seller_key,
                seller_name=seller.seller_name,
                seller_url=seller.seller_url,
                item_id=item.item_id,
                item_url=item.item_url,
                title=item.title,
                image_url=item.image_url,
                listing_type="unknown",
                current_price=item.current_price,
                status="on_sale",
                observed_at=observed_at,
                raw={
                    "seller_id": item.seller_id or seller.seller_id,
                    "platform_status": item.raw_status,
                    "is_archived": item.is_archived,
                },
            )
            for item in parsed.items
        ]
        diagnostics.stage = "completed"
        logger.info(
            "Mercari seller latest window complete: navigation=%d get_items=%d items=%d has_next=%s",
            diagnostics.navigation_count,
            diagnostics.get_items_response_count,
            diagnostics.item_count,
            diagnostics.has_next,
        )
        return FetchResult(
            snapshots=snapshots,
            complete=False,
            list_page_request_count=diagnostics.get_items_request_count,
            detail_page_request_count=0,
            network_request_count=diagnostics.network_request_count,
            coverage="latest_window",
            window_complete=True,
            has_next=parsed.has_next,
            window_limit=30,
        )

    def fetch_seller(self, seller: MonitoredSeller) -> FetchResult:
        diagnostics = MercariTransportDiagnostics(stage="browser_starting")
        self.last_diagnostics = diagnostics
        browser = None
        context = None
        phase = {"value": "initial"}
        captured: dict[str, _CapturedItemsResponse | None] = {
            "initial": None,
            "filtered": None,
        }

        try:
            with self._playwright_factory() as playwright:
                browser = playwright.chromium.launch(channel="chrome", headless=True)
                context = browser.new_context(
                    locale="ja-JP",
                    viewport={"width": 1365, "height": 900},
                    accept_downloads=False,
                )
                page = context.new_page()

                def route_handler(route):
                    request = route.request
                    url_lower = request.url.lower()
                    if request.resource_type in {"image", "font", "media"}:
                        route.abort()
                        return
                    if any(marker in url_lower for marker in ANALYTICS_MARKERS):
                        route.abort()
                        return
                    if (
                        request.resource_type == "document"
                        and request.url.rstrip("/") != seller.seller_url.rstrip("/")
                    ):
                        route.abort()
                        return
                    route.continue_()

                def on_request(request):
                    diagnostics.network_request_count += 1
                    if request.resource_type == "document":
                        diagnostics.navigation_count += 1
                    if _is_get_items_url(request.url):
                        diagnostics.get_items_request_count += 1

                def on_response(response):
                    diagnostics.response_count += 1
                    is_get_items = _is_get_items_url(response.url)
                    resource_type = getattr(response.request, "resource_type", "")
                    is_relevant = is_get_items or resource_type == "document"
                    if response.status == 403 and is_relevant:
                        diagnostics.http_403_count += 1
                    if response.status == 429 and is_relevant:
                        diagnostics.http_429_count += 1
                    if not is_get_items:
                        return
                    diagnostics.get_items_response_count += 1
                    key = phase["value"]
                    if key not in captured or captured[key] is not None:
                        return
                    query = _safe_query(response.url)
                    # A second unfiltered request can finish near the toggle
                    # click. Only a response with an explicit status parameter
                    # is eligible to be the post-click filtered response.
                    if key == "filtered" and "status" not in query:
                        return
                    payload = None
                    parse_error = None
                    try:
                        candidate = response.json()
                        if not isinstance(candidate, dict):
                            raise ValueError("response JSON is not an object")
                        payload = candidate
                    except Exception:
                        parse_error = "get_items_response_json_error"
                    captured[key] = _CapturedItemsResponse(
                        http_status=response.status,
                        query=query,
                        payload=payload,
                        parse_error=parse_error,
                    )

                page.route("**/*", route_handler)
                page.on("request", on_request)
                page.on("response", on_response)

                diagnostics.stage = "profile_loading"
                main_response = page.goto(
                    seller.seller_url,
                    wait_until="domcontentloaded",
                    timeout=self.navigation_timeout_ms,
                )
                diagnostics.stage = "profile_loaded"
                if main_response is not None and main_response.status == 403:
                    return self._failed_result(diagnostics, "profile_document_http_403")
                if main_response is not None and main_response.status == 429:
                    return self._failed_result(diagnostics, "profile_document_http_429")

                (
                    diagnostics.captcha_detected,
                    diagnostics.login_wall_detected,
                ) = _detect_access_wall(page)
                if diagnostics.captcha_detected:
                    return self._failed_result(diagnostics, "captcha_detected")
                if diagnostics.login_wall_detected:
                    return self._failed_result(diagnostics, "login_wall_detected")

                diagnostics.stage = "waiting_for_initial_items"
                _wait_until(
                    page,
                    lambda: captured["initial"] is not None,
                    self.response_timeout_ms,
                    "initial get_items response",
                )

                phase["value"] = "filtered"
                diagnostics.stage = "clicking_on_sale_filter"
                _click_on_sale_toggle(page)
                diagnostics.filter_clicked = True
                diagnostics.stage = "waiting_for_filtered_items"
                _wait_until(
                    page,
                    lambda: captured["filtered"] is not None,
                    self.response_timeout_ms,
                    "filtered get_items response",
                )
                captcha_detected, login_wall_detected = _detect_access_wall(page)
                diagnostics.captcha_detected = diagnostics.captcha_detected or captcha_detected
                diagnostics.login_wall_detected = (
                    diagnostics.login_wall_detected or login_wall_detected
                )
                diagnostics.stage = "validating_filtered_items"
                filtered = captured["filtered"]
                if filtered is None:
                    return self._failed_result(diagnostics, "filtered_response_not_captured")
                return self._result_from_filtered_response(seller, filtered, diagnostics)
        except Exception as exc:
            # Do not persist or log the Playwright exception text: it can embed
            # target URLs or browser internals. Stage plus exception type is
            # sufficient for operational diagnosis without leaking headers.
            return self._failed_result(
                diagnostics,
                f"{diagnostics.stage}_failed:{type(exc).__name__}",
            )
        finally:
            if context is not None:
                try:
                    context.close()
                    diagnostics.context_closed = True
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                    diagnostics.browser_closed = True
                except Exception:
                    pass
