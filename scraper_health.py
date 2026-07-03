"""
抓取健康检查 - 让"某平台连续多轮抓到 0 条"变得可见,而不是被当成"没有商品"。

只做三件事:
1. 记录每个平台每轮的抓取数量与连续 0 次数(纯内存状态,由调用方传入/取回)
2. 状态持久化到本地 JSON 文件,支持缺失/损坏时安全恢复,写入原子替换
3. 判定哪些平台达到连续 0 阈值、需要触发一次告警(直到该平台恢复才允许再次告警)

不涉及抓取、品牌筛选、LLM 评估、推送等业务逻辑,由 main.py 调用。
"""
import os
import json
import logging
import tempfile

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 3


def _default_entry():
    return {"consecutive_zero": 0, "alerted": False}


def load_health_state(path):
    """读取健康状态文件。文件不存在、损坏或格式异常时都安全回退到空状态。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("健康状态文件内容不是 JSON 对象")
        return data
    except Exception as e:
        logger.warning(f"抓取健康状态文件损坏或格式异常,重新初始化: {e}")
        return {}


def save_health_state(path, state):
    """原子写入健康状态文件,写入失败不影响已有文件内容。"""
    try:
        target_dir = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp_path = tempfile.mkstemp(prefix=".scraper_health_", suffix=".tmp", dir=target_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
    except Exception as e:
        logger.error(f"写入抓取健康状态失败: {e}")


def update_platform_counts(state, counts, threshold=DEFAULT_THRESHOLD):
    """
    用本轮各平台抓取数量更新状态。

    - count > 0: 连续 0 计数清零,告警状态清除(允许下次异常重新触发)。
    - count == 0: 连续 0 计数 +1;首次达到 threshold 时才加入待告警列表并标记已告警,
      之后同一段连续异常不会重复出现在待告警列表里,直到恢复。

    返回 (更新后的 state, 本轮需要告警的平台列表)。
    """
    to_alert = []
    for platform, count in counts.items():
        entry = dict(state.get(platform, _default_entry()))
        if count > 0:
            entry["consecutive_zero"] = 0
            entry["alerted"] = False
        else:
            entry["consecutive_zero"] = entry.get("consecutive_zero", 0) + 1
            if entry["consecutive_zero"] >= threshold and not entry.get("alerted", False):
                entry["alerted"] = True
                to_alert.append(platform)
        state[platform] = entry
    return state, to_alert


def format_alert_message(platform, consecutive_zero):
    """告警文案:不断言一定是抓取失败,只提示排查方向。"""
    return (
        f"[{platform}] 已连续 {consecutive_zero} 轮抓取结果为 0,"
        f"可能是页面结构变化、访问受限,也可能是当前确实没有符合条件的商品,请检查。"
    )
