"""Command-line entrypoint for the independent seller monitor."""

from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml

from seller_monitor.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_ENV_PATH,
    append_seller,
    load_config,
    make_seller_key,
    pushplus_token,
)
from seller_monitor.models import MonitoredSeller
from seller_monitor.monitor import SellerMonitorService
from seller_monitor.notifier import PushPlusNotifier, write_preview
from seller_monitor.platforms import default_adapters, resolve_seller_input
from seller_monitor.repository import SellerMonitorRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="独立重点卖家监控")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--once", action="store_true", help="执行一轮检查")
    action.add_argument("--bootstrap", action="store_true", help="仅为未初始化卖家建立基线")
    action.add_argument("--status", action="store_true", help="查看最近运行状态，不创建数据库")
    action.add_argument("--check-config", action="store_true", help="离线检查配置，不访问平台")
    action.add_argument("--add-seller", metavar="URL_OR_SHARE_TEXT", help="从主页 URL 或分享文本添加卖家")
    action.add_argument("--preview-notification", action="store_true", help="只生成本地通知 HTML")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="监控 YAML 路径")
    parser.add_argument("--env", default=DEFAULT_ENV_PATH, help="独立环境变量文件路径")
    parser.add_argument("--preview-output", default="seller_monitor_notification_preview.html")
    return parser


def _configure_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("seller_monitor")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        logger.addHandler(handler)


def check_config(config_path: str) -> int:
    config = load_config(config_path)
    adapters = default_adapters()
    print(f"配置有效：{len(config.sellers)} 个卖家")
    for seller in config.sellers:
        capabilities = adapters[seller.platform].capabilities
        print(
            f"- {seller.seller_name}: {seller.platform} {seller.seller_url} "
            f"enabled={seller.enabled} capabilities={capabilities}"
        )
    return 0


def show_status(config_path: str) -> int:
    config_file = Path(config_path)
    if config_file.exists():
        config = load_config(config_file)
        state_path = config.state_path
        database_path = config.database_path
    else:
        state_path = config_file.resolve().parent / "seller_monitor_state.json"
        database_path = config_file.resolve().parent / "seller_monitor.db"
    state = None
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    latest = None
    if database_path.exists():
        latest = SellerMonitorRepository(database_path).latest_run()
    print(json.dumps({"state": state, "latest_database_run": latest}, ensure_ascii=False, indent=2))
    return 0


def add_seller_interactive(raw_input: str, config_path: str, *, input_func=input) -> int:
    adapter, seller_url, seller_id = resolve_seller_input(raw_input)
    suggested_name = seller_id or seller_url.rstrip("/").rsplit("/", 1)[-1]
    seller_name = input_func(f"卖家名称 [{suggested_name}]: ").strip() or suggested_name
    seller = MonitoredSeller(
        seller_key=make_seller_key(adapter.platform, seller_id, seller_url),
        seller_id=seller_id,
        seller_identity_source="url_native_id" if seller_id else "canonical_url",
        seller_name=seller_name,
        platform=adapter.platform,
        seller_url=seller_url,
        enabled=True,
    )
    proposed = {
        "seller_key": seller.seller_key,
        "seller_id": seller.seller_id,
        "seller_identity_source": seller.seller_identity_source,
        "seller_name": seller.seller_name,
        "platform": seller.platform,
        "seller_url": seller.seller_url,
        "enabled": True,
    }
    print("准备写入：")
    print(yaml.safe_dump(proposed, allow_unicode=True, sort_keys=False).rstrip())
    if input_func("确认写入 seller_monitor.yaml？[y/N]: ").strip().lower() not in {"y", "yes"}:
        print("已取消，未修改配置。")
        return 1
    append_seller(config_path, seller)
    print("已写入配置。下一次 --bootstrap 会建立基线，不推送历史商品。")
    return 0


def run_monitor(config_path: str, env_path: str, mode: str) -> int:
    config = load_config(config_path)
    _configure_logging(config.log_path)
    repository = SellerMonitorRepository(config.database_path)
    token = pushplus_token(env_path)
    notifier = PushPlusNotifier(token) if token else None
    service = SellerMonitorService(repository, default_adapters(), notifier)
    summary = service.run(config, mode=mode)
    print(json.dumps(summary.__dict__, ensure_ascii=False, indent=2))
    return 1 if summary.status == "failed" else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.check_config:
            return check_config(args.config)
        if args.status:
            return show_status(args.config)
        if args.add_seller is not None:
            return add_seller_interactive(args.add_seller, args.config)
        if args.preview_notification:
            path = write_preview(args.preview_output)
            print(f"通知预览已生成（未发送）：{path.resolve()}")
            return 0
        return run_monitor(args.config, args.env, "bootstrap" if args.bootstrap else "once")
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        print(f"错误：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

