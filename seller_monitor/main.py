"""Command-line entrypoint for the independent seller monitor."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

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
from seller_monitor.notifier import (
    PREVIEW_PAYLOAD,
    PREVIEW_TITLE,
    REAL_ITEM_TEST_DISCLAIMER,
    REAL_ITEM_TEST_TITLE,
    TEST_NOTIFICATION_DISCLAIMER,
    PushPlusNotifier,
    write_preview,
)
from seller_monitor.platforms import default_adapters, resolve_seller_input
from seller_monitor.repository import SellerMonitorRepository


_TOKEN_ASSIGNMENT_RE = re.compile(r"(PUSHPLUS_TOKEN\s*=\s*)[^\s]+", re.IGNORECASE)


def _redact_cli_secrets(text: str) -> str:
    return _TOKEN_ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)


def _stream_safe_text(text: str, stream) -> str:
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return text
    try:
        text.encode(encoding)
        return text
    except UnicodeEncodeError:
        fallback = text.replace("¥", "JPY ")
        return fallback.encode(encoding, errors="backslashreplace").decode(encoding)
    except LookupError:
        return text.replace("¥", "JPY ")


def safe_print(*values, sep: str = " ", end: str = "\n", file=None, flush: bool = False) -> None:
    stream = file if file is not None else sys.stdout
    text = _redact_cli_secrets(sep.join(str(value) for value in values) + end)
    safe_text = _stream_safe_text(text, stream)
    try:
        stream.write(safe_text)
    except UnicodeEncodeError:
        fallback = safe_text.replace("¥", "JPY ")
        try:
            stream.write(fallback)
        except UnicodeEncodeError:
            stream.write(fallback.encode("ascii", errors="backslashreplace").decode("ascii"))
    if flush:
        stream.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="独立重点卖家监控")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--once", action="store_true", help="执行一轮检查")
    action.add_argument("--bootstrap", action="store_true", help="仅为未初始化卖家建立基线")
    action.add_argument("--status", action="store_true", help="查看最近运行状态，不创建数据库")
    action.add_argument("--check-config", action="store_true", help="离线检查配置，不访问平台")
    action.add_argument("--add-seller", metavar="URL_OR_SHARE_TEXT", help="从主页 URL 或分享文本添加卖家")
    action.add_argument("--preview-notification", action="store_true", help="只生成本地通知 HTML")
    action.add_argument("--test-notification", action="store_true", help="确认后发送一条合成 PushPlus 测试消息")
    action.add_argument(
        "--test-notification-from-seller",
        action="store_true",
        help="确认后用唯一 Mercari 卖家的首件有效在售商品发送展示测试",
    )
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
    safe_print(f"配置有效：{len(config.sellers)} 个卖家")
    for seller in config.sellers:
        capabilities = adapters[seller.platform].capabilities
        safe_print(
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
    safe_print(json.dumps({"state": state, "latest_database_run": latest}, ensure_ascii=False, indent=2))
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
    safe_print("准备写入：")
    safe_print(yaml.safe_dump(proposed, allow_unicode=True, sort_keys=False).rstrip())
    if input_func("确认写入 seller_monitor.yaml？[y/N]: ").strip().lower() not in {"y", "yes"}:
        safe_print("已取消，未修改配置。")
        return 1
    append_seller(config_path, seller)
    safe_print("已写入配置。下一次 --bootstrap 会建立基线，不推送历史商品。")
    return 0


def run_monitor(config_path: str, env_path: str, mode: str) -> int:
    config = load_config(config_path)
    _configure_logging(config.log_path)
    repository = SellerMonitorRepository(config.database_path)
    token = pushplus_token(env_path)
    notifier = PushPlusNotifier(token) if token else None
    service = SellerMonitorService(repository, default_adapters(), notifier)
    summary = service.run(config, mode=mode)
    safe_print(json.dumps(summary.__dict__, ensure_ascii=False, indent=2))
    return 1 if summary.status == "failed" else 0


_NOTIFICATION_STATUS_MESSAGES = {
    "accepted": "accepted（PushPlus 已接受，不代表微信已送达）",
    "rejected": "rejected（PushPlus 明确拒绝）",
    "delivery_unknown": "delivery_unknown（结果未知，不会自动重试）",
    "retryable_failure": "retryable_failure（连接前失败，本命令不会自动重试）",
}


def _report_test_notification_result(result, output_func) -> int:
    output_func(f"测试通知结果：{_NOTIFICATION_STATUS_MESSAGES.get(result.status, result.status)}")
    return 0 if result.status == "accepted" else 1


def send_test_notification_interactive(
    env_path: str,
    *,
    input_func=input,
    output_func=safe_print,
) -> int:
    env_file = Path(env_path)
    if not env_file.is_file():
        output_func(f"错误：独立环境文件不存在：{env_file}")
        return 2
    token = pushplus_token(env_file)
    if not token:
        output_func("错误：seller_monitor.env 中未配置非空 PUSHPLUS_TOKEN。")
        return 2

    payload = dict(PREVIEW_PAYLOAD)
    output_func("PUSHPLUS_TOKEN 已配置（值不会显示）。")
    output_func("即将发送以下测试消息：")
    output_func(PREVIEW_TITLE)
    output_func("事件：新上架")
    output_func("平台：Mercari")
    output_func(f"卖家：{payload['seller_name']}")
    output_func("类型：待确认")
    output_func(f"商品：{payload['title']}")
    output_func(f"价格：¥{payload['new_price']:,}")
    output_func(f"商品图片：{payload['image_url']}")
    output_func(f"商品链接：{payload['item_url']}")
    output_func(f"检测时间：{payload['observed_at']}")
    output_func(TEST_NOTIFICATION_DISCLAIMER)
    if input_func("确认发送一条 PushPlus 测试消息？[y/N]: ").strip().lower() not in {"y", "yes"}:
        output_func("已取消，未发送测试消息。")
        return 1

    result = PushPlusNotifier(token).send_test_notification(payload)
    return _report_test_notification_result(result, output_func)


def _valid_http_url(value: str) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _select_display_test_snapshot(snapshots):
    for snapshot in snapshots:
        platform_status = (snapshot.raw or {}).get("platform_status")
        is_on_sale = snapshot.status == "on_sale" or (
            snapshot.status == "active" and platform_status == "on_sale"
        )
        valid_price = (
            isinstance(snapshot.current_price, int)
            and not isinstance(snapshot.current_price, bool)
            and snapshot.current_price > 0
        )
        if (
            is_on_sale
            and isinstance(snapshot.title, str)
            and snapshot.title.strip()
            and _valid_http_url(snapshot.image_url)
            and _valid_http_url(snapshot.item_url)
            and valid_price
        ):
            return snapshot
    return None


def send_test_notification_from_seller_interactive(
    config_path: str,
    env_path: str,
    *,
    adapters=None,
    notifier_factory=None,
    input_func=input,
    output_func=safe_print,
    now_func=None,
) -> int:
    config = load_config(config_path)
    enabled_sellers = [seller for seller in config.sellers if seller.enabled and not seller.deleted_at]
    if len(enabled_sellers) != 1:
        output_func("错误：必须恰好配置一个启用且未删除的卖家；未发送测试消息。")
        return 2
    seller = enabled_sellers[0]
    if seller.platform != "mercari":
        output_func("错误：唯一启用卖家不是 Mercari；未发送测试消息。")
        return 2

    env_file = Path(env_path)
    if not env_file.is_file():
        output_func(f"错误：独立环境文件不存在：{env_file}")
        return 2
    token = pushplus_token(env_file)
    if not token:
        output_func("错误：seller_monitor.env 中未配置非空 PUSHPLUS_TOKEN。")
        return 2

    available_adapters = adapters if adapters is not None else default_adapters()
    adapter = available_adapters.get("mercari")
    if adapter is None:
        output_func("错误：Mercari adapter 不可用；未发送测试消息。")
        return 2
    try:
        fetch_result = adapter.fetch_seller(seller)
    except Exception:
        output_func("错误：Mercari 卖家商品获取失败；未发送测试消息。")
        return 1

    diagnostics = getattr(adapter, "last_diagnostics", None)
    has_next = getattr(fetch_result, "has_next", None)
    if has_next is None:
        has_next = getattr(diagnostics, "has_next", None)
    if has_next is True:
        output_func("错误：Mercari 在售列表仍有下一页；未发送测试消息。")
        return 1
    if not fetch_result.complete:
        output_func("错误：Mercari FetchResult.complete=False；未发送测试消息。")
        return 1
    if has_next is not False:
        output_func("错误：无法确认 Mercari 在售列表已到最后一页；未发送测试消息。")
        return 1

    snapshot = _select_display_test_snapshot(fetch_result.snapshots)
    if snapshot is None:
        output_func("错误：没有同时具备在售状态、图片、标题、价格和链接的商品；未发送。")
        return 1

    observed_at = snapshot.observed_at
    if not observed_at:
        current_time = now_func() if now_func is not None else datetime.now().astimezone()
        observed_at = current_time.isoformat(timespec="seconds")
    payload = {
        "event_type": "真实商品展示测试",
        "platform": "mercari",
        "seller_name": seller.seller_name,
        "listing_type": snapshot.listing_type,
        "title": snapshot.title,
        "image_url": snapshot.image_url,
        "item_url": snapshot.item_url,
        "old_price": None,
        "new_price": snapshot.current_price,
        "observed_at": observed_at,
    }
    image_domain = urlsplit(snapshot.image_url).hostname or "(未知域名)"
    output_func("PUSHPLUS_TOKEN 已配置（值不会显示）。")
    output_func("即将发送以下真实商品展示测试：")
    output_func(REAL_ITEM_TEST_TITLE)
    output_func(f"卖家：{seller.seller_name}")
    output_func(f"商品：{snapshot.title}")
    output_func(f"价格：¥{snapshot.current_price:,}")
    output_func(f"图片域名：{image_domain}")
    output_func(f"商品链接：{snapshot.item_url}")
    output_func("数据库：不会读取或写入正式事件及商品表。")
    output_func(REAL_ITEM_TEST_DISCLAIMER)
    if input_func("确认发送一条真实商品展示测试消息？[y/N]: ").strip().lower() not in {"y", "yes"}:
        output_func("已取消，未发送测试消息。")
        return 1

    factory = notifier_factory if notifier_factory is not None else PushPlusNotifier
    result = factory(token).send_test_notification(
        payload,
        title=REAL_ITEM_TEST_TITLE,
        disclaimer=REAL_ITEM_TEST_DISCLAIMER,
    )
    return _report_test_notification_result(result, output_func)


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
            safe_print(f"通知预览已生成（未发送）：{path.resolve()}")
            return 0
        if args.test_notification:
            return send_test_notification_interactive(args.env, input_func=input, output_func=safe_print)
        if args.test_notification_from_seller:
            return send_test_notification_from_seller_interactive(
                args.config,
                args.env,
                input_func=input,
                output_func=safe_print,
            )
        return run_monitor(args.config, args.env, "bootstrap" if args.bootstrap else "once")
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        safe_print(f"错误：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
