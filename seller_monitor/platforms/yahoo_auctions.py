from __future__ import annotations

import re
from urllib.parse import urlsplit

from seller_monitor.models import PlatformCapabilities
from seller_monitor.platforms.base import PlatformAdapter
from seller_monitor.utils import canonicalize_url


class YahooAuctionsAdapter(PlatformAdapter):
    platform = "yahoo_auctions"
    hostnames = ("auctions.yahoo.co.jp",)
    capabilities = PlatformCapabilities(
        supports_native_seller_id=True,
        supports_share_text=True,
        supports_seller_search=False,
        requires_login=False,
        supports_auction=True,
        supports_price_drop=True,
    )
    _seller_pattern = re.compile(r"^/seller/([^/?#]+)$", re.IGNORECASE)

    def normalize_seller_url(self, url: str) -> str:
        normalized = canonicalize_url(url)
        match = self._seller_pattern.match(urlsplit(normalized).path)
        if not match:
            raise ValueError("Yahoo Auctions 仅接受形如 /seller/<seller_id> 的卖家主页 URL")
        return f"https://auctions.yahoo.co.jp/seller/{match.group(1)}"

    def extract_seller_id(self, seller_url: str) -> str | None:
        match = self._seller_pattern.match(urlsplit(seller_url).path)
        return match.group(1) if match else None

