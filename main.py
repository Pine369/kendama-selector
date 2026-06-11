"""
跨境采销自动选品系统 - 主入口

每 60 分钟扫描一次煤炉、雅虎拍卖、乐天三个平台,
通过本地预筛 + AI 评估,推送候选商品到微信。
每天 22:00 输出全天去重汇总。
"""

import os
import time
import requests
import schedule
import yaml  # 新增的库
import logging  # [必须新增] 日志模块，解决 NameError
from datetime import datetime
from dotenv import load_dotenv

from scraper import scrape_multiple_keywords
from ai_filter import evaluate_with_ai, generate_daily_summary

# [必须新增] 初始化专业日志配置
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s - %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

load_dotenv()
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN")

# ==========================================
# 加载外部配置 (动静分离)
# ==========================================
def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

CONFIG = load_config()
BROAD_KEYWORDS = CONFIG["search"]["broad_keywords"]
VALID_BRANDS = CONFIG["valid_brands"]
MAX_ITEMS = CONFIG["search"]["max_items_per_platform"]
SCAN_INTERVAL = CONFIG["schedule"]["scan_interval_minutes"]
SUMMARY_TIME = CONFIG["schedule"]["daily_summary_time"]

# ==========================================
# 微信推送
# ==========================================
def push_to_wechat(title, content):
    """通过 PushPlus 把报告推送到微信"""
    if not PUSHPLUS_TOKEN or "填入" in PUSHPLUS_TOKEN:
        logger.warning("PUSHPLUS_TOKEN 未配置,跳过推送")
        return

    try:
        response = requests.post(
            "http://www.pushplus.plus/send",
            json={
                "token": PUSHPLUS_TOKEN,
                "title": title,
                "content": content,
                "template": "markdown"
            },
            timeout=15
        ).json()

        if response.get("code") == 200:
            logger.info("推送成功")
        else:
            logger.error(f"推送被拒绝: {response.get('msg')}")
    except Exception as e:
        logger.error(f"推送请求异常: {e}")


# ==========================================
# 本地预筛
# ==========================================
def filter_by_brand_whitelist(items):
    """
    在送给 AI 前,先过滤掉标题中不包含任何目标品牌的商品。
    这是一道便宜的关卡,大幅降低 AI 调用成本。
    """
    filtered = []
    for item in items:
        title_lower = item['title'].lower()
        # [修正] 将没定义的 TARGET_BRANDS 改为配置读取出来的 VALID_BRANDS
        if any(brand in title_lower for brand in VALID_BRANDS):
            filtered.append(item)
    return filtered


# ==========================================
# 单次扫描任务
# ==========================================
def run_scan():
    """完整流程:抓取 → 预筛 → AI 评估 → 推送"""
    logger.info("开始新一轮扫描")

    # 1. 抓取 ( [修正] 替换为配置读取出来的 BROAD_KEYWORDS 和 MAX_ITEMS )
    raw_items = scrape_multiple_keywords(BROAD_KEYWORDS, max_items_per_platform=MAX_ITEMS)
    if not raw_items:
        logger.warning("爬虫未返回任何数据")
        return

    total = len(raw_items)
    logger.info(f"抓取完成,共 {total} 条,开始本地预筛")

    # 2. 本地预筛
    candidates = filter_by_brand_whitelist(raw_items)
    logger.info(f"预筛结果: {len(candidates)}/{total} 条匹配品牌白名单")

    if not candidates:
        logger.info("无匹配品牌的商品,跳过 AI 评估")
        return

    # 3. AI 评估
    report = evaluate_with_ai(candidates)

    # 4. 推送
    if report and report.strip() and "无符合规则" not in report and "未抓到" not in report:
        time_str = datetime.now().strftime("%H:%M")
        push_to_wechat(f"选品扫描报告 ({time_str})", report)
    else:
        logger.info("本轮无符合条件的商品,跳过推送")


def run_daily_summary():
    """每日 22:00 触发,输出全天去重汇总"""
    logger.info("开始生成每日汇总")
    report = generate_daily_summary()

    if report and "无任何符合" not in report and "汇总生成失败" not in report:
        time_str = datetime.now().strftime("%m-%d")
        push_to_wechat(f"全天选品汇总 ({time_str})", report)
        logger.info("每日汇总已推送")

        # 清空当天数据,准备明天
        if os.path.exists("daily_pool.json"):
            os.remove("daily_pool.json")
    else:
        logger.info("今日无精选数据,跳过汇总")


# ==========================================
# 异常隔离包装
# ==========================================
def safe_run(task_func, task_name):
    """任何任务失败都不应该让定时器停止,统一捕获异常"""
    try:
        task_func()
    except Exception as e:
        logger.error(f"{task_name} 执行异常: {e}", exc_info=True)


# ==========================================
# 入口
# ==========================================
def main():
    logger.info("系统启动,执行首次扫描")
    safe_run(run_scan, "扫描任务")

    # 定时任务 [修正] 修复了调用不存在的函数，使用 lambda 传参给 safe_run
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