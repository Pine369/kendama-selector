"""
跨境采销自动选品系统 - 主入口

定时扫描三个平台,经过 LLM 评估后,把候选商品推送到飞书自定义机器人。
每条推送是一张交互式卡片,带反馈按钮(买入/弃-太贵/弃-成色差),
点击会跳转到反馈端点(见 feedback_server.py),把决策写进 SQLite。

注意:飞书对卡片 button 的 url 字段有 HTTPS 要求。
如果 FEEDBACK_URL 是 http,飞书会静默丢弃整个 action 元素(按钮看不见)。
"""
import os
import re
import json
import time
import hmac
import sqlite3
import hashlib
import logging
import argparse
from datetime import datetime
from urllib.parse import urlencode

import requests
import schedule
import yaml
from dotenv import load_dotenv

from scraper import scrape_multiple_keywords, PLATFORMS
from ai_filter import evaluate_with_ai, generate_daily_summary, JPY_TO_CNY, DAILY_POOL_FILE, is_valid_url
import scraper_health
import reporting
import db

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

load_dotenv()

# ----------------------------------------
# 配置
# ----------------------------------------
with open("config.yaml", "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

BROAD_KEYWORDS = CONFIG["search"]["broad_keywords"]
VALID_BRANDS = CONFIG["valid_brands"]
MAX_ITEMS = CONFIG["search"]["max_items_per_platform"]
SCAN_INTERVAL = CONFIG["schedule"]["scan_interval_minutes"]
SUMMARY_TIME = CONFIG["schedule"]["daily_summary_time"]

FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK")
FEEDBACK_URL = os.getenv("FEEDBACK_URL")  # 反馈接收端,需 HTTPS
FEEDBACK_SIGNING_SECRET = os.getenv("FEEDBACK_SIGNING_SECRET")  # 反馈链接签名密钥,需与 feedback_server.py 一致

SCRAPER_HEALTH_FILE = "scraper_health.json"
SCRAPER_HEALTH_THRESHOLD = 3

FEEDBACK_DB_FILE = "feedback.db"  # 旧反馈库,只读导入用,--migrate-feedback 之外不碰它
PRICE_DROP_MIN_JPY = 500
PRICE_DROP_MIN_PCT = 5


# ----------------------------------------
# 工具
# ----------------------------------------
def item_id(item):
    """用商品 URL 的 hash 当唯一 id,比传日文标题进 URL 更稳。"""
    url = item.get("url", "")
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:12]


def sign_feedback_params(item_id_value, action, reason):
    """对反馈参数做 HMAC-SHA256 签名,防止反馈链接被伪造篡改。"""
    payload = "\x1f".join([item_id_value, action, reason])
    return hmac.new(
        FEEDBACK_SIGNING_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def feedback_link(item, action, reason):
    """生成反馈点击链接。FEEDBACK_URL 或签名密钥没配就返回空,卡片自动降级到无按钮,
    避免生成可写入数据库的未签名链接。"""
    if not FEEDBACK_URL or not is_configured(FEEDBACK_SIGNING_SECRET):
        return None
    iid = item_id(item)
    params = urlencode({
        "id": iid,
        "action": action,
        "reason": reason,
        "url": item.get("url", ""),
        "sig": sign_feedback_params(iid, action, reason),
    })
    return f"{FEEDBACK_URL}?{params}"


def is_configured(value):
    """判断环境变量是否配置过(非空且非占位符)。"""
    if not value:
        return False
    placeholders = ("your_", "请填入", "fill_in", "xxxxx", "your.public.ip")
    return not any(p in value.lower() for p in placeholders)


def diagnose_feedback_config():
    """启动时打印反馈端点配置状态,辅助排查"按钮看不见"的问题。"""
    if not is_configured(FEEDBACK_URL):
        logger.warning("FEEDBACK_URL 未配置 → 卡片将不带反馈按钮")
        return
    if not is_configured(FEEDBACK_SIGNING_SECRET):
        logger.warning("FEEDBACK_SIGNING_SECRET 未配置 → 反馈功能未启用,卡片将不带反馈按钮")
        return
    logger.info(f"反馈端点已配置: {FEEDBACK_URL[:40]}...")


# ----------------------------------------
# 卡片构造
# ----------------------------------------
def _price_drop_annotation(item):
    """降价只是附加展示信号,不影响利润/标签规则。没有降价时返回空字符串。"""
    if not item.get("is_price_drop"):
        return ""
    return f"【降价 ¥{item.get('price_drop_jpy')} / {item.get('price_drop_pct')}%】"


def build_item_card(item):
    """单条商品的交互式卡片。"""
    profit = item.get("estimated_profit", "?")
    brand = item.get("brand", "未知商品")
    title = item.get("title", "未知")
    price_jpy = item.get("price_jpy", 0)
    price_cny = item.get("price_cny", round(price_jpy * JPY_TO_CNY, 2) if isinstance(price_jpy, (int, float)) else "?")
    total_cost = item.get("total_cost", "?")
    ref_price = item.get("domestic_ref_price", "?")
    taxed = item.get("taxed", False)
    tag = item.get("tag", "?")
    reason = item.get("reason", "")
    img_url = item.get("img_url", "")
    url = item.get("url", "")

    tax_note = "含 13% 税" if taxed else "免税"
    drop_note = _price_drop_annotation(item)
    drop_line = f"**{drop_note}**\n" if drop_note else ""

    info_md = (
        f"**原标题:** {title}\n"
        f"{drop_line}"
        f"**煤炉价:** ¥{price_jpy} 日元 ≈ ¥{price_cny} CNY(汇率 {JPY_TO_CNY})\n"
        f"**国内参考:** ¥{ref_price}\n"
        f"**总成本:** ¥{total_cost}({tax_note},含运费手续费 50)\n"
        f"**判定:** {tag}\n"
        f"**理由:** {reason}\n\n"
        f"[查看大图]({img_url})  ·  [前往煤炉]({url})"
    )

    elements = [{"tag": "markdown", "content": info_md}]

    # 反馈端点配置了才放按钮
    buy_link = feedback_link(item, "选的好", "符合预期")
    skip_expensive = feedback_link(item, "放弃", "价格倒挂")
    skip_quality = feedback_link(item, "放弃", "有瑕疵")

    if buy_link:
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "✅ 选的好"},
                    "type": "primary",
                    "url": buy_link,
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "❌ 贵了"},
                    "type": "danger",
                    "url": skip_expensive,
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "❌ 选的差"},
                    "type": "default",
                    "url": skip_quality,
                },
            ],
        })

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"💰 利润 ¥{profit} · {brand}",
                },
                "template": "blue",
            },
            "elements": elements,
        },
    }


def build_summary_card(items):
    """全天汇总卡片。用 Python 拼模板,不再走 LLM。"""
    today = datetime.now().strftime("%m-%d")
    lines = [f"全天累计 **{len(items)}** 条候选,按预估利润降序:\n"]
    for i, item in enumerate(items, 1):
        price_jpy = item.get("price_jpy", "?")
        price_cny = item.get("price_cny", "?")
        drop_note = _price_drop_annotation(item)
        header = f"**{i}. {item.get('brand', '未知')} · 利润 ¥{item.get('estimated_profit', '?')}**"
        if drop_note:
            header += f" {drop_note}"
        lines.append(
            f"{header}\n"
            f"{item.get('title', '')}\n"
            f"煤炉价 ¥{price_jpy} 日元 ≈ ¥{price_cny} CNY · {item.get('tag', '')}\n"
            f"{item.get('reason', '')}\n"
            f"[查看大图]({item.get('img_url', '')})  ·  [前往煤炉]({item.get('url', '')})\n"
        )
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📊 选品全天汇总 ({today})"},
                "template": "purple",
            },
            "elements": [{"tag": "markdown", "content": "\n".join(lines)}],
        },
    }


# ----------------------------------------
# 推送
# ----------------------------------------
def post_to_feishu(payload, label=""):
    """统一的飞书发送函数,带 timeout 和错误处理。"""
    if not is_configured(FEISHU_WEBHOOK):
        logger.warning("飞书 Webhook 未配置,跳过推送")
        return False
    try:
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        data = resp.json()
        if data.get("code") != 0:
            logger.error(f"飞书推送失败 [{label}]: {data}")
            return False
        return True
    except requests.Timeout:
        logger.error(f"飞书推送超时 [{label}]")
    except requests.RequestException as e:
        logger.error(f"飞书网络异常 [{label}]: {e}")
    except ValueError as e:
        logger.error(f"飞书响应非 JSON [{label}]: {e}")
    return False


def push_items(items):
    """逐条推送候选商品。0.5s 间隔避开飞书 webhook 频率限制。"""
    for item in items:
        ok = post_to_feishu(build_item_card(item), label=item.get("brand", ""))
        if ok:
            logger.info(f"已推送: {item.get('brand', '')} · ¥{item.get('estimated_profit', '?')}")
        time.sleep(0.5)


def push_summary(items):
    return post_to_feishu(build_summary_card(items), label="daily_summary")


# ----------------------------------------
# 业务流程
# ----------------------------------------
def filter_by_brand_whitelist(items):
    """品牌白名单匹配,大小写无关(不修改 VALID_BRANDS 本身)。"""
    brands_lower = [b.lower() for b in VALID_BRANDS]
    return [
        item for item in items
        if any(b in item["title"].lower() for b in brands_lower)
    ]


def _parse_price_jpy(price_str):
    """从抓取到的价格字符串(如 '¥18,000')中提取数字,解析失败返回 None。"""
    if not price_str:
        return None
    digits = re.sub(r"[^\d]", "", str(price_str))
    return int(digits) if digits else None


def _load_seen_prices(pool_file):
    """读取当日候选池,返回 {url: price_jpy},用于跳过价格未变化的重复评估。"""
    if not os.path.exists(pool_file):
        return {}
    try:
        with open(pool_file, "r", encoding="utf-8") as f:
            pool = json.load(f)
    except Exception as e:
        logger.warning(f"读取候选池失败,本轮不做去重: {e}")
        return {}

    seen = {}
    for entry in pool:
        url = entry.get("url")
        price_jpy = entry.get("price_jpy")
        if url and isinstance(price_jpy, (int, float)):
            seen[url] = price_jpy
    return seen


def filter_already_evaluated(items, seen_prices):
    """跳过'同 URL 且价格未变化'的商品,减少重复 LLM 调用。价格变化或新 URL 都保留。
    返回 (fresh, skipped_count);调用方把 skipped_count 汇总进 LLM 前的统一日志,
    这里不再单独打印,避免和 _clean_llm_candidates() 的汇总日志重复刷屏。"""
    fresh = []
    skipped = 0
    for item in items:
        url = item.get("url")
        seen_price = seen_prices.get(url) if url else None
        if seen_price is not None:
            current_price = _parse_price_jpy(item.get("price"))
            if current_price is not None and current_price == seen_price:
                skipped += 1
                continue
        fresh.append(item)

    return fresh, skipped


def _clean_llm_candidates(candidates):
    """品牌白名单命中之后、送 LLM 之前的清洗:
    - 剔除无效 URL(复用 ai_filter.is_valid_url,标准与利润阶段一致);
    - 同一轮内同 URL 只保留抓取顺序中的第一条,结果可复现;
    - 同 URL 但价格不同也不覆盖、不静默丢弃,只计入 price_conflict_count 供日志展示;
    - 标题相同但 URL 不同的候选视为不同商品,不受影响(去重键只按 URL,不看标题)。
    返回 (cleaned, stats);这里不逐条打印日志,只在调用方拼一条汇总日志。"""
    cleaned = []
    seen_price_by_url = {}
    invalid_url_count = 0
    duplicate_url_count = 0
    price_conflict_count = 0

    for item in candidates:
        url = item.get("url")
        if not is_valid_url(url):
            invalid_url_count += 1
            continue

        if url in seen_price_by_url:
            duplicate_url_count += 1
            current_price = _parse_price_jpy(item.get("price"))
            if current_price is not None and current_price != seen_price_by_url[url]:
                price_conflict_count += 1
            continue

        seen_price_by_url[url] = _parse_price_jpy(item.get("price"))
        cleaned.append(item)

    stats = {
        "invalid_url_count": invalid_url_count,
        "duplicate_url_count": duplicate_url_count,
        "price_conflict_count": price_conflict_count,
    }
    return cleaned, stats


def _derive_platform(item):
    """从 scraper.py 写入的 category 字段(格式'[平台] 关键词')解析出平台名。"""
    category = item.get("category", "")
    for platform in PLATFORMS:
        if category.startswith(f"[{platform}]"):
            return platform
    return "unknown"


def _count_items_by_platform(raw_items):
    """统计各平台本轮抓取数量。"""
    counts = {platform: 0 for platform in PLATFORMS}
    for item in raw_items:
        platform = _derive_platform(item)
        if platform in counts:
            counts[platform] += 1
    return counts


# ----------------------------------------
# SQLite 影子写入 (Phase 1,不改变 daily_pool.json / 飞书 / 反馈的任何可见行为)
# ----------------------------------------
def _detect_price_drop(listing_id, scan_run_id, current_price_jpy):
    """查询该 listing 在更早 scan_run 中最近一条价格,判断本轮是否构成降价信号。
    降价定义: 降价金额 >= 500 日元 或 降价比例 >= 5%(且当前价格确实低于上一次价格)。
    只是附加信号,不影响利润计算/标签规则本身。
    查不到历史价格(首次发现)、listing_id/价格缺失、数据库异常时,is_price_drop=False。"""
    result = {
        "previous_price_jpy": None,
        "price_drop_jpy": None,
        "price_drop_pct": None,
        "is_price_drop": False,
    }
    if listing_id is None or current_price_jpy is None:
        return result

    previous_price = db.get_previous_price(listing_id, scan_run_id)
    if previous_price is None or previous_price <= 0:
        return result

    result["previous_price_jpy"] = previous_price
    if current_price_jpy >= previous_price:
        return result

    drop_jpy = previous_price - current_price_jpy
    drop_pct = round(drop_jpy / previous_price * 100, 1)
    result["price_drop_jpy"] = drop_jpy
    result["price_drop_pct"] = drop_pct
    result["is_price_drop"] = drop_jpy >= PRICE_DROP_MIN_JPY or drop_pct >= PRICE_DROP_MIN_PCT
    return result


def _record_listings_and_prices(candidates, scan_run_id):
    """品牌白名单命中且 URL 有效的候选:upsert listing + 写一条价格快照 + 附加价格变动信号。
    返回 {url: listing_id},供后续写 evaluations 时查找。
    任何一步失败都只记日志(db.py 内部已吞掉异常),不影响 candidates 本身。"""
    url_to_listing_id = {}
    if scan_run_id is None:
        return url_to_listing_id

    now = datetime.now().isoformat(timespec="seconds")
    for item in candidates:
        url = item.get("url")
        if not is_valid_url(url):
            continue

        listing_id = db.get_or_create_listing(
            platform=_derive_platform(item),
            url=url,
            legacy_url_hash=item_id(item),
            title=item.get("title", ""),
            img_url=item.get("img_url", ""),
            observed_at=now,
        )
        if listing_id is None:
            continue
        url_to_listing_id[url] = listing_id

        price_jpy = _parse_price_jpy(item.get("price"))

        # 价格变动识别:必须在这一轮价格写入 price_history 之前查询"更早"的历史价格,
        # 但即使写入顺序反过来也没关系——查询本身已按 scan_run_id != 当前轮 过滤。
        drop_info = _detect_price_drop(listing_id, scan_run_id, price_jpy)
        item["previous_price_jpy"] = drop_info["previous_price_jpy"]
        item["price_drop_jpy"] = drop_info["price_drop_jpy"]
        item["price_drop_pct"] = drop_info["price_drop_pct"]
        item["is_price_drop"] = drop_info["is_price_drop"]

        if price_jpy is not None:
            db.record_price(
                listing_id=listing_id,
                scan_run_id=scan_run_id,
                price_jpy=price_jpy,
                raw_price_text=item.get("price", ""),
                observed_at=now,
            )
    return url_to_listing_id


def _record_evaluations(all_evaluated_items, top_items, url_to_listing_id, scan_run_id):
    """把 evaluate_with_ai() 返回的全部已评估候选写进 evaluations,
    包含利润 >= 0 的(tag 为强推/推荐/观望/盲盒)和利润 < 0 被标记为"跳过"的商品。
    selected_for_push 只表示该条是否进入本轮 top_items,不代表飞书一定推送成功。
    找不到对应 listing(理论上不应发生,防御性处理)或写库失败都只记日志。"""
    if scan_run_id is None:
        return

    pushed_urls = {item.get("url") for item in top_items}
    now = datetime.now().isoformat(timespec="seconds")
    for item in all_evaluated_items:
        listing_id = url_to_listing_id.get(item.get("url"))
        if listing_id is None:
            logger.warning(f"评估结果找不到对应 listing,跳过影子写入: {item.get('url')}")
            continue

        db.record_evaluation(
            listing_id=listing_id,
            scan_run_id=scan_run_id,
            price_jpy=item.get("price_jpy"),
            domestic_ref_price=item.get("domestic_ref_price"),
            is_gold_mine=item.get("is_gold_mine", False),
            reason=item.get("reason"),
            estimated_profit=item.get("estimated_profit"),
            total_cost=item.get("total_cost"),
            taxed=item.get("taxed", False),
            tag=item.get("tag"),
            selected_for_push=item.get("url") in pushed_urls,
            evaluated_at=now,
            brand=item.get("brand"),
            source_category=item.get("category"),
            previous_price_jpy=item.get("previous_price_jpy"),
            price_drop_jpy=item.get("price_drop_jpy"),
            price_drop_pct=item.get("price_drop_pct"),
            is_price_drop=item.get("is_price_drop", False),
        )


def check_scraper_health(raw_items):
    """连续 3 轮某平台抓取为 0 时告警一次,直到该平台恢复(抓到 >0 条)才允许再次告警。"""
    counts = _count_items_by_platform(raw_items)
    state = scraper_health.load_health_state(SCRAPER_HEALTH_FILE)
    state, to_alert = scraper_health.update_platform_counts(
        state, counts, threshold=SCRAPER_HEALTH_THRESHOLD
    )
    scraper_health.save_health_state(SCRAPER_HEALTH_FILE, state)

    for platform in to_alert:
        consecutive = state[platform]["consecutive_zero"]
        message = scraper_health.format_alert_message(platform, consecutive)
        logger.error(f"抓取健康告警: {message}")
        post_to_feishu(
            {
                "msg_type": "interactive",
                "card": {
                    "header": {
                        "title": {"tag": "plain_text", "content": f"⚠️ 抓取疑似异常: {platform}"},
                        "template": "red",
                    },
                    "elements": [{"tag": "markdown", "content": message}],
                },
            },
            label=f"scraper_health_{platform}",
        )


def run_scan(keywords=None, platforms=None, max_items_per_platform=None):
    """keywords/platforms/max_items_per_platform 均为 None 时,行为与不带参数完全一致
    (使用 config.yaml 的 BROAD_KEYWORDS/PLATFORMS/MAX_ITEMS)。仅供 --once 模式做范围收窄,
    定时模式永远不传这些参数。"""
    scope_overridden = (
        keywords is not None or platforms is not None or max_items_per_platform is not None
    )
    keywords = keywords if keywords is not None else BROAD_KEYWORDS
    max_items = max_items_per_platform if max_items_per_platform is not None else MAX_ITEMS

    logger.info("开始新一轮扫描")
    if scope_overridden:
        logger.info(
            f"本轮范围覆盖: 平台={platforms or list(PLATFORMS)}, "
            f"关键词={keywords}, 每平台每关键词上限={max_items}"
        )
    scan_run_id = db.create_scan_run(datetime.now().isoformat(timespec="seconds"))

    try:
        raw_items = scrape_multiple_keywords(
            keywords, max_items_per_platform=max_items, platforms=platforms
        )

        check_scraper_health(raw_items)

        if not raw_items:
            logger.warning("爬虫未返回任何数据")
            db.finish_scan_run(
                scan_run_id, datetime.now().isoformat(timespec="seconds"), "ok",
                raw_item_count=0, brand_matched_count=0,
                llm_input_count=0, evaluated_count=0, candidate_count=0,
            )
            return

        total = len(raw_items)
        candidates = filter_by_brand_whitelist(raw_items)
        logger.info(f"抓取 {total} 条,品牌白名单匹配 {len(candidates)} 条")
        brand_matched_count = len(candidates)

        if not candidates:
            db.finish_scan_run(
                scan_run_id, datetime.now().isoformat(timespec="seconds"), "ok",
                raw_item_count=total, brand_matched_count=0,
                llm_input_count=0, evaluated_count=0, candidate_count=0,
            )
            return

        # LLM 前候选清洗:剔除无效 URL、同轮内同 URL 去重(保留第一条,价格冲突只计数不静默覆盖)
        cleaned_candidates, clean_stats = _clean_llm_candidates(candidates)

        # 只对清洗后的候选写 listings/price_history:天然保证"只有有效 URL 才写、
        # 本轮重复 URL 只写一次",不需要额外判断
        url_to_listing_id = _record_listings_and_prices(cleaned_candidates, scan_run_id)

        seen_prices = _load_seen_prices(DAILY_POOL_FILE)
        candidates, history_skipped_count = filter_already_evaluated(cleaned_candidates, seen_prices)
        llm_input_count = len(candidates)

        logger.info(
            f"LLM 前候选清洗: 品牌命中 {brand_matched_count} 条 → "
            f"无效 URL 跳过 {clean_stats['invalid_url_count']} 条, "
            f"本轮重复 URL 跳过 {clean_stats['duplicate_url_count']} 条"
            f"(其中价格冲突 {clean_stats['price_conflict_count']} 条), "
            f"历史同价格跳过 {history_skipped_count} 条 → 送入 LLM {llm_input_count} 条"
        )

        if not candidates:
            logger.info("本轮候选均已评估过且价格未变化,跳过 LLM 调用")
            db.finish_scan_run(
                scan_run_id, datetime.now().isoformat(timespec="seconds"), "ok",
                raw_item_count=total, brand_matched_count=brand_matched_count,
                llm_input_count=0, evaluated_count=0, candidate_count=0,
            )
            return

        top_items, all_evaluated_items = evaluate_with_ai(candidates)

        _record_evaluations(all_evaluated_items, top_items, url_to_listing_id, scan_run_id)

        if not top_items:
            logger.info("本轮无符合条件的商品")
            db.finish_scan_run(
                scan_run_id, datetime.now().isoformat(timespec="seconds"), "ok",
                raw_item_count=total, brand_matched_count=brand_matched_count,
                llm_input_count=llm_input_count, evaluated_count=len(all_evaluated_items),
                candidate_count=0,
            )
            return

        logger.info(f"筛出 {len(top_items)} 条高潜商品,开始推送")
        push_items(top_items)

        db.finish_scan_run(
            scan_run_id, datetime.now().isoformat(timespec="seconds"), "ok",
            raw_item_count=total, brand_matched_count=brand_matched_count,
            llm_input_count=llm_input_count, evaluated_count=len(all_evaluated_items),
            candidate_count=len(top_items),
        )
    except Exception:
        db.finish_scan_run(scan_run_id, datetime.now().isoformat(timespec="seconds"), "error")
        raise


def run_daily_summary():
    logger.info("生成每日汇总")
    items = generate_daily_summary()
    if not items:
        logger.info("今日无精选数据,跳过汇总")
        return

    ok = push_summary(items)
    if not ok:
        logger.error("汇总推送失败,保留 daily_pool.json,等待后续重试")
        return

    logger.info(f"每日汇总已推送,共 {len(items)} 条")

    if os.path.exists(DAILY_POOL_FILE):
        os.remove(DAILY_POOL_FILE)


def safe_run(task_func, task_name):
    try:
        task_func()
    except Exception as e:
        logger.error(f"{task_name} 执行异常: {e}", exc_info=True)


def migrate_feedback_to_kendama_db():
    """把旧 feedback.db 的最后状态一次性导入 kendama.db.feedback_events。
    只读 feedback.db,不修改、不删除它。幂等:dedupe_key 基于旧表自身的 (id, ts),
    重复执行只会被 INSERT OR IGNORE 忽略,不会产生重复历史事件。
    返回 (imported, skipped)。"""
    if not os.path.exists(FEEDBACK_DB_FILE):
        logger.info(f"未找到 {FEEDBACK_DB_FILE},没有旧反馈可导入")
        return 0, 0

    conn = sqlite3.connect(FEEDBACK_DB_FILE)
    try:
        rows = conn.execute("SELECT id, url, action, reason, ts FROM feedback").fetchall()
    finally:
        conn.close()

    imported = 0
    skipped = 0
    for legacy_id, url, action, reason, ts in rows:
        listing_id = db.get_listing_id_by_url(url) if url else None
        dedupe_key = f"legacy:{legacy_id}:{ts}"
        inserted = db.record_feedback_event(
            listing_id=listing_id,
            legacy_item_id=legacy_id,
            url=url or "",
            action=action or "",
            reason=reason,
            source="legacy_import",
            dedupe_key=dedupe_key,
            created_at=ts or datetime.now().isoformat(timespec="seconds"),
        )
        if inserted:
            imported += 1
        else:
            skipped += 1

    logger.info(f"旧反馈导入完成: 共处理 {len(rows)} 条,新增 {imported} 条,已存在跳过 {skipped} 条")
    return imported, skipped


def _find_latest_weekly_review(reports_dir=None):
    """按文件修改时间(不是文件名)找最近一次生成的周报,兼容默认/非默认 --days 命名。
    reports/ 目录不存在或没有周报文件时返回 None。"""
    reports_dir = reports_dir or reporting.REPORTS_DIR
    if not os.path.isdir(reports_dir):
        return None
    candidates = [
        os.path.join(reports_dir, f) for f in os.listdir(reports_dir)
        if f.startswith("weekly_review_") and f.endswith(".md")
    ]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def print_status():
    """只读输出系统状态摘要。不初始化/不创建 kendama.db,不扫描、不调用 LLM、
    不推送飞书,不写 daily_pool.json/feedback.db。单个检查项查询失败只在该行标注
    失败原因,不中断其余检查项、不让命令崩溃。"""
    lines = ["# Kendama Sourcing Skill - 状态摘要", ""]

    db_path = db.DB_FILE
    db_exists = os.path.exists(db_path)
    lines.append(f"- kendama.db 是否存在: {'是' if db_exists else '否'}({db_path})")

    if not db_exists:
        lines.append("")
        lines.append(
            "数据库尚未初始化。运行 `python main.py --once`(或其他会写库的命令)"
            "完成首次初始化后再查看状态。"
        )
        print("\n".join(lines))
        return

    conn = None
    try:
        conn = db._connect_db(db_path)

        try:
            row = conn.execute(
                "SELECT id, started_at, finished_at, status, raw_item_count, "
                "brand_matched_count, llm_input_count, evaluated_count, candidate_count "
                "FROM scan_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                lines.append(
                    f"- 最新 scan_run: id={row[0]}, started_at={row[1]}, finished_at={row[2]}, "
                    f"status={row[3]}, raw={row[4]}, brand_matched={row[5]}, "
                    f"llm_input={row[6]}, evaluated={row[7]}, candidate={row[8]}"
                )
            else:
                lines.append("- 最新 scan_run: (暂无记录)")
        except Exception as e:
            lines.append(f"- 最新 scan_run: 查询失败({e})")

        for table in ("listings", "price_history", "evaluations", "feedback_events"):
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                lines.append(f"- {table} 总数: {count}")
            except Exception as e:
                lines.append(f"- {table} 总数: 查询失败({e})")

        try:
            tag_rows = conn.execute("SELECT tag, COUNT(*) FROM evaluations GROUP BY tag").fetchall()
            lines.append("- evaluations 按 tag 聚合:")
            if tag_rows:
                for tag, count in sorted(tag_rows, key=lambda x: -x[1]):
                    lines.append(f"  - {tag}: {count}")
            else:
                lines.append("  - (无数据)")
        except Exception as e:
            lines.append(f"- evaluations 按 tag 聚合: 查询失败({e})")

        try:
            selected = conn.execute(
                "SELECT COUNT(*) FROM evaluations WHERE selected_for_push=1"
            ).fetchone()[0]
            lines.append(f"- selected_for_push=1 总数: {selected}")
        except Exception as e:
            lines.append(f"- selected_for_push=1 总数: 查询失败({e})")

        try:
            drops = conn.execute(
                "SELECT COUNT(*) FROM evaluations WHERE is_price_drop=1"
            ).fetchone()[0]
            lines.append(f"- is_price_drop=1 总数: {drops}")
        except Exception as e:
            lines.append(f"- is_price_drop=1 总数: 查询失败({e})")

        try:
            fk_issues = conn.execute("PRAGMA foreign_key_check").fetchall()
            lines.append(f"- PRAGMA foreign_key_check: {'无异常' if not fk_issues else fk_issues}")
        except Exception as e:
            lines.append(f"- PRAGMA foreign_key_check: 查询失败({e})")
    except Exception as e:
        lines.append(f"- 数据库连接失败: {e}")
    finally:
        if conn is not None:
            conn.close()

    try:
        latest_report = _find_latest_weekly_review()
        lines.append(
            f"- 最近周报文件: {'存在(' + latest_report + ')' if latest_report else '不存在'}"
        )
    except Exception as e:
        lines.append(f"- 最近周报文件: 检查失败({e})")

    try:
        signals_exists = os.path.exists(reporting.SIGNALS_FILE)
        lines.append(f"- personalized_signals.md 是否存在: {'是' if signals_exists else '否'}")
    except Exception as e:
        lines.append(f"- personalized_signals.md 是否存在: 检查失败({e})")

    try:
        if os.path.exists(DAILY_POOL_FILE):
            with open(DAILY_POOL_FILE, "r", encoding="utf-8") as f:
                pool = json.load(f)
            urls = {item.get("url") for item in pool if isinstance(item, dict) and item.get("url")}
            lines.append(
                f"- daily_pool.json 是否存在: 是,候选 {len(pool)} 条,不同 URL {len(urls)} 个"
            )
        else:
            lines.append("- daily_pool.json 是否存在: 否")
    except Exception as e:
        lines.append(f"- daily_pool.json 是否存在: 是,但读取失败({e})")

    print("\n".join(lines))


# ----------------------------------------
# 入口
# ----------------------------------------
def parse_args(argv=None):
    """解析命令行参数。不带任何参数时返回的默认值必须让 main() 走原有的持续运行分支。"""
    parser = argparse.ArgumentParser(
        description="跨境采销自动选品系统。不带参数时持续运行(启动即扫描一次,"
                     "之后进入定时循环);加 --once 只完整扫描一轮后退出。"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="只执行一次完整扫描后退出,不注册定时任务、不进入循环",
    )
    parser.add_argument(
        "--platform", action="append", dest="platforms", default=None,
        metavar="PLATFORM",
        help=f"只扫描指定平台,可重复传入;不传则使用全部平台。可选值: {', '.join(PLATFORMS)}",
    )
    parser.add_argument(
        "--keyword", action="append", dest="keywords", default=None,
        metavar="KEYWORD",
        help="覆盖 config.yaml 里的 broad_keywords,可重复传入;不传则使用配置文件的值",
    )
    parser.add_argument(
        "--max-items", type=int, dest="max_items", default=None,
        metavar="N",
        help="覆盖本轮每个平台每个关键词的抓取上限;不传则使用 config.yaml 的值",
    )
    parser.add_argument(
        "--migrate-feedback", action="store_true",
        help="把旧 feedback.db 的最后状态导入 kendama.db.feedback_events 后退出;"
             "只初始化数据库、导入、打印数量,不扫描、不推送、不注册定时任务",
    )
    parser.add_argument(
        "--weekly-review", action="store_true",
        help="生成最近 N 天(默认 7 天)的复盘报告到 reports/ 目录后退出;"
             "不扫描、不调用 LLM、不推送飞书、不改写 feedback.db",
    )
    parser.add_argument(
        "--days", type=int, default=None, metavar="N",
        help="配合 --weekly-review 使用,统计最近 N 天;不传默认 7 天",
    )
    parser.add_argument(
        "--refresh-signals", action="store_true",
        help="从 kendama.db 生成 personalized_signals.md(仅供人工审核)后退出;"
             "不扫描、不调用 LLM、不推送飞书、不修改数据库",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="输出只读状态摘要(kendama.db 是否存在、最新 scan_run、各表总数等)后退出;"
             "不扫描、不调用 LLM、不推送飞书、不创建数据库",
    )

    args = parser.parse_args(argv)

    scope_args_used = (
        args.platforms is not None or args.keywords is not None or args.max_items is not None
    )
    if scope_args_used and not args.once:
        parser.error("--platform / --keyword / --max-items 必须和 --once 一起使用")

    maintenance_flags = {
        "--migrate-feedback": args.migrate_feedback,
        "--weekly-review": args.weekly_review,
        "--refresh-signals": args.refresh_signals,
        "--status": args.status,
    }
    active_maintenance = [name for name, used in maintenance_flags.items() if used]
    if len(active_maintenance) > 1:
        parser.error(f"{' / '.join(active_maintenance)} 不能同时使用")

    if active_maintenance and (args.once or scope_args_used):
        parser.error(
            f"{active_maintenance[0]} 不能与 --once / --platform / --keyword / --max-items 同时使用"
        )

    if args.days is not None and not args.weekly_review:
        parser.error("--days 必须和 --weekly-review 一起使用")

    if args.days is not None and args.days <= 0:
        parser.error("--days 必须是大于 0 的整数")

    if args.platforms is not None:
        invalid = [p for p in args.platforms if p not in PLATFORMS]
        if invalid:
            parser.error(f"未知平台: {', '.join(invalid)}(可选: {', '.join(PLATFORMS)})")

    if args.max_items is not None and args.max_items <= 0:
        parser.error("--max-items 必须是大于 0 的整数")

    return args


def main(argv=None):
    args = parse_args(argv)

    logger.info(f"系统启动 | 汇率 JPY→CNY={JPY_TO_CNY}")

    if args.status:
        # 只读检查,故意不调用 db.init_db(),避免"看一眼状态"就意外创建了数据库。
        print_status()
        return

    if args.weekly_review:
        if not db.init_db():
            logger.error("kendama.db 初始化失败,无法生成周报")
            return
        days = args.days or 7
        path, summary = reporting.generate_weekly_review(days=days)
        logger.info(f"核心摘要: {summary}")
        return

    if args.refresh_signals:
        if not db.init_db():
            logger.error("kendama.db 初始化失败,无法生成偏好信号")
            return
        path, summary = reporting.generate_signals_report()
        logger.info(f"核心摘要: {summary}")
        return

    if args.migrate_feedback:
        if not db.init_db():
            logger.error("kendama.db 初始化失败,无法执行反馈导入")
            return
        imported, skipped = migrate_feedback_to_kendama_db()
        logger.info(f"--migrate-feedback 完成: 新增 {imported} 条,跳过(已存在) {skipped} 条")
        return

    diagnose_feedback_config()
    if not db.init_db():
        logger.warning("kendama.db 初始化失败,本次运行将跳过 SQLite 影子写入")

    if args.once:
        logger.info("单轮模式(--once): 执行一次扫描后退出,不注册定时任务")
        safe_run(
            lambda: run_scan(
                keywords=args.keywords,
                platforms=args.platforms,
                max_items_per_platform=args.max_items,
            ),
            "扫描任务",
        )
        return

    safe_run(run_scan, "扫描任务")

    schedule.every(SCAN_INTERVAL).minutes.do(lambda: safe_run(run_scan, "扫描任务"))
    schedule.every().day.at(SUMMARY_TIME).do(lambda: safe_run(run_daily_summary, "每日汇总"))

    logger.info(f"进入定时模式: 每 {SCAN_INTERVAL} 分钟扫描一次,{SUMMARY_TIME} 输出汇总")

    while True:
        try:
            schedule.run_pending()
            time.sleep(60)
        except KeyboardInterrupt:
            logger.info("收到中断信号,程序退出")
            break
        except Exception as e:
            logger.error(f"调度异常: {e}", exc_info=True)
            time.sleep(60)


if __name__ == "__main__":
    main()