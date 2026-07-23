from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from seller_monitor.models import PlatformCapabilities
from seller_monitor.platforms.base import PlatformAdapter
from seller_monitor.utils import canonicalize_url


ITEM_ID_RE = re.compile(r"^m[0-9]+$", re.IGNORECASE)


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

    def normalize_seller_url(self, url: str) -> str:
        normalized = canonicalize_url(url)
        match = self._seller_pattern.match(urlsplit(normalized).path)
        if not match:
            raise ValueError("Mercari 仅接受形如 /user/profile/<seller_id> 的卖家主页 URL")
        return f"https://jp.mercari.com/user/profile/{match.group(1)}"

    def extract_seller_id(self, seller_url: str) -> str | None:
        match = self._seller_pattern.match(urlsplit(seller_url).path)
        return match.group(1) if match else None
