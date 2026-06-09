"""
AI 评估模块

两阶段评估:
1. 分批送 AI,产出 JSON 候选列表(机器读)
2. 汇总候选,生成精简推送报告(人读)

API 策略:DeepSeek 官方(主)→ 硅基流动(备),temperature=0。
"""

import os
import json
import time
import logging
from urllib.parse import quote

import httpx
from openai import OpenAI
from dotenv import load_dotenv


# ==========================================
# 配置
# ==========================================
load_dotenv()
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
SILICONFLOW_KEY = os.getenv("SILICONFLOW_API_KEY")

# 禁用系统代理,避免 VPN/Clash 等工具干扰 API 请求
http_client = httpx.Client(proxy=None)

primary_client = OpenAI(
    api_key=DEEPSEEK_KEY,
    base_url="https://api.deepseek.com/v1",
    timeout=120.0,
    http_client=http_client
)

backup_client = OpenAI(
    api_key=SILICONFLOW_KEY,
    base_url="https://api.siliconflow.cn/v1",
    timeout=120.0,
    http_client=http_client
)

DAILY_POOL_FILE = "daily_pool.json"
IMAGE_PROXY_HOST = "https://wsrv.nl/"

logger = logging.getLogger(__name__)


# ==========================================
# 工具函数
# ==========================================
def _load_file(filename, default_text):
    """读取项目目录下的文本文件"""
    path = os.path.join(os.path.dirname(__file__), filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        logger.info(f"已加载 {filename}: {len(content)} 字符")
        return content
    except FileNotFoundError:
        logger.warning(f"未找到 {filename},使用默认值")
        return default_text


def proxy_image_url(url):
    """
    通过 wsrv.nl 代理图片 URL,绕过微信内置浏览器的防盗链。
    对 URL 做完整编码,避免查询参数被 wsrv.nl 误解析。
    """
    if not url:
        return ""
    return f"{IMAGE_PROXY_HOST}?url={quote(url, safe='')}"


def ensure_proxied(url):
    """已代理的 URL 直接返回,未代理的做转换"""
    if not url or url.startswith(IMAGE_PROXY_HOST):
        return url
    return proxy_image_url(url)


RULES = _load_file("rules.md", "请使用通用商业知识进行判断。")
CASES = _load_file("cases.md", "暂无实战案例。")


# ==========================================
# AI 调用
# ==========================================
def call_ai(messages):
    """调用 AI,主 API 失败时切换备用"""
    try:
        response = primary_client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            temperature=0
        )
        return response.choices[0].message.content
    except Exception as primary_err:
        logger.warning(f"主 API 失败,切换备用: {primary_err}")
        try:
            response = backup_client.chat.completions.create(
                model="deepseek-ai/DeepSeek-V3",
                messages=messages,
                temperature=0
            )
            return response.choices[0].message.content
        except Exception as backup_err:
            raise RuntimeError(
                f"主备 API 均失败。主: {primary_err} | 备: {backup_err}"
            )


# ==========================================
# Prompt 模板
# ==========================================
BATCH_FILTER_PROMPT = f"""
你是跨境采销选品助手。任务是从一批商品中筛出值得人工复核的候选。

# 规则书
{RULES}

# 实战案例
{CASES}

# 筛选要求
1. 严格按规则书和实战案例判断,不要凭主观感觉。
2. 命中"踩坑案例"特征的商品,直接淘汰。
3. 命中"金矿案例"特征的商品,标记为"强推"。
4. 命中"绝对跳过"条件的商品,直接淘汰。
5. 标题信息不足但煤炉价 < 3000 日元的商品,标记为"盲盒"保留。
6. 预估利润低于 20 元的商品,淘汰。
7. 利润计算严格按成本公式执行: 总成本 = 煤炉价 × 0.045 + 80 + 30。
   预估利润 ≤ 0 时,无论品牌或稀缺性如何,直接淘汰。


# 输出格式
仅输出 JSON 数组,不要额外说明,不要 Markdown 代码块包裹。

每个元素结构:
[
  {{
    "title": "原始商品标题",
    "brand": "识别出的品牌或款式",
    "price_jpy": 18000,
    "estimated_profit": 220,
    "url": "商品链接",
    "img_url": "图片链接",
    "reason": "一句话理由,引用规则书或案例库的具体条目",
    "tag": "强推"
  }}
]

tag 三选一: "强推" / "推荐" / "盲盒"。
本批无符合商品时,输出 []。
"""

FINAL_REPORT_PROMPT = """
你是跨境采销总顾问。下面是多批次合并后的候选商品 (JSON)。

# 任务
从候选中挑出最值得关注的商品,最多 15 条。

# 风格要求
- 不使用 emoji。
- 不使用"暴利""绝密""王炸""无脑"等词汇。
- 不使用感叹号。
- 语气冷静,像运营人员的日常报告。
- 移动端阅读,每条尽量精简。

# 输出格式
本轮扫到 X 条,精选 N 条。

**1. [品牌/款式] · 利润 ¥XXX**
原标题: [完整的原始日文商品标题]
煤炉价 ¥XXXX 日元 · [标签]
<img src="从 JSON 的 img_url 字段取值" width="200">
[一句话理由,30 字以内]
[从 JSON 的 url 字段取值]

**2. ...**

# 约束
- 排序按预估利润从高到低。
- img_url 和 url: 使用 JSON 中提供的真实链接,不要替换为占位符。
- 不加表头、分隔线、总结段。
- 最后追加一行: "请前往复盘表登记今日反馈。"
"""

DAILY_SUMMARY_PROMPT = """
你是跨境采销总顾问。下面是今日全天去重后的候选商品 (JSON)。

# 任务
生成全天精选汇总,最多 30 条。

# 风格要求
- 不使用 emoji、夸张词汇或感叹号。
- 语气冷静专业。

# 输出格式
今日全天精选汇总
全天累计 N 条符合规则的商品。

**1. [品牌/款式] · 利润 ¥XXX**
原标题: [完整的原始日文商品标题]
煤炉价 ¥XXXX 日元 · [标签]
<img src="从 JSON 的 img_url 字段取值" width="200">
[一句话理由,30 字以内]
[从 JSON 的 url 字段取值]

**2. ...**

# 约束
- 排序按预估利润从高到低。
- img_url 和 url: 使用 JSON 中提供的真实链接。
"""


# ==========================================
# 阶段 1:分批筛选
# ==========================================
def filter_single_batch(items, batch_num, total_batches):
    """让 AI 对一批商品输出 JSON 格式的候选列表"""
    user_prompt = (
        f"这是第 {batch_num}/{total_batches} 批,共 {len(items)} 个商品。"
        f"请按规则筛选并输出 JSON:\n\n"
        f"{json.dumps(items, ensure_ascii=False, indent=2)}"
    )
    messages = [
        {"role": "system", "content": BATCH_FILTER_PROMPT},
        {"role": "user", "content": user_prompt}
    ]

    for attempt in range(3):
        try:
            raw = call_ai(messages)
            candidates = _parse_json_response(raw)
            logger.info(f"第 {batch_num} 批筛出 {len(candidates)} 条候选")
            return candidates
        except json.JSONDecodeError:
            logger.warning(f"第 {batch_num} 批 JSON 解析失败 (尝试 {attempt + 1}/3)")
            if attempt < 2:
                time.sleep(3)
        except Exception as e:
            logger.warning(f"第 {batch_num} 批调用异常 (尝试 {attempt + 1}/3): {e}")
            if attempt < 2:
                time.sleep((attempt + 1) * 5)

    logger.error(f"第 {batch_num} 批最终失败")
    return []


def _parse_json_response(raw):
    """处理可能被 Markdown 代码块包裹的 JSON"""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    return json.loads(cleaned)


# ==========================================
# 阶段 2:汇总报告
# ==========================================
def evaluate_with_ai(items, batch_size=15):
    """主入口:分批筛选 → 图片代理 → 持久化 → 生成报告"""
    if not items:
        return "本轮未抓到数据。"

    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    total = len(items)
    logger.info(f"共 {total} 条数据,分 {len(batches)} 批送 AI")

    all_candidates = []
    for i, batch in enumerate(batches, 1):
        logger.info(f"筛选第 {i}/{len(batches)} 批")
        candidates = filter_single_batch(batch, i, len(batches))
        all_candidates.extend(candidates)
        if i < len(batches):
            time.sleep(2)

    logger.info(f"分批完成,共 {len(all_candidates)} 条候选")

    # 对图片 URL 做代理转换
    for item in all_candidates:
        item["img_url"] = proxy_image_url(item.get("img_url", ""))

    if all_candidates:
        _append_to_daily_pool(all_candidates)

    if not all_candidates:
        return f"本轮扫到 {total} 条,无符合规则的商品。"

    return _generate_final_report(all_candidates, total)


def _append_to_daily_pool(candidates):
    """追加候选到每日汇总池"""
    try:
        pool = []
        if os.path.exists(DAILY_POOL_FILE):
            with open(DAILY_POOL_FILE, "r", encoding="utf-8") as f:
                pool = json.load(f)
        pool.extend(candidates)
        with open(DAILY_POOL_FILE, "w", encoding="utf-8") as f:
            json.dump(pool, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"写入每日汇总池失败: {e}")


def _generate_final_report(candidates, total):
    """生成精简报告,AI 失败时降级为本地拼装"""
    user_prompt = (
        f"本轮扫到 {total} 条,候选 {len(candidates)} 条。\n\n"
        f"候选 JSON:\n{json.dumps(candidates, ensure_ascii=False, indent=2)}\n\n"
        f"请按格式生成移动端精简报告,最多 15 条。"
    )
    messages = [
        {"role": "system", "content": FINAL_REPORT_PROMPT},
        {"role": "user", "content": user_prompt}
    ]

    try:
        return call_ai(messages)
    except Exception as e:
        logger.error(f"报告生成失败,降级为本地拼装: {e}")
        return _build_fallback_report(candidates, total)


def _build_fallback_report(candidates, total):
    """降级方案:按利润排序,本地拼装文本"""
    sorted_items = sorted(
        candidates,
        key=lambda x: x.get('estimated_profit', 0)
        if isinstance(x.get('estimated_profit'), (int, float)) else 0,
        reverse=True
    )

    lines = [f"本轮扫到 {total} 条,精选 {min(15, len(sorted_items))} 条。\n"]
    for i, item in enumerate(sorted_items[:15], 1):
        lines.append(
            f"**{i}. {item.get('brand', '?')} · 利润 ¥{item.get('estimated_profit', '?')}**\n"
            f"原标题: {item.get('title', '未知')}\n"
            f"煤炉价 ¥{item.get('price_jpy', '?')} 日元 · {item.get('tag', '?')}\n"
            f"<img src=\"{item.get('img_url', '')}\" width=\"200\">\n"
            f"{item.get('reason', '')}\n"
            f"{item.get('url', '')}\n"
        )
    lines.append("请前往复盘表登记今日反馈。")
    return "\n".join(lines)


# ==========================================
# 每日汇总
# ==========================================
def generate_daily_summary():
    """汇总当天所有候选,按 URL 去重后生成日报"""
    if not os.path.exists(DAILY_POOL_FILE):
        return "今日无符合规则的商品。"

    try:
        with open(DAILY_POOL_FILE, "r", encoding="utf-8") as f:
            pool = json.load(f)
        if not pool:
            return "今日无符合规则的商品。"

        # 按 URL 去重(同一商品白天可能被多轮扫到)
        unique = {item.get('url'): item for item in pool if item.get('url')}
        deduped = list(unique.values())

        # 统一做图片代理转换
        for item in deduped:
            item["img_url"] = ensure_proxied(item.get("img_url", ""))

        logger.info(f"全天去重后 {len(deduped)} 条")

        messages = [
            {"role": "system", "content": DAILY_SUMMARY_PROMPT},
            {"role": "user", "content": (
                f"今日去重后共 {len(deduped)} 条。\n"
                f"JSON:\n{json.dumps(deduped, ensure_ascii=False, indent=2)}\n"
                f"请生成汇总报告。"
            )}
        ]
        return call_ai(messages)
    except Exception as e:
        logger.error(f"生成每日汇总失败: {e}")
        return "汇总生成失败,请检查日志。"