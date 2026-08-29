"""
刷屏智能检测模块

原则：不删除、不过滤，只标记和分析。
区分正常重复（如应援、玩梗）与恶意刷屏（垃圾内容、机器人）。
"""
import difflib
from collections import Counter
from typing import Tuple

from config import (SPAM_HIGH_THRESHOLD, SPAM_MEDIUM_THRESHOLD,
                    SPAM_BURST_WINDOW_SECONDS, SPAM_BURST_HIGH_COUNT,
                    SPAM_BURST_MEDIUM_COUNT, SPAM_VARIANT_SIMILARITY,
                    SPAM_VARIANT_MIN_COUNT, SPAM_BURST_MIN_COUNT,
                    SPAM_COMBO_BONUS, SPAM_RELATIVE_MIN_POOL,
                    SPAM_RELATIVE_REPEAT_FLOOR, SPAM_RELATIVE_COUNT_FLOOR,
                    SPAM_RELATIVE_SCORE, REPEAT_EVENT_WINDOW_SECONDS,
                    REPEAT_EVENT_MIN_SENDERS, REPEAT_EVENT_MIN_TOTAL,
                    REPEAT_EVENT_TOP_N)


def content_similarity(a: str, b: str) -> float:
    """计算两条内容的相似度（0-1）"""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def analyze_content_repeat(contents: list[str]) -> Tuple[float, float, int]:
    """
    分析内容重复率

    Returns:
        (整体重复率, 两两相似度加权和, 两两相似度对数)
        不物化全量相似度列表（n(n-1)/2 个 float），调用方按 sum/count 取均值。
    """
    n = len(contents)
    if n <= 1:
        return 0.0, 0.0, 0

    counts = Counter(contents)
    unique = list(counts)
    repeat_rate = 1 - len(unique) / n

    # 先按内容去重，只对唯一内容两两比较，再用出现次数加权展开为全量两两相似度。
    # 与原 O(n²) 全量比较数学等价（相同内容相似度恒为 1，唯一对 (a,b) 贡献
    # count[a]*count[b] 个相同 sim），复杂度降为 O(u²)，u = 唯一内容数；
    # 且边算边累加 sum/count，不再物化 n(n-1)/2 个元素的列表。
    sim_sum = 0.0
    sim_count = 0
    for c in counts.values():
        k = c * (c - 1) // 2
        sim_sum += float(k)
        sim_count += k
    for i in range(len(unique)):
        ci = counts[unique[i]]
        for j in range(i + 1, len(unique)):
            sim = content_similarity(unique[i], unique[j])
            k = ci * counts[unique[j]]
            sim_sum += sim * k
            sim_count += k

    return repeat_rate, sim_sum, sim_count


def _burst_max(sorted_ts: list[int], window_seconds: int) -> int:
    """滑动窗口双指针 O(n)：任意 window_seconds 秒窗口内的最大弹幕条数。

    供机器人模式与「高频爆发」规则共用；要求 sorted_ts 已升序。"""
    burst = 0
    left = 0
    for right in range(len(sorted_ts)):
        while sorted_ts[right] - sorted_ts[left] > window_seconds:
            left += 1
        burst = max(burst, right - left + 1)
    return burst


def detect_bot_pattern(timestamps: list[int]) -> float:
    """
    检测机器人模式

    特征：
    - 时间间隔过于规律（标准差极小）
    - 短时间内大量发送

    Returns:
        bot_score (0-1)，越高越像机器人
    """
    if len(timestamps) < 3:
        return 0.0

    # 按时间排序
    sorted_ts = sorted(timestamps)
    intervals = [sorted_ts[i] - sorted_ts[i - 1] for i in range(1, len(sorted_ts))]

    if not intervals:
        return 0.0

    avg_interval = sum(intervals) / len(intervals)
    if avg_interval == 0:
        return 1.0  # 同一时间发送多条，高度疑似机器人

    # 计算间隔的标准差系数（变异系数）
    variance = sum((x - avg_interval) ** 2 for x in intervals) / len(intervals)
    std = variance ** 0.5
    cv = std / avg_interval if avg_interval > 0 else 0

    # 变异系数极小（<0.1）说明间隔非常规律，疑似机器人
    bot_score = max(0, 1 - cv * 10)  # cv越小，分数越高

    # 短时间内爆发：滑动窗口双指针 O(n) 统计窗口内最大条数
    burst_count = _burst_max(sorted_ts, SPAM_BURST_WINDOW_SECONDS)

    if burst_count >= SPAM_BURST_HIGH_COUNT:
        bot_score = max(bot_score, 0.7)
    elif burst_count >= SPAM_BURST_MEDIUM_COUNT:
        bot_score = max(bot_score, 0.4)

    return min(1.0, bot_score)


def analyze_spam(danmaku_contents: list[str], timestamps: list[int]) -> dict:
    """
    综合分析刷屏程度
    
    Returns:
        {
            "count": int,                # 弹幕总数
            "unique_count": int,         # 唯一内容数
            "repeat_rate": float,        # 重复率 0-1
            "avg_similarity": float,     # 平均内容相似度
            "avg_interval": float,       # 平均发送间隔（秒）
            "burst_max": int,            # 滑动窗口内最大条数（SPAM_BURST_WINDOW_SECONDS 秒窗）
            "bot_score": float,          # 机器人评分 0-1
            "spam_score": float,         # 综合刷屏评分 0-1
            "spam_level": str,           # 高/中/低
            "reason": str,               # 判定理由
        }
    """
    count = len(danmaku_contents)
    unique_contents = set(danmaku_contents)
    unique_count = len(unique_contents)

    # 内容重复率
    repeat_rate, sim_sum, sim_count = analyze_content_repeat(danmaku_contents)
    avg_similarity = sim_sum / sim_count if sim_count else 0.0

    # 时间间隔
    if len(timestamps) >= 2:
        sorted_ts = sorted(timestamps)
        intervals = [sorted_ts[i] - sorted_ts[i - 1] for i in range(1, len(sorted_ts))]
        avg_interval = sum(intervals) / len(intervals)
    else:
        avg_interval = 0.0

    # 机器人检测
    bot_score = detect_bot_pattern(timestamps)
    sorted_ts = sorted(timestamps)
    burst_max = _burst_max(sorted_ts, SPAM_BURST_WINDOW_SECONDS) if sorted_ts else 0

    # 综合刷屏评分：规则各自产出候选分，最终分 = 最高分 + 每多触发一条规则加成
    # SPAM_COMBO_BONUS（封顶 1.0）——「max 取极值」会让多个中等信号叠加的刷子
    # （重复中等+间隔偏规律+相似度偏高）得分反不如单触一条规则的人，弱证据累积
    # 恰是机器人最典型的特征，故改为组合计分。
    rule_scores: list[float] = []
    reasons = []

    # 规则1：大量重复内容
    if count >= SPAM_HIGH_THRESHOLD[0] and repeat_rate >= SPAM_HIGH_THRESHOLD[1]:
        rule_scores.append(0.85)
        reasons.append(f"大量重复({count}条, 重复率{repeat_rate:.0%})")
    elif count >= SPAM_MEDIUM_THRESHOLD[0] and repeat_rate >= SPAM_MEDIUM_THRESHOLD[1]:
        rule_scores.append(0.6)
        reasons.append(f"中度重复({count}条, 重复率{repeat_rate:.0%})")

    # 规则2：机器人模式
    if bot_score >= 0.7:
        rule_scores.append(0.8)
        reasons.append(f"机器人模式(评分{bot_score:.2f})")
    elif bot_score >= 0.4:
        rule_scores.append(0.5)
        reasons.append(f"疑似机器人(评分{bot_score:.2f})")

    # 规则3：内容高度相似但不完全相同（变种刷屏）
    if avg_similarity >= SPAM_VARIANT_SIMILARITY and count >= SPAM_VARIANT_MIN_COUNT:
        rule_scores.append(0.7)
        reasons.append(f"变种刷屏(相似度{avg_similarity:.0%})")

    # 规则4：短时间内爆发（滑动窗口口径）——窗口内最大条数达标即触发；
    # 旧口径用全时段平均间隔，「长期低频+某一刻爆发」的用户会被平均值稀释漏检。
    if burst_max >= SPAM_BURST_MIN_COUNT:
        rule_scores.append(0.75)
        reasons.append(f"高频爆发({SPAM_BURST_WINDOW_SECONDS}秒内最多{burst_max}条)")

    if rule_scores:
        spam_score = min(1.0, max(rule_scores) + SPAM_COMBO_BONUS * (len(rule_scores) - 1))
    else:
        spam_score = 0.0

    # 判定等级
    if spam_score >= 0.7:
        level = "高"
    elif spam_score >= 0.4:
        level = "中"
    else:
        level = "低"

    return {
        "count": count,
        "unique_count": unique_count,
        "repeat_rate": repeat_rate,
        "avg_similarity": avg_similarity,
        "avg_interval": avg_interval,
        "burst_max": burst_max,
        "bot_score": bot_score,
        "spam_score": spam_score,
        "spam_level": level,
        "reason": "; ".join(reasons) if reasons else "正常发言",
    }


def _percentile(values: list[float], q: float) -> float:
    """朴素分位数（线性插值）；values 非空"""
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _apply_relative_outliers(results: dict[str, dict]) -> None:
    """相对阈值补强（第二遍，原地修改）。

    绝对阈值对全体视频一刀切（500 条弹幕的小视频偏松、8 万条的大视频偏紧）；
    这里以本视频全池分布取相对离群：弹幕量与重复率双双超过全池 P95（且过绝对
    下限防小样本噪声）的「低」风险发送者保底提到中档，避免大水军池里的相对
    高 repeat 用户被绝对阈值漏掉。"""
    pool = [r for r in results.values() if r["count"] >= SPAM_RELATIVE_COUNT_FLOOR]
    if len(pool) < SPAM_RELATIVE_MIN_POOL:
        return
    p95_repeat = _percentile([r["repeat_rate"] for r in pool], 0.95)
    p95_count = _percentile([r["count"] for r in pool], 0.95)
    for r in results.values():
        if r["spam_level"] != "低":
            continue
        if (r["count"] >= max(p95_count, SPAM_RELATIVE_COUNT_FLOOR)
                and r["repeat_rate"] >= max(p95_repeat, SPAM_RELATIVE_REPEAT_FLOOR)):
            r["spam_score"] = max(r["spam_score"], SPAM_RELATIVE_SCORE)
            r["spam_level"] = "中"
            reason = f"相对离群(重复率{r['repeat_rate']:.0%}超全池P95={p95_repeat:.0%})"
            r["reason"] = reason if r["reason"] == "正常发言" else r["reason"] + "; " + reason


def batch_detect_spam(sender_groups: dict[str, dict]) -> dict[str, dict]:
    """
    批量检测所有发送者的刷屏程度（绝对规则 + 全池相对离群补强两遍）

    Args:
        sender_groups: {mid_hash: group_data}

    Returns:
        {mid_hash: spam_analysis_result}
    """
    results = {}
    for mid_hash, group in sender_groups.items():
        result = analyze_spam(
            group["contents"],
            group["timestamps"]
        )
        results[mid_hash] = result
    _apply_relative_outliers(results)
    return results


def distribution_stats(results: dict[str, dict]) -> dict:
    """全池分布自检（阈值校准参考）：弹幕数/重复率/刷屏分的 P50/P90/P95。

    输入为 batch_detect_spam 的结果；人工调阈值或误报数据积累后评估
    规则 precision 时，用这个分布判断当前阈值松紧。"""
    if not results:
        return {"senders": 0}
    counts = [r["count"] for r in results.values()]
    repeats = [r["repeat_rate"] for r in results.values()]
    scores = [r["spam_score"] for r in results.values()]
    return {
        "senders": len(results),
        "count_p50": _percentile(counts, 0.50),
        "count_p90": _percentile(counts, 0.90),
        "count_p95": _percentile(counts, 0.95),
        "repeat_p50": _percentile(repeats, 0.50),
        "repeat_p90": _percentile(repeats, 0.90),
        "repeat_p95": _percentile(repeats, 0.95),
        "score_p50": _percentile(scores, 0.50),
        "score_p90": _percentile(scores, 0.90),
        "score_p95": _percentile(scores, 0.95),
    }


def pool_distribution_from_rows(rows: list[dict]) -> dict:
    """从弹幕行直接算全池分布（web 概览页用，无需重跑完整 analyze_spam）：
    每发送者弹幕数/重复率的 P50/P90/P95。rows: [{mid_hash, content}]。"""
    per_sender: dict[str, list[str]] = {}
    for r in rows:
        mh = r.get("mid_hash") or ""
        if mh:
            per_sender.setdefault(mh, []).append(r.get("content") or "")
    if not per_sender:
        return {"senders": 0}
    counts = [len(v) for v in per_sender.values()]
    repeats = [1 - len(set(v)) / len(v) if v else 0.0 for v in per_sender.values()]
    return {
        "senders": len(per_sender),
        "count_p50": _percentile(counts, 0.50),
        "count_p95": _percentile(counts, 0.95),
        "repeat_p50": _percentile(repeats, 0.50),
        "repeat_p95": _percentile(repeats, 0.95),
    }


def detect_repeat_events(rows: list[dict],
                         window_seconds: int = REPEAT_EVENT_WINDOW_SECONDS,
                         min_senders: int = REPEAT_EVENT_MIN_SENDERS,
                         min_total: int = REPEAT_EVENT_MIN_TOTAL,
                         top_n: int = REPEAT_EVENT_TOP_N) -> list[dict]:
    """群体复读事件检测（全视频维度，补单人检测的最大盲区）。

    单人检测（analyze_spam）抓不到「一人一句的接龙/+1 队列」——每个发送者
    只发一两条、单看完全正常，合起来才是刷屏事件。这里按内容聚合全体弹幕，
    同一内容在 window_seconds 内被 ≥min_senders 个不同发送者发送且窗口内
    总条数 ≥min_total 即记一次事件（每内容只报峰值窗口，避免重叠窗口刷屏列表）。

    Args:
        rows: [{content, mid_hash, timestamp}]（timestamp 为真实发送时间戳）

    Returns:
        按发送者数降序的事件列表 [{content, sender_count, total, start, end}]，
        start/end 为窗口起止 Unix 时间戳。
    """
    by_content: dict[str, list[tuple[int, str]]] = {}
    for r in rows:
        ts = int(r.get("timestamp") or 0)
        content = r.get("content") or ""
        if ts > 0 and content:
            by_content.setdefault(content, []).append((ts, r.get("mid_hash") or ""))

    events = []
    for content, items in by_content.items():
        if len(items) < min_total:
            continue
        items.sort()
        best = None  # (发送者数, 总条数, 窗口起, 窗口止)
        left = 0
        senders_in_win: Counter = Counter()
        for right in range(len(items)):
            ts_r, mh_r = items[right]
            senders_in_win[mh_r] += 1
            while ts_r - items[left][0] > window_seconds:
                mh_l = items[left][1]
                senders_in_win[mh_l] -= 1
                if senders_in_win[mh_l] <= 0:
                    del senders_in_win[mh_l]
                left += 1
            total = right - left + 1
            n_senders = len(senders_in_win)
            if n_senders >= min_senders and total >= min_total:
                if best is None or (n_senders, total) > (best[0], best[1]):
                    best = (n_senders, total, items[left][0], ts_r)
        if best:
            events.append({"content": content, "sender_count": best[0],
                           "total": best[1], "start": best[2], "end": best[3]})

    events.sort(key=lambda e: (e["sender_count"], e["total"]), reverse=True)
    return events[:top_n]
