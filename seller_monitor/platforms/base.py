"""Platform adapter contract.

V0 adapters intentionally provide only offline URL recognition. Real fetching is
added after representative seller URLs and saved fixtures are available.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from seller_monitor.models import FetchResult, MonitoredSeller, PlatformCapabilities


class PlatformAccessNotImplemented(RuntimeError):
    pass


class PlatformAdapter(ABC):
    platform: str
    hostnames: tuple[str, ...]
    capabilities: PlatformCapabilities

    def recognizes_hostname(self, hostname: str) -> bool:
        host = hostname.lower()
        return any(host == known or host.endswith(f".{known}") for known in self.hostnames)

    @abstractmethod
    def normalize_seller_url(self, url: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def extract_seller_id(self, seller_url: str) -> str | None:
        raise NotImplementedError

    def fetch_seller(self, seller: MonitoredSeller) -> FetchResult:
        raise PlatformAccessNotImplemented(
            f"{self.platform} 真实卖家页适配尚未实现；V0 第一阶段禁止访问真实平台"
        )

