from __future__ import annotations

from urllib.parse import urlsplit

from seller_monitor.models import PlatformCapabilities
from seller_monitor.platforms.base import PlatformAdapter
from seller_monitor.utils import canonicalize_url


class RakutenAdapter(PlatformAdapter):
    platform = "rakuten"
    hostnames = ("www.rakuten.co.jp",)
    capabilities = PlatformCapabilities(
        supports_native_seller_id=False,
        supports_share_text=True,
        supports_seller_search=False,
        requires_login=False,
        supports_auction=False,
        supports_price_drop=True,
    )

    def normalize_seller_url(self, url: str) -> str:
        normalized = canonicalize_url(url)
        path = urlsplit(normalized).path.strip("/")
        if not path or "/" in path:
            raise ValueError("Rakuten V0 仅接受 www.rakuten.co.jp/<shop> 店铺主页 URL")
        return f"https://www.rakuten.co.jp/{path}"

    def extract_seller_id(self, seller_url: str) -> str | None:
        return None

