"""
AI 评估模块

关键设计:LLM 只做判断,Python 做算术。

LLM 的职责:
- 识别品牌/款式
- 判断是否命中金矿/踩坑案例
- 给出"国内参考行情价"(domestic_ref_price)

Python 的职责:
- 用确定性公式算成本、税费、利润
- 按利润判定标签(强推/推荐/盲盒/淘汰)
- 货币换算

这么分工是因为 LLM 做多步骤算术不可靠,prompt 写得再清楚也常算错。
"""
import os
import re
import json
import time
import logging
import tempfile
from urllib.parse import quote

import httpx
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ----------------------------------------
# 配置
# ----------------------------------------
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
SILICONFLOW_KEY = os.getenv("SILICONFLOW_API_KEY")

PRIMARY_MODEL = os.getenv("PRIMARY_MODEL", "deepseek-chat")
BACKUP_MODEL = os.getenv("BACKUP_MODEL", "deepseek-ai/DeepSeek-V3")

# 日元兑人民币汇率,卡片显示和成本计算都用这个值
JPY_TO_CNY = float(os.getenv("JPY_TO_CNY", "0.045"))

DAILY_POOL_FILE = "daily_pool.json"
IMAGE_PROXY_HOST = "https://wsrv.nl/"

_http_client = httpx.Client(proxy=None)

primary_client = OpenAI(
    api_key=DEEPSEEK_KEY,
    base_url="https://api.deepseek.com/v1",
    timeout=120.0,
    http_client=_http_client,
)

backup_client = OpenAI(
    api_key=SILICONFLOW_KEY,
    base_url="https://api.siliconflow.cn/v1",
    timeout=120.0,
    http_client=_http_client,
)


# ----------------------------------------
# 工具
# ----------------------------------------
def _load_file(filename, default_text):
    path = os.path.join(os.path.dirname(__file__), filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning(f"未找到 {filename},使用默认值")
        return default_text


def proxy_image_url(url):
    if not url:
        return ""
    return f"{IMAGE_PROXY_HOST}?url={quote(url, safe='')}&w=300&h=300&fit=cover"


def ensure_proxied(url):
    if not url or url.startswith(IMAGE_PROXY_HOST):
        return url
    return proxy_image_url(url)


RULES = _load_file("rules.md", "请使用通用商业知识进行判断。")
CASES = _load_file("cases.md", "暂无实战案例。")


# ----------------------------------------
# 利润计算(Python 端确定性公式)
# ----------------------------------------
def calculate_cost(price_jpy):
    """返回 (换算基础价 CNY, 总成本 CNY, 是否缴税)"""
    base = price_jpy * JPY_TO_CNY
    tax_base = base * 0.2
    if tax_base < 50:
        cost = base + 50  # 免税,运费+手续费固定 50
        taxed = False
    else:
        cost = base + 50 + base * 0.13  # 缴税 13%
        taxed = True
    return round(base, 2), round(cost, 2), taxed


def calculate_profit(price_jpy, domestic_ref_price):
    """返回 (利润 CNY, 换算基础价, 总成本, 是否缴税)"""
    base, cost, taxed = calculate_cost(price_jpy)
    profit = round(domestic_ref_price - cost, 2)
    return profit, base, cost, taxed


def assign_tag(profit, is_gold_mine):
    """按利润五档判定标签(与 rules.md 第七条一致)。返回 None 表示淘汰。
    is_gold_mine 保留形参以兼容调用方,不再影响判定结果。"""
    if profit < 0:
        return None
    if profit < 10:
        return "盲盒"
    if profit < 80:
        return "观望"
    if profit < 150:
        return "推荐"
    return "强推"


# ----------------------------------------
# LLM 调用
# ----------------------------------------
def call_ai(messages):
    """主 API 失败时切到备用,两边都失败才抛异常。"""
    try:
        resp = primary_client.chat.completions.create(
            model=PRIMARY_MODEL,
            messages=messages,
            temperature=0,
        )
        return resp.choices[0].message.content
    except Exception as primary_err:
        logger.warning(f"主 API 失败,切换备用: {primary_err}")
        try:
            resp = backup_client.chat.completions.create(
                model=BACKUP_MODEL,
                messages=messages,
                temperature=0,
            )
            return resp.choices[0].message.content
        except Exception as backup_err:
            raise RuntimeError(f"主备 API 均失败。主: {primary_err} | 备: {backup_err}")


# ----------------------------------------
# Prompt
# ----------------------------------------
# 关键改动:不让 LLM 算利润,只让它给参考价。
# 利润 = 国内参考价 - 总成本,Python 算。

BATCH_FILTER_PROMPT = f"""
你是跨境采销选品助手,任务是从一批商品里筛出值得人工复核的候选。

# 规则书
{RULES}

# 实战案例
{CASES}

# 你的任务
对每个商品,做以下判断:
1. 识别品牌或款式名(brand)
2. 判断是否命中"踩坑案例"特征 → 是则跳过这条,不要输出
3. 判断是否命中"金矿案例"特征 → 是则 is_gold_mine=true
4. 给出"国内参考行情价"(domestic_ref_price),单位人民币
   - 这是你最重要的判断,要参考规则书里的款式价格区间
   - 标题信息不足时,给出一个保守估计
5. 一句话理由,引用规则书或案例库的具体条目

# 重要约定
- 你只负责给出参考价(domestic_ref_price),不要计算利润、成本、税。
  这些由代码用确定性公式计算,你算了也会被忽略。
- 命中"绝对跳过"条件的商品,不要输出。
- 标题信息严重不足、无法判断款式的,也不要输出。

# 输出格式
仅输出 JSON 数组,不要任何额外说明,不要 Markdown 代码块包裹。
每个元素结构:
[
  {{
    "title": "原始商品标题",
    "brand": "识别出的品牌或款式",
    "price_jpy": 18000,
    "domestic_ref_price": 380,
    "is_gold_mine": false,
    "url": "商品链接",
    "img_url": "图片链接",
    "reason": "【绝对不能留空】请用 15 字以内写明保留原因（例如：命中xx金矿款、低于3000日元低价盲盒、高利润推荐）"
  }}
]
本批无符合商品时,输出 []。
"""


# ----------------------------------------
# 分批筛选
# ----------------------------------------
def _extract_json_array(raw):
    """从 LLM 返回里抽出 JSON 数组,容忍代码块包裹和多余文字。"""
    code_block = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.DOTALL)
    text = code_block.group(1) if code_block else raw

    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError("LLM 响应中未找到 JSON 数组")
    return json.loads(match.group(0))


def filter_single_batch(items, batch_num, total_batches):
    user_prompt = (
        f"这是第 {batch_num}/{total_batches} 批,共 {len(items)} 个商品。"
        f"请按规则筛选并输出 JSON:\n\n"
        f"{json.dumps(items, ensure_ascii=False, indent=2)}"
    )
    messages = [
        {"role": "system", "content": BATCH_FILTER_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    for attempt in range(3):
        try:
            raw = call_ai(messages)
            candidates = _extract_json_array(raw)
            logger.info(f"第 {batch_num} 批 LLM 返回 {len(candidates)} 条")
            return candidates
        except Exception as e:
            logger.warning(f"第 {batch_num} 批异常 (尝试 {attempt + 1}/3): {e}")
            time.sleep((attempt + 1) * 3)
    return []


def is_valid_url(url):
    """商品链接是否可用于推送。图片链接缺失不受此约束,单独处理。
    公开函数(main.py 在写 SQLite 影子数据前也复用同一套判定标准)。"""
    return isinstance(url, str) and url.strip().lower().startswith(("http://", "https://"))


def _enrich_with_profit(candidates):
    """用 Python 算利润、补字段、判定标签。返回 (pushable_items, all_evaluated_items):

    - pushable_items: 利润 >= 0 的候选,字段和行为与此前完全一致,
      用于 daily_pool.json / 飞书推送。
    - all_evaluated_items: 包含 pushable_items,以及利润 < 0 但已完成成本/利润计算的商品——
      这些商品 tag 固定写"跳过"(与 rules.md 第七条"跳过"档一致),
      estimated_profit/total_cost/taxed 仍是真实计算值,只是不进入推送。

    缺有效 URL、缺价格字段的候选仍然被跳过,不出现在任一列表(从未真正完成利润计算)。
    """
    pushable = []
    all_evaluated = []
    skipped_no_url = 0
    for item in candidates:
        if not is_valid_url(item.get("url")):
            skipped_no_url += 1
            continue

        price_jpy = item.get("price_jpy")
        ref_price = item.get("domestic_ref_price")

        if not isinstance(price_jpy, (int, float)) or not isinstance(ref_price, (int, float)):
            logger.warning(f"跳过缺少价格字段的候选: {str(item.get('title', ''))[:30]}")
            continue

        profit, base, cost, taxed = calculate_profit(price_jpy, ref_price)
        tag = assign_tag(profit, item.get("is_gold_mine", False))

        item["price_cny"] = base       # 日元换算成人民币(基础价,不含运费税)
        item["total_cost"] = cost      # 总成本(含运费手续费,可能含税)
        item["estimated_profit"] = round(profit)
        item["taxed"] = taxed

        if tag is None:
            logger.info(f"过滤亏损商品: {str(item.get('brand', ''))[:20]} 利润 ¥{profit}")
            item["tag"] = "跳过"
            all_evaluated.append(item)
            continue

        item["tag"] = tag
        pushable.append(item)
        all_evaluated.append(item)

    if skipped_no_url:
        logger.info(f"跳过缺少有效 URL 的候选: {skipped_no_url} 条")

    return pushable, all_evaluated


def evaluate_with_ai(items, batch_size=15):
    """对外主入口。返回 (top_items, all_evaluated_items):
    top_items 是按利润降序排好的前 15 条,推送/daily_pool 口径完全不变;
    all_evaluated_items 是全部完成成本/利润计算的候选,包含 top_items 的来源集合
    (利润 >= 0)以及利润 < 0 被标记为"跳过"的商品,供 main.py 写入 SQLite evaluations 留档
    (ai_filter.py 本身不导入 sqlite3、不碰任何数据库,也不因为这份留档改变推送内容)。"""
    if not items:
        return [], []

    # LLM 输出的 JSON schema 里既不带 category,也不带 main.py 算好的价格变动信号,
    # 评估后按 url 从原始候选透传回来,不能被 LLM 输出覆盖或丢失。
    _PASSTHROUGH_FIELDS = (
        "category", "previous_price_jpy", "price_drop_jpy", "price_drop_pct", "is_price_drop",
    )
    url_to_passthrough = {
        item.get("url"): {f: item.get(f) for f in _PASSTHROUGH_FIELDS}
        for item in items if item.get("url")
    }

    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    all_candidates = []

    for i, batch in enumerate(batches, 1):
        all_candidates.extend(filter_single_batch(batch, i, len(batches)))
        if i < len(batches):
            time.sleep(2)

    if not all_candidates:
        return [], []

    # 1. Python 算利润、判标签;pushable 只含利润 >= 0 的(推送口径),
    #    all_evaluated 额外包含利润 < 0 但已完成计算的"跳过"商品(仅供留档)。
    pushable, all_evaluated = _enrich_with_profit(all_candidates)

    # 2. 补回 LLM 输出里丢失的 category / 价格变动字段,对全部已评估的商品都做
    #    (即使利润 < 0 被标记"跳过"的商品,也保留价格变动信息,只是不参与推送)
    for item in all_evaluated:
        passthrough = url_to_passthrough.get(item.get("url"), {})
        if not item.get("category"):
            item["category"] = passthrough.get("category")
        item["previous_price_jpy"] = passthrough.get("previous_price_jpy")
        item["price_drop_jpy"] = passthrough.get("price_drop_jpy")
        item["price_drop_pct"] = passthrough.get("price_drop_pct")
        item["is_price_drop"] = passthrough.get("is_price_drop", False)

    if not pushable:
        logger.info("所有候选利润 < 0,过滤后无可推送商品")
        return [], all_evaluated

    logger.info(f"利润过滤后剩 {len(pushable)}/{len(all_candidates)} 条")

    # 3. 图片走代理(对老链接更稳),只对会被推送展示的候选做
    for item in pushable:
        item["img_url"] = ensure_proxied(item.get("img_url", ""))

    # 4. 写入当日候选池(用于晚间汇总),只写利润 >= 0 的,行为与此前完全一致
    _append_to_daily_pool(pushable)

    # 5. 按利润降序,取前 15 作为实际推送候选
    top_items = sorted(
        pushable,
        key=lambda x: x.get("estimated_profit", 0),
        reverse=True,
    )[:15]
    return top_items, all_evaluated


def _append_to_daily_pool(candidates):
    """读取-合并-原子写回,避免写入中途崩溃导致 daily_pool.json 损坏。"""
    try:
        pool = []
        if os.path.exists(DAILY_POOL_FILE):
            with open(DAILY_POOL_FILE, "r", encoding="utf-8") as f:
                pool = json.load(f)
        pool.extend(candidates)

        pool_dir = os.path.dirname(os.path.abspath(DAILY_POOL_FILE)) or "."
        fd, tmp_path = tempfile.mkstemp(prefix=".daily_pool_", suffix=".tmp", dir=pool_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(pool, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, DAILY_POOL_FILE)  # 同盘原子替换,不会留下半截文件
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
    except Exception as e:
        logger.error(f"写入汇总池失败: {e}")


# ----------------------------------------
# 每日汇总
# ----------------------------------------
def generate_daily_summary():
    """返回去重排序后的候选列表。不再走 LLM。"""
    if not os.path.exists(DAILY_POOL_FILE):
        return []

    try:
        with open(DAILY_POOL_FILE, "r", encoding="utf-8") as f:
            pool = json.load(f)
    except Exception as e:
        logger.error(f"读取汇总池失败: {e}")
        return []

    if not pool:
        return []

    unique = {item.get("url"): item for item in pool if item.get("url")}
    deduped = list(unique.values())

    for item in deduped:
        item["img_url"] = ensure_proxied(item.get("img_url", ""))

    return sorted(
        deduped,
        key=lambda x: x.get("estimated_profit", 0) if isinstance(x.get("estimated_profit"), (int, float)) else 0,
        reverse=True,
    )