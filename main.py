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
import hashlib
import logging
from datetime import datetime
from urllib.parse import urlencode

import requests
import schedule
import yaml
from dotenv import load_dotenv

from scraper import scrape_multiple_keywords, PLATFORMS
from ai_filter import evaluate_with_ai, generate_daily_summary, JPY_TO_CNY, DAILY_POOL_FILE
import scraper_health

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

    info_md = (
        f"**原标题:** {title}\n"
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
        lines.append(
            f"**{i}. {item.get('brand', '未知')} · 利润 ¥{item.get('estimated_profit', '?')}**\n"
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
    """跳过'同 URL 且价格未变化'的商品,减少重复 LLM 调用。价格变化或新 URL 都保留。"""
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

    if skipped:
        logger.info(f"跳过当日已评估且价格未变化的商品: {skipped} 条")
    return fresh


def _count_items_by_platform(raw_items):
    """按 scraper.py 写入的 category 字段(格式'[平台] 关键词')统计各平台本轮抓取数量。"""
    counts = {platform: 0 for platform in PLATFORMS}
    for item in raw_items:
        category = item.get("category", "")
        for platform in PLATFORMS:
            if category.startswith(f"[{platform}]"):
                counts[platform] += 1
                break
    return counts


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


def run_scan():
    logger.info("开始新一轮扫描")
    raw_items = scrape_multiple_keywords(BROAD_KEYWORDS, max_items_per_platform=MAX_ITEMS)

    check_scraper_health(raw_items)

    if not raw_items:
        logger.warning("爬虫未返回任何数据")
        return

    total = len(raw_items)
    candidates = filter_by_brand_whitelist(raw_items)
    logger.info(f"抓取 {total} 条,品牌白名单匹配 {len(candidates)} 条")

    if not candidates:
        return

    seen_prices = _load_seen_prices(DAILY_POOL_FILE)
    candidates = filter_already_evaluated(candidates, seen_prices)
    if not candidates:
        logger.info("本轮候选均已评估过且价格未变化,跳过 LLM 调用")
        return

    top_items = evaluate_with_ai(candidates)
    if not top_items:
        logger.info("本轮无符合条件的商品")
        return

    logger.info(f"筛出 {len(top_items)} 条高潜商品,开始推送")
    push_items(top_items)


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


# ----------------------------------------
# 入口
# ----------------------------------------
def main():
    logger.info(f"系统启动 | 汇率 JPY→CNY={JPY_TO_CNY}")
    diagnose_feedback_config()

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