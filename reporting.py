"""
reporting.py - 只读复盘报告 / 偏好信号生成器

只负责:连接 kendama.db、做统计聚合、渲染 Markdown 文件。
不抓取、不调用 LLM、不生成飞书卡片、不修改任何业务规则(rules.md/cases.md/LLM prompt),
不碰 feedback.db(反馈复盘统计的是 kendama.db.feedback_events,不是旧库本身)。

两个对外入口:
    generate_weekly_review(days=7)  -> (报告文件路径, 摘要 dict)
    generate_signals_report()       -> (信号文件路径, 摘要 dict)
"""
import os
import logging
from datetime import datetime, timedelta

import db

logger = logging.getLogger(__name__)

REPORTS_DIR = "reports"
SIGNALS_FILE = "personalized_signals.md"

# 反馈动作的正负向映射——只按当前项目里实际观察到的字面值归类,
# 不猜测未知动作的业务含义;其余动作一律算作"未映射/其他",不参与正负向统计。
# "买入" 是 v1 时代的历史动作标签,"选的好" 是当前签名反馈按钮使用的标签,
# 两者都是"正向"的字面意思,必须一并纳入,否则真实历史数据会被系统性低估。
POSITIVE_ACTIONS = {"选的好", "买入"}
NEGATIVE_ACTIONS = {"放弃"}

# 偏好信号最小样本量门槛:低于这个数量的分组不形成"高反馈/低反馈"结论,
# 只如实列出样本数,标记为"样本不足"。
MIN_SAMPLE_SIZE = 3


def _cutoff_iso(days):
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


def _format_count_list(counts, empty_text="(无数据)"):
    """把 {key: count} 字典渲染成 Markdown 子列表,而不是直接打印 Python dict 的 repr。"""
    if not counts:
        return [f"  - {empty_text}"]
    return [f"  - {k}: {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])]


# ----------------------------------------
# 周报:扫描与筛选漏斗
# ----------------------------------------
def _funnel_stats(conn, cutoff):
    rows = conn.execute(
        """
        SELECT raw_item_count, brand_matched_count, llm_input_count,
               evaluated_count, candidate_count, status, finished_at
        FROM scan_runs
        WHERE started_at >= ?
        """,
        (cutoff,),
    ).fetchall()

    def _sum(idx):
        return sum((r[idx] or 0) for r in rows)

    raw_total = _sum(0)
    brand_total = _sum(1)
    llm_total = _sum(2)
    eval_total = _sum(3)
    cand_total = _sum(4)
    abnormal = sum(1 for r in rows if r[5] != "ok" or r[6] is None)

    def _rate(numer, denom):
        return round(numer / denom * 100, 1) if denom else None

    return {
        "scan_run_count": len(rows),
        "raw_item_total": raw_total,
        "brand_matched_total": brand_total,
        "llm_input_total": llm_total,
        "evaluated_total": eval_total,
        "candidate_total": cand_total,
        "abnormal_count": abnormal,
        "rate_brand_from_raw": _rate(brand_total, raw_total),
        "rate_llm_from_brand": _rate(llm_total, brand_total),
        "rate_eval_from_llm": _rate(eval_total, llm_total),
        "rate_candidate_from_eval": _rate(cand_total, eval_total),
    }


# ----------------------------------------
# 周报:评估与利润分布
# ----------------------------------------
def _profit_bucket(profit):
    if profit < 0:
        return "跳过(<0)"
    if profit < 10:
        return "盲盒(0-9)"
    if profit < 80:
        return "观望(10-79)"
    if profit < 150:
        return "推荐(80-149)"
    return "强推(>=150)"


def _profit_stats(conn, cutoff):
    rows = conn.execute(
        """
        SELECT e.tag, e.selected_for_push, e.price_jpy, e.estimated_profit,
               e.is_price_drop, e.brand, e.source_category
        FROM evaluations e
        JOIN scan_runs r ON r.id = e.scan_run_id
        WHERE r.started_at >= ?
        """,
        (cutoff,),
    ).fetchall()

    by_tag = {}
    selected_for_push = 0
    skipped = 0
    price_drop_count = 0
    profits = []
    prices = []
    by_brand = {}
    by_category = {}
    profit_buckets = {}

    for tag, selected, price_jpy, profit, is_drop, brand, category in rows:
        by_tag[tag] = by_tag.get(tag, 0) + 1
        if selected:
            selected_for_push += 1
        if tag == "跳过":
            skipped += 1
        if is_drop:
            price_drop_count += 1
        if profit is not None:
            profits.append(profit)
            profit_buckets[_profit_bucket(profit)] = profit_buckets.get(_profit_bucket(profit), 0) + 1
        if price_jpy is not None:
            prices.append(price_jpy)

        brand_key = brand or "(未知品牌)"
        b = by_brand.setdefault(brand_key, {"count": 0, "profit_sum": 0.0})
        b["count"] += 1
        b["profit_sum"] += profit or 0

        cat_key = category or "(未知来源)"
        c = by_category.setdefault(cat_key, {"count": 0, "profit_sum": 0.0})
        c["count"] += 1
        c["profit_sum"] += profit or 0

    def _avg_summary(groups):
        return {
            k: {"count": v["count"], "avg_profit": round(v["profit_sum"] / v["count"], 1)}
            for k, v in groups.items()
        }

    return {
        "total_evaluations": len(rows),
        "by_tag": by_tag,
        "selected_for_push": selected_for_push,
        "skipped_count": skipped,
        "price_drop_count": price_drop_count,
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "price_avg": round(sum(prices) / len(prices), 1) if prices else None,
        "profit_min": min(profits) if profits else None,
        "profit_max": max(profits) if profits else None,
        "profit_avg": round(sum(profits) / len(profits), 1) if profits else None,
        "profit_buckets": profit_buckets,
        "by_brand": _avg_summary(by_brand),
        "by_category": _avg_summary(by_category),
    }


# ----------------------------------------
# 周报:反馈复盘
# ----------------------------------------
def _feedback_stats(conn, cutoff):
    """窗口内反馈是品牌/来源/tag/正负向等"行为分析"的唯一依据;
    窗口外历史反馈只统计数量(含 live/legacy_import 拆分),不参与行为分析——
    避免"刚导入的历史反馈"被误判为"反馈数据缺失"或"系统未生效"。
    空 URL 的反馈(历史测试脏数据)仍计入原始总数和 by_action/by_source,
    但不参与品牌/来源/tag 分布,也不参与正负向倾向统计。"""
    all_rows = conn.execute("SELECT source, created_at FROM feedback_events").fetchall()
    total_all = len(all_rows)
    window_meta = [r for r in all_rows if r[1] >= cutoff]
    outside_meta = [r for r in all_rows if r[1] < cutoff]

    window_total = len(window_meta)
    outside_total = len(outside_meta)
    window_live = sum(1 for r in window_meta if r[0] == "live")
    window_legacy = sum(1 for r in window_meta if r[0] == "legacy_import")
    outside_live = sum(1 for r in outside_meta if r[0] == "live")
    outside_legacy = sum(1 for r in outside_meta if r[0] == "legacy_import")

    rows = conn.execute(
        """
        SELECT fe.action, fe.source, fe.listing_id, fe.url,
               e.brand, e.source_category, e.tag
        FROM feedback_events fe
        LEFT JOIN evaluations e ON e.id = (
            SELECT id FROM evaluations e2
            WHERE e2.listing_id = fe.listing_id
            ORDER BY e2.id DESC LIMIT 1
        )
        WHERE fe.created_at >= ?
        """,
        (cutoff,),
    ).fetchall()

    by_action = {}
    by_source = {}
    by_brand = {}
    by_category = {}
    by_tag = {}
    linked = 0
    positive = 0
    negative = 0

    for action, source, listing_id, url, brand, category, tag in rows:
        by_action[action] = by_action.get(action, 0) + 1
        by_source[source] = by_source.get(source, 0) + 1
        if listing_id is not None:
            linked += 1

        has_url = bool(url)
        if has_url:
            # 空 URL 的历史测试记录不参与正负向倾向统计
            if action in POSITIVE_ACTIONS:
                positive += 1
            elif action in NEGATIVE_ACTIONS:
                negative += 1

        if has_url and listing_id is not None:
            # 只有非空 URL 且能关联到 listing(从而查得到 brand/tag)的反馈才参与分布统计
            by_brand[brand or "(未知品牌)"] = by_brand.get(brand or "(未知品牌)", 0) + 1
            by_category[category or "(未知来源)"] = by_category.get(category or "(未知来源)", 0) + 1
            by_tag[tag or "(未知标签)"] = by_tag.get(tag or "(未知标签)", 0) + 1

    return {
        "total_all": total_all,
        "window_total": window_total,
        "outside_total": outside_total,
        "window_live": window_live,
        "window_legacy": window_legacy,
        "outside_live": outside_live,
        "outside_legacy": outside_legacy,
        "by_action": by_action,
        "by_source": by_source,
        "linked_count": linked,
        "unlinked_count": window_total - linked,
        "positive_count": positive,
        "negative_count": negative,
        "by_brand": by_brand,
        "by_category": by_category,
        "by_tag": by_tag,
    }


# ----------------------------------------
# 周报:数据质量与边界
# ----------------------------------------
def _quality_stats(conn, cutoff):
    fk_issues = conn.execute("PRAGMA foreign_key_check").fetchall()

    no_feedback_pushed = conn.execute(
        """
        SELECT COUNT(*) FROM evaluations e
        JOIN scan_runs r ON r.id = e.scan_run_id
        WHERE e.selected_for_push = 1 AND r.started_at >= ?
          AND NOT EXISTS (SELECT 1 FROM feedback_events fe WHERE fe.listing_id = e.listing_id)
        """,
        (cutoff,),
    ).fetchone()[0]

    missing_brand_or_category = conn.execute(
        """
        SELECT COUNT(*) FROM evaluations e
        JOIN scan_runs r ON r.id = e.scan_run_id
        WHERE r.started_at >= ? AND (e.brand IS NULL OR e.source_category IS NULL)
        """,
        (cutoff,),
    ).fetchone()[0]

    listings_without_price_history = conn.execute(
        """
        SELECT COUNT(*) FROM listings l
        WHERE NOT EXISTS (SELECT 1 FROM price_history ph WHERE ph.listing_id = l.id)
        """
    ).fetchone()[0]

    # 以下两项按全部历史(不限统计窗口)计算——它们是数据质量问题,
    # 不会因为发生在窗口之外就不存在,应该始终被看到。
    empty_url_feedback = conn.execute(
        "SELECT COUNT(*) FROM feedback_events WHERE url IS NULL OR url = ''"
    ).fetchone()[0]

    unlinked_feedback = conn.execute(
        "SELECT COUNT(*) FROM feedback_events WHERE listing_id IS NULL"
    ).fetchone()[0]

    return {
        "fk_issues": fk_issues,
        "no_feedback_pushed_count": no_feedback_pushed,
        "missing_brand_or_category_count": missing_brand_or_category,
        "listings_without_price_history_count": listings_without_price_history,
        "empty_url_feedback_count": empty_url_feedback,
        "unlinked_feedback_count": unlinked_feedback,
    }


def _render_weekly_review_markdown(days, cutoff, funnel, profit, feedback, quality):
    lines = [
        "# 每周复盘报告",
        "",
        f"- 生成时间: {datetime.now().isoformat(timespec='seconds')}",
        f"- 统计窗口: 最近 {days} 天(scan_runs.started_at >= {cutoff})",
        "",
        "> 本报告反映的是已抓取样本和当前规则下的统计结果,不代表全市场规模或真实成交利润"
        "(利润是基于国内参考价的估算,不是实际卖出价)。",
        "",
        "## 扫描与筛选漏斗",
        "",
        f"- scan_runs 数量: {funnel['scan_run_count']}",
        f"- raw_item_count 总和: {funnel['raw_item_total']}",
        f"- brand_matched_count 总和: {funnel['brand_matched_total']}",
        f"- llm_input_count 总和: {funnel['llm_input_total']}",
        f"- evaluated_count 总和: {funnel['evaluated_total']}",
        f"- candidate_count 总和: {funnel['candidate_total']}",
        f"- 转化率: 品牌命中/抓取={funnel['rate_brand_from_raw']}%,"
        f" 送LLM/品牌命中={funnel['rate_llm_from_brand']}%,"
        f" 评估完成/送LLM={funnel['rate_eval_from_llm']}%,"
        f" 候选/评估完成={funnel['rate_candidate_from_eval']}%",
        f"- 状态异常或未正常完成的 scan_run 数: {funnel['abnormal_count']}",
        "",
        "## 评估与利润分布",
        "",
        f"- evaluations 总数: {profit['total_evaluations']}",
        "- 按 tag 聚合:",
    ]
    lines += _format_count_list(profit["by_tag"])
    lines += [
        f"- selected_for_push=1 数: {profit['selected_for_push']}",
        f"- tag=\"跳过\" 数: {profit['skipped_count']}",
        f"- 降价候选数(is_price_drop=1): {profit['price_drop_count']}",
        f"- 价格区间(日元): min={profit['price_min']}, max={profit['price_max']}, avg={profit['price_avg']}",
        f"- 利润区间(元): min={profit['profit_min']}, max={profit['profit_max']}, avg={profit['profit_avg']}",
        "- 利润分档分布:",
    ]
    lines += _format_count_list(profit["profit_buckets"])
    lines += ["", "### 按品牌聚合(评估数量 / 平均利润)"]

    if profit["by_brand"]:
        for brand, stat in sorted(profit["by_brand"].items(), key=lambda x: -x[1]["count"]):
            lines.append(f"- {brand}: {stat['count']} 条, 平均利润 ¥{stat['avg_profit']}")
    else:
        lines.append("- (本窗口无数据)")

    lines += ["", "### 按来源分类聚合(评估数量 / 平均利润)"]
    if profit["by_category"]:
        for cat, stat in sorted(profit["by_category"].items(), key=lambda x: -x[1]["count"]):
            lines.append(f"- {cat}: {stat['count']} 条, 平均利润 ¥{stat['avg_profit']}")
    else:
        lines.append("- (本窗口无数据)")

    lines += [
        "",
        "## 反馈复盘",
        "",
        "> 动作映射规则(仅按当前观察到的字面值归类,不构成通用结论):"
        f" 正向 = {sorted(POSITIVE_ACTIONS)}, 负向 = {sorted(NEGATIVE_ACTIONS)},"
        " 其余动作单列为\"未映射/其他\",不强行归类。",
        "",
        f"- 数据库中 feedback_events 总数(不限时间窗口): {feedback['total_all']}",
        f"- 统计窗口内 feedback_events 数: {feedback['window_total']}"
        f"(其中 live={feedback['window_live']}, legacy_import={feedback['window_legacy']})",
        f"- 统计窗口外历史 feedback_events 数: {feedback['outside_total']}"
        f"(其中 live={feedback['outside_live']}, legacy_import={feedback['outside_legacy']})",
    ]

    if feedback["window_total"] == 0 and feedback["outside_total"] > 0:
        lines.append(
            f"> 本统计窗口内暂无反馈;数据库中仍有 {feedback['outside_total']} 条"
            "更早历史反馈未计入本周期行为分析。这不代表反馈数据缺失或系统未生效,"
            "只是这些反馈发生在统计窗口之外(可用 `--days` 加大统计窗口查看)。"
        )

    lines += [
        "",
        "以下行为分析(action / source / brand / source_category / tag / 正负向)"
        "仅基于**统计窗口内**的反馈:",
        "",
        "- 按 action 原样统计:",
    ]
    lines += _format_count_list(feedback["by_action"])
    lines += ["- 按 source 统计(legacy_import 与实时 live 分开,不混淆):"]
    lines += _format_count_list(feedback["by_source"])
    lines += [
        f"- 可关联到 listing 的反馈数: {feedback['linked_count']}",
        f"- 无法关联的反馈数: {feedback['unlinked_count']}",
        f"- 映射后正向反馈数: {feedback['positive_count']}, 负向反馈数: {feedback['negative_count']}"
        "(空 URL 的历史测试记录不参与此项统计)",
        "- 按 brand 分布(仅计入可关联、非空 URL 的反馈):",
    ]
    lines += _format_count_list(feedback["by_brand"])
    lines += ["- 按 source_category 分布(仅计入可关联、非空 URL 的反馈):"]
    lines += _format_count_list(feedback["by_category"])
    lines += ["- 按 tag 分布(仅计入可关联、非空 URL 的反馈):"]
    lines += _format_count_list(feedback["by_tag"])
    lines += [
        "",
        "> 注意:以上反馈统计不能直接称为"
        "\"模型准确率\"——反馈只反映你个人的买入/放弃决策,不是对 AI 判断正确性的"
        "完整、无偏验证。",
        "",
        "## 数据质量与边界",
        "",
        f"- PRAGMA foreign_key_check: {quality['fk_issues'] or '无异常'}",
        f"- 已推送但没有任何反馈的候选数量: {quality['no_feedback_pushed_count']}",
        f"- 缺少 brand 或 source_category 的 evaluations 数: {quality['missing_brand_or_category_count']}",
        f"- 首次出现且无价格历史的 listings 数: {quality['listings_without_price_history_count']}",
        f"- 空 URL 的 feedback_events 数(不限时间窗口,历史测试/脏数据,不参与偏好结论): "
        f"{quality['empty_url_feedback_count']}",
        f"- 无法关联到任何 listing 的 feedback_events 数(不限时间窗口): "
        f"{quality['unlinked_feedback_count']}",
        "",
        "> 本报告反映的是已抓取样本和当前规则下的统计结果,不代表全市场规模,"
        "也不代表真实成交利润。",
    ]

    return "\n".join(lines) + "\n"


def generate_weekly_review(days=7, db_file=None, reports_dir=None):
    """生成一份 Markdown 周报,写入 reports/weekly_review_YYYYMMDD.md
    (默认 7 天窗口)或 reports/weekly_review_YYYYMMDD_d{days}.md(非默认天数),
    避免同一天内不同 --days 取值互相覆盖。
    只读 kendama.db,不扫描、不调用 LLM、不推送飞书、不改写 feedback.db。
    返回 (报告文件路径, 摘要 dict)。"""
    reports_dir = reports_dir or REPORTS_DIR
    cutoff = _cutoff_iso(days)

    conn = db._connect_db(db_file)
    try:
        funnel = _funnel_stats(conn, cutoff)
        profit = _profit_stats(conn, cutoff)
        feedback = _feedback_stats(conn, cutoff)
        quality = _quality_stats(conn, cutoff)
    finally:
        conn.close()

    report_md = _render_weekly_review_markdown(days, cutoff, funnel, profit, feedback, quality)

    os.makedirs(reports_dir, exist_ok=True)
    today = datetime.now().strftime('%Y%m%d')
    if days == 7:
        filename = f"weekly_review_{today}.md"
    else:
        filename = f"weekly_review_{today}_d{days}.md"
    path = os.path.join(reports_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(report_md)

    summary = {
        "scan_runs": funnel["scan_run_count"],
        "evaluations": profit["total_evaluations"],
        "feedback_events_in_window": feedback["window_total"],
        "feedback_events_total": feedback["total_all"],
    }
    logger.info(f"周报已生成: {path}")
    return path, summary


# ----------------------------------------
# 偏好信号(仅供人工审核)
# ----------------------------------------
def _price_bucket(price_jpy):
    if price_jpy is None:
        return "(未知价格)"
    if price_jpy < 3000:
        return "<3000 日元"
    if price_jpy < 6000:
        return "3000-6000 日元"
    if price_jpy < 10000:
        return "6000-10000 日元"
    return ">=10000 日元"


def _linked_feedback_rows(conn):
    """只取能关联到 listing、且能查到对应评估记录的反馈行(取该 listing 最近一次评估)。"""
    return conn.execute(
        """
        SELECT fe.action, e.brand, e.source_category, e.price_jpy, e.is_price_drop
        FROM feedback_events fe
        JOIN evaluations e ON e.id = (
            SELECT id FROM evaluations e2
            WHERE e2.listing_id = fe.listing_id
            ORDER BY e2.id DESC LIMIT 1
        )
        WHERE fe.listing_id IS NOT NULL
        """
    ).fetchall()


def _pos_neg_other(actions):
    pos = sum(1 for a in actions if a in POSITIVE_ACTIONS)
    neg = sum(1 for a in actions if a in NEGATIVE_ACTIONS)
    return pos, neg, len(actions) - pos - neg


def _summarize_actions(groups):
    summary = {}
    for key, actions in groups.items():
        pos, neg, other = _pos_neg_other(actions)
        summary[key] = {"count": len(actions), "positive": pos, "negative": neg, "other": other}
    return summary


def _render_group_section(title, summary, min_sample_size):
    lines = [f"## {title}", ""]
    strong, weak, insufficient = [], [], []

    for key, stat in summary.items():
        mapped = stat["positive"] + stat["negative"]
        if stat["count"] < min_sample_size or mapped == 0:
            insufficient.append((key, stat))
            continue
        rate = stat["positive"] / mapped
        if rate >= 0.6:
            strong.append((key, stat, rate))
        elif rate <= 0.4:
            weak.append((key, stat, rate))
        else:
            insufficient.append((key, stat))  # 中性区间(40%-60%),不强行归类为高/低

    lines.append(f"### 高反馈(正向占比 >= 60%,且样本 >= {min_sample_size})")
    if strong:
        for key, stat, rate in sorted(strong, key=lambda x: -x[2]):
            lines.append(
                f"- {key}: 样本 {stat['count']} 条(正向 {stat['positive']} / 负向 {stat['negative']}),"
                f" 正向占比 {round(rate * 100, 1)}%"
            )
    else:
        lines.append("- (无满足条件的分组)")

    lines += ["", f"### 低反馈(正向占比 <= 40%,且样本 >= {min_sample_size})"]
    if weak:
        for key, stat, rate in sorted(weak, key=lambda x: x[2]):
            lines.append(
                f"- {key}: 样本 {stat['count']} 条(正向 {stat['positive']} / 负向 {stat['negative']}),"
                f" 正向占比 {round(rate * 100, 1)}%"
            )
    else:
        lines.append("- (无满足条件的分组)")

    lines += ["", f"### 样本不足,不形成结论(< {min_sample_size} 条,或正负向占比在 40%-60% 中性区间)"]
    if insufficient:
        for key, stat in insufficient:
            lines.append(
                f"- {key}: 样本 {stat['count']} 条(正向 {stat['positive']} / 负向 {stat['negative']})"
            )
    else:
        lines.append("- (无)")

    lines.append("")
    return lines


def _render_signals_markdown(total_all, linked_total, min_sample_size,
                              brand_summary, category_summary, price_summary, drop_stats):
    lines = [
        "# 个性化偏好信号(仅供人工审核)",
        "",
        f"- 生成时间: {datetime.now().isoformat(timespec='seconds')}",
        "- 数据窗口: 全部可关联的历史反馈(kendama.db.feedback_events,不限时间范围)",
        f"- 反馈总数: {total_all}",
        f"- 可关联到 listing 且能查到评估记录的反馈数: {linked_total}",
        f"- 动作映射说明: 正向 = {sorted(POSITIVE_ACTIONS)}, 负向 = {sorted(NEGATIVE_ACTIONS)},"
        " 其余动作不参与正负向统计",
        f"- 最小样本量门槛: {min_sample_size} 条(低于此数量的分组不形成结论,只如实列出样本数)",
        "",
        "**重要声明:本文件当前仅供人工审核。本轮不会被自动注入 LLM prompt,"
        "不修改 rules.md / cases.md / 筛选规则。是否采纳、如何采纳,由你自己决定。**",
        "",
        "统计方法:仅做计数、占比等可解释的算术聚合,不使用 embedding / 向量数据库 / XGBoost,"
        "不做隐藏黑箱评分,不会因为一条反馈就形成偏好结论。",
        "",
    ]

    if linked_total == 0:
        lines += [
            "> **当前没有可关联到近期扫描商品的有效反馈。**",
            "> 这通常意味着历史反馈对应的商品尚未在当前 SQLite 数据中重新出现,"
            "或反馈记录缺少可关联 URL;并不代表反馈功能失效。",
            "",
        ]

    lines += _render_group_section("高反馈 / 低反馈品牌", brand_summary, min_sample_size)
    lines += _render_group_section("关键词 / source_category 信号", category_summary, min_sample_size)
    lines += _render_group_section("价格区间信号", price_summary, min_sample_size)

    lines.append("## 降价商品的反馈情况")
    lines.append("")
    count, pos, neg, other = drop_stats
    mapped = pos + neg
    if count >= min_sample_size and mapped > 0:
        rate = round(pos / mapped * 100, 1)
        lines.append(
            f"- 降价商品关联反馈样本 {count} 条(正向 {pos} / 负向 {neg}),正向占比 {rate}%"
        )
    else:
        lines.append(
            f"- 样本不足,不形成结论(降价商品关联反馈仅 {count} 条,正向 {pos} / 负向 {neg})"
        )

    return "\n".join(lines) + "\n"


def generate_signals_report(db_file=None, signals_file=None, min_sample_size=MIN_SAMPLE_SIZE):
    """从 kendama.db 可关联的 feedback_events + evaluations 生成 personalized_signals.md。
    只读数据库,不做任何写入;只做可解释的统计聚合,样本不足时明确标注,
    本轮不会被自动注入 LLM prompt。返回 (信号文件路径, 摘要 dict)。"""
    signals_file = signals_file or SIGNALS_FILE

    conn = db._connect_db(db_file)
    try:
        rows = _linked_feedback_rows(conn)
        total_all = conn.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0]
    finally:
        conn.close()

    linked_total = len(rows)

    by_brand, by_category, by_price_bucket = {}, {}, {}
    drop_actions = []

    for action, brand, category, price_jpy, is_drop in rows:
        by_brand.setdefault(brand or "(未知品牌)", []).append(action)
        by_category.setdefault(category or "(未知来源)", []).append(action)
        by_price_bucket.setdefault(_price_bucket(price_jpy), []).append(action)
        if is_drop:
            drop_actions.append(action)

    brand_summary = _summarize_actions(by_brand)
    category_summary = _summarize_actions(by_category)
    price_summary = _summarize_actions(by_price_bucket)
    drop_stats = (len(drop_actions),) + _pos_neg_other(drop_actions)

    md = _render_signals_markdown(
        total_all, linked_total, min_sample_size,
        brand_summary, category_summary, price_summary, drop_stats,
    )

    with open(signals_file, "w", encoding="utf-8") as f:
        f.write(md)

    summary = {"total_feedback": total_all, "linked_feedback": linked_total}
    logger.info(f"偏好信号已生成: {signals_file}")
    return signals_file, summary
