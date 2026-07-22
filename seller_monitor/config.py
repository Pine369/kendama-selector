"""Configuration parsing and safe YAML updates."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from seller_monitor.models import MonitoredSeller
from seller_monitor.platforms import default_adapters
from seller_monitor.utils import stable_hash


DEFAULT_CONFIG_PATH = "seller_monitor.yaml"
DEFAULT_ENV_PATH = "seller_monitor.env"


@dataclass(frozen=True)
class MonitorConfig:
    config_path: Path
    database_path: Path
    state_path: Path
    log_path: Path
    notify_price_increase: bool
    sellers: list[MonitoredSeller]


def make_seller_key(platform: str, seller_id: str | None, seller_url: str) -> str:
    anchor = f"native:{seller_id}" if seller_id else f"url:{seller_url}"
    return stable_hash(platform, anchor, prefix="seller_")[:31]


def default_document() -> dict[str, Any]:
    return {
        "version": 1,
        "settings": {
            "database_path": "seller_monitor.db",
            "state_path": "seller_monitor_state.json",
            "log_path": "seller_monitor.log",
            "notify_price_increase": False,
        },
        "sellers": [],
    }


def _resolve(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> MonitorConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    if document.get("version") != 1:
        raise ValueError("seller_monitor.yaml 的 version 必须为 1")

    settings = document.get("settings") or {}
    raw_sellers = document.get("sellers") or []
    if not isinstance(raw_sellers, list):
        raise ValueError("sellers 必须是列表")

    adapters = default_adapters()
    sellers: list[MonitoredSeller] = []
    seen_keys: set[str] = set()
    seen_native: set[tuple[str, str]] = set()
    seen_urls: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_sellers, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 个 seller 必须是对象")
        platform = str(raw.get("platform") or "").strip().lower()
        if platform not in adapters:
            raise ValueError(f"第 {index} 个 seller 的平台不支持: {platform or '(空)'}")
        adapter = adapters[platform]
        seller_url = adapter.normalize_seller_url(str(raw.get("seller_url") or ""))
        seller_id = raw.get("seller_id")
        seller_id = str(seller_id).strip() if seller_id not in (None, "") else None
        extracted_seller_id = adapter.extract_seller_id(seller_url)
        if seller_id and extracted_seller_id and seller_id != extracted_seller_id:
            raise ValueError(
                f"第 {index} 个 seller 的 seller_id 与主页 URL 不一致: "
                f"{seller_id} != {extracted_seller_id}"
            )
        seller_id = seller_id or extracted_seller_id
        seller_key = str(raw.get("seller_key") or "").strip() or make_seller_key(
            platform, seller_id, seller_url
        )
        seller_name = str(raw.get("seller_name") or "").strip()
        if not seller_name:
            raise ValueError(f"第 {index} 个 seller 缺少 seller_name")
        identity_source = str(
            raw.get("seller_identity_source")
            or ("url_native_id" if seller_id else "canonical_url")
        )
        if seller_key in seen_keys:
            raise ValueError(f"重复 seller_key: {seller_key}")
        if seller_id and (platform, seller_id) in seen_native:
            raise ValueError(f"重复 platform + seller_id: {platform}/{seller_id}")
        if (platform, seller_url) in seen_urls:
            raise ValueError(f"重复卖家主页: {platform}/{seller_url}")
        seen_keys.add(seller_key)
        seen_urls.add((platform, seller_url))
        if seller_id:
            seen_native.add((platform, seller_id))
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"第 {index} 个 seller 的 enabled 必须是 true 或 false")
        sellers.append(
            MonitoredSeller(
                seller_key=seller_key,
                seller_id=seller_id,
                seller_identity_source=identity_source,
                seller_name=seller_name,
                platform=platform,
                seller_url=seller_url,
                enabled=enabled,
                deleted_at=raw.get("deleted_at"),
            )
        )

    base = config_path.resolve().parent
    return MonitorConfig(
        config_path=config_path,
        database_path=_resolve(base, str(settings.get("database_path", "seller_monitor.db"))),
        state_path=_resolve(base, str(settings.get("state_path", "seller_monitor_state.json"))),
        log_path=_resolve(base, str(settings.get("log_path", "seller_monitor.log"))),
        notify_price_increase=_boolean_setting(settings, "notify_price_increase", False),
        sellers=sellers,
    )


def _boolean_setting(settings: dict, key: str, default: bool) -> bool:
    value = settings.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"settings.{key} 必须是 true 或 false")
    return value


def load_env_file(path: str | Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = Path(path)
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def pushplus_token(env_path: str | Path = DEFAULT_ENV_PATH) -> str | None:
    values = load_env_file(env_path)
    value = values.get("PUSHPLUS_TOKEN")
    if not value or "your_" in value.lower():
        return None
    return value


def append_seller(path: str | Path, seller: MonitoredSeller) -> None:
    config_path = Path(path)
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle) or default_document()
    else:
        document = default_document()
    document.setdefault("version", 1)
    document.setdefault("settings", default_document()["settings"])
    raw_sellers = document.setdefault("sellers", [])
    if not isinstance(raw_sellers, list):
        raise ValueError("现有配置的 sellers 不是列表")
    for raw in raw_sellers:
        if not isinstance(raw, dict):
            continue
        if raw.get("seller_key") == seller.seller_key:
            raise ValueError(f"卖家已存在: {seller.seller_key}")
        if str(raw.get("platform", "")).lower() == seller.platform:
            if seller.seller_id and str(raw.get("seller_id") or "") == seller.seller_id:
                raise ValueError(f"卖家已存在: {seller.platform}/{seller.seller_id}")
            if str(raw.get("seller_url") or "") == seller.seller_url:
                raise ValueError(f"卖家主页已存在: {seller.seller_url}")
    raw_sellers.append(
        {
            "seller_key": seller.seller_key,
            "seller_id": seller.seller_id,
            "seller_identity_source": seller.seller_identity_source,
            "seller_name": seller.seller_name,
            "platform": seller.platform,
            "seller_url": seller.seller_url,
            "enabled": seller.enabled,
        }
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(document, handle, allow_unicode=True, sort_keys=False)
        os.replace(temp_path, config_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
