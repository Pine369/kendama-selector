"""
跨境采销自动选品系统 - 主入口

定时扫描三个平台,经过 LLM 评估后,把候选商品推送到飞书自定义机器人。
每条推送是一张交互式卡片,带反馈按钮(买入/弃-太贵/弃-成色差),
点击会跳转到反馈端点(见 feedback_server.py),把决策写进 SQLite。

注意:飞书对卡片 button 的 url 字段有 HTTPS 要求。
如果 FEEDBACK_URL 是 http,飞书会静默丢弃整个 action 元素(按钮看不见)。
"""
import os
import time
import hashlib
import logging
from datetime import datetime
from urllib.parse import urlencode

import requests
import schedule
import yaml
from dotenv import load_dotenv

from scraper import scrape_multiple_keywords
from ai_filter import evaluate_with_ai, generate_daily_summary, JPY_TO_CNY

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


# ----------------------------------------
# 工具
# ----------------------------------------
def item_id(item):
    """用商品 URL 的 hash 当唯一 id,比传日文标题进 URL 更稳。"""
    url = item.get("url", "")
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:12]


def feedback_link(item, action, reason):
    """生成反馈点击链接。FEEDBACK_URL 没配就返回空,卡片自动降级到无按钮。"""
    if not FEEDBACK_URL:
        return None
    params = urlencode({
        "id": item_id(item),
        "action": action,
        "reason": reason,
        "url": item.get("url", ""),
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
    else:
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
    post_to_feishu(build_summary_card(items), label="daily_summary")


# ----------------------------------------
# 业务流程
# ----------------------------------------
def filter_by_brand_whitelist(items):
    return [
        item for item in items
        if any(b in item["title"].lower() for b in VALID_BRANDS)
    ]


def run_scan():
    logger.info("开始新一轮扫描")
    raw_items = scrape_multiple_keywords(BROAD_KEYWORDS, max_items_per_platform=MAX_ITEMS)

    if not raw_items:
        logger.warning("爬虫未返回任何数据")
        return

    total = len(raw_items)
    candidates = filter_by_brand_whitelist(raw_items)
    logger.info(f"抓取 {total} 条,品牌白名单匹配 {len(candidates)} 条")

    if not candidates:
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

    push_summary(items)
    logger.info(f"每日汇总已推送,共 {len(items)} 条")

    if os.path.exists("daily_pool.json"):
        os.remove("daily_pool.json")


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