"""
刷屏智能检测模块

原则：不删除、不过滤，只标记和分析。
区分正常重复（如应援、玩梗）与恶意刷屏（垃圾内容、机器人）。
"""
import difflib
from collections import Counter
from typing import Tuple

from config import SPAM_HIGH_THRESHOLD, SPAM_MEDIUM_THRESHOLD


def content_similarity(a: str, b: str) -> float:
    """计算两条内容的相似度（0-1）"""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def analyze_content_repeat(contents: list[str]) -> Tuple[float, list[float]]:
    """
    分析内容重复率
    
    Returns:
        (整体重复率, 两两相似度列表)
    """
    n = len(contents)
    if n <= 1:
        return 0.0, []

    unique = set(contents)
    repeat_rate = 1 - len(unique) / n

    # 计算所有两两相似度
    similarities = []
    for i in range(n):
        for j in range(i + 1, n):
            sim = content_similarity(contents[i], contents[j])
            similarities.append(sim)

    return repeat_rate, similarities


def detect_bot_pattern(timestamps: list[int], video_times: list[float]) -> float:
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

    # 短时间内爆发（10秒内发送5条以上）
    burst_count = 0
    window = 10  # 10秒窗口
    for i in range(len(sorted_ts)):
        count_in_window = sum(1 for j in range(i, len(sorted_ts)) if sorted_ts[j] - sorted_ts[i] <= window)
        burst_count = max(burst_count, count_in_window)

    if burst_count >= 5:
        bot_score = max(bot_score, 0.7)
    elif burst_count >= 3:
        bot_score = max(bot_score, 0.4)

    return min(1.0, bot_score)


def analyze_spam(danmaku_contents: list[str], timestamps: list[int], video_times: list[float]) -> dict:
    """
    综合分析刷屏程度
    
    Returns:
        {
            "count": int,                # 弹幕总数
            "unique_count": int,         # 唯一内容数
            "repeat_rate": float,        # 重复率 0-1
            "avg_similarity": float,     # 平均内容相似度
            "avg_interval": float,       # 平均发送间隔（秒）
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
    repeat_rate, similarities = analyze_content_repeat(danmaku_contents)
    avg_similarity = sum(similarities) / len(similarities) if similarities else 0.0

    # 时间间隔
    if len(timestamps) >= 2:
        sorted_ts = sorted(timestamps)
        intervals = [sorted_ts[i] - sorted_ts[i - 1] for i in range(1, len(sorted_ts))]
        avg_interval = sum(intervals) / len(intervals)
    else:
        avg_interval = 0.0

    # 机器人检测
    bot_score = detect_bot_pattern(timestamps, video_times)

    # 综合刷屏评分
    spam_score = 0.0
    reasons = []

    # 规则1：大量重复内容
    if count >= SPAM_HIGH_THRESHOLD[0] and repeat_rate >= SPAM_HIGH_THRESHOLD[1]:
        spam_score = max(spam_score, 0.85)
        reasons.append(f"大量重复({count}条, 重复率{repeat_rate:.0%})")
    elif count >= SPAM_MEDIUM_THRESHOLD[0] and repeat_rate >= SPAM_MEDIUM_THRESHOLD[1]:
        spam_score = max(spam_score, 0.6)
        reasons.append(f"中度重复({count}条, 重复率{repeat_rate:.0%})")

    # 规则2：机器人模式
    if bot_score >= 0.7:
        spam_score = max(spam_score, 0.8)
        reasons.append(f"机器人模式(评分{bot_score:.2f})")
    elif bot_score >= 0.4:
        spam_score = max(spam_score, 0.5)
        reasons.append(f"疑似机器人(评分{bot_score:.2f})")

    # 规则3：内容高度相似但不完全相同（变种刷屏）
    if avg_similarity >= 0.8 and count >= 5:
        spam_score = max(spam_score, 0.7)
        reasons.append(f"变种刷屏(相似度{avg_similarity:.0%})")

    # 规则4：短时间内爆发
    if count >= 10 and avg_interval < 2:
        spam_score = max(spam_score, 0.75)
        reasons.append(f"高频爆发(间隔{avg_interval:.1f}s)")

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
        "bot_score": bot_score,
        "spam_score": spam_score,
        "spam_level": level,
        "reason": "; ".join(reasons) if reasons else "正常发言",
    }


def batch_detect_spam(sender_groups: dict[str, dict]) -> dict[str, dict]:
    """
    批量检测所有发送者的刷屏程度
    
    Args:
        sender_groups: {mid_hash: group_data}
    
    Returns:
        {mid_hash: spam_analysis_result}
    """
    results = {}
    for mid_hash, group in sender_groups.items():
        result = analyze_spam(
            group["contents"],
            group["timestamps"],
            group["video_times"]
        )
        results[mid_hash] = result
    return results
