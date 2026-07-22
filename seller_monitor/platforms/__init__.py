from __future__ import annotations

from urllib.parse import urlsplit

from seller_monitor.platforms.base import PlatformAdapter
from seller_monitor.platforms.mercari import MercariAdapter
from seller_monitor.platforms.rakuten import RakutenAdapter
from seller_monitor.platforms.yahoo_auctions import YahooAuctionsAdapter
from seller_monitor.utils import extract_urls


def default_adapters() -> dict[str, PlatformAdapter]:
    adapters = [MercariAdapter(), YahooAuctionsAdapter(), RakutenAdapter()]
    return {adapter.platform: adapter for adapter in adapters}


def resolve_seller_input(raw_input: str, adapters=None) -> tuple[PlatformAdapter, str, str | None]:
    adapters = adapters or default_adapters()
    urls = extract_urls(raw_input)
    if not urls and raw_input.strip().lower().startswith(("http://", "https://")):
        urls = [raw_input.strip()]
    if not urls:
        raise ValueError("输入中没有可识别的 HTTP(S) 卖家主页 URL；V0 不支持昵称搜索")

    errors = []
    for url in urls:
        hostname = (urlsplit(url).hostname or "").lower()
        for adapter in adapters.values():
            if not adapter.recognizes_hostname(hostname):
                continue
            try:
                normalized = adapter.normalize_seller_url(url)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            return adapter, normalized, adapter.extract_seller_id(normalized)

    if errors:
        raise ValueError("；".join(dict.fromkeys(errors)))
    raise ValueError("未知或不支持的平台卖家主页；请手工补充 platform 和规范化 seller_url")


__all__ = ["default_adapters", "resolve_seller_input", "PlatformAdapter"]

