"""Domain models shared by the seller monitor layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, TypeAlias


ListingType: TypeAlias = Literal["fixed", "auction", "unknown"]
FetchCoverage: TypeAlias = Literal["full", "latest_window"]


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
    listing_type: ListingType
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
    coverage: FetchCoverage = "full"
    window_complete: Optional[bool] = None
    has_next: Optional[bool] = None
    window_limit: Optional[int] = None

    def __post_init__(self):
        if self.coverage not in {"full", "latest_window"}:
            raise ValueError(f"未知 FetchResult coverage: {self.coverage}")
        if self.coverage == "latest_window" and self.complete:
            raise ValueError("latest_window 不能声明 full listing complete")
        if self.window_complete is None:
            object.__setattr__(self, "window_complete", self.complete)
        if self.coverage == "latest_window":
            if self.window_limit is None or self.window_limit <= 0:
                raise ValueError("latest_window 必须声明正数 window_limit")
            if self.window_complete and self.has_next not in {True, False}:
                raise ValueError("有效 latest_window 必须明确声明 has_next")

    @property
    def full_listing_complete(self) -> bool:
        return self.complete


@dataclass(frozen=True)
class SellerLatestWindow:
    seller_key: str
    scan_run_id: str
    captured_at: str
    ordered_identity_keys: tuple[str, ...]
    window_limit: int
    has_next: bool
    coverage: Literal["latest_window"] = "latest_window"


@dataclass(frozen=True)
class ItemChange:
    item_row_id: int
    identity_key: str
    is_new: bool
    previous_price: Optional[int]
    previous_listing_type: Optional[ListingType]
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
