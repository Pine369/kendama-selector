"""Domain models shared by the seller monitor layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class PlatformCapabilities:
    supports_native_seller_id: bool
    supports_share_text: bool
    supports_seller_search: bool
    requires_login: bool
    supports_auction: bool
    supports_price_drop: bool


@dataclass
class MonitoredSeller:
    seller_key: str
    seller_id: Optional[str]
    seller_identity_source: str
    seller_name: str
    platform: str
    seller_url: str
    enabled: bool = True
    deleted_at: Optional[str] = None
    baseline_completed_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_error: Optional[str] = None


@dataclass
class ListingSnapshot:
    platform: str
    seller_key: str
    seller_name: str
    seller_url: str
    item_url: str
    title: str
    image_url: str
    listing_type: str
    current_price: Optional[int]
    item_id: Optional[str] = None
    auction_start_price: Optional[int] = None
    auction_buyout_price: Optional[int] = None
    status: str = "active"
    observed_at: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FetchResult:
    snapshots: list[ListingSnapshot]
    complete: bool = True
    list_page_request_count: int = 0
    detail_page_request_count: int = 0
    network_request_count: int = 0


@dataclass(frozen=True)
class ItemChange:
    item_row_id: int
    identity_key: str
    is_new: bool
    previous_price: Optional[int]
    previous_listing_type: Optional[str]
    previous_auction_start_price: Optional[int]
    previous_auction_buyout_price: Optional[int]


@dataclass(frozen=True)
class NotificationResult:
    status: str
    provider_message_id: Optional[str] = None
    provider_code: Optional[str] = None
    provider_message: Optional[str] = None
    http_status: Optional[int] = None
    error: Optional[str] = None

