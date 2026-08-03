"""
快速分析 - 只分析刷屏得分最高的前N个发送者
用法: python quick_test.py [BV号] [--top N]
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from auth import get_auth_client
from danmaku import collect_danmaku_data
from spam_detector import batch_detect_spam
from comment import collect_comment_data
from uid_resolver import resolve_sender
from user_collector import collect_user_data
from profile_analyzer import analyze_profile
from llm_analyzer import LLMAnalyzer
from report import save_report


def main():
    bvid = "BV1vu4y1b7Y9"
    top_n = 1
    for a in sys.argv[1:]:
        if a.startswith("BV"):
            bvid = a
        elif a == "--top" or a == "-n":
            continue
        else:
            try:
                top_n = int(a)
            except ValueError:
                pass

    print(f"🎯 快速分析: {bvid}  (刷屏 Top {top_n})")
    print(f"   策略: 全量弹幕 → 刷屏检测 → 只解 Top{top_n} UID\n")

    # 1. 登录
    print("[1/6] 登录...")
    client = get_auth_client()

    # 2. 采集全部弹幕
    print("[2/6] 采集弹幕...")
    video_info, danmaku_list, sender_groups = collect_danmaku_data(bvid, client)
    print(f"   视频: {video_info.get('title')}")
    print(f"   弹幕: {len(danmaku_list)} 条, 发送者: {len(sender_groups)} 人")

    # 3. 刷屏检测 → 取 Top N
    print("[3/6] 刷屏检测...")
    spam_results = batch_detect_spam(sender_groups)
    scored = [
        (mid_hash, r["spam_score"], r["spam_level"])
        for mid_hash, r in spam_results.items()
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    targets = scored[:top_n]
    for i, (mid, score, level) in enumerate(targets, 1):
        grp = sender_groups[mid]
        print(f"   #{i} mid_hash={mid} score={score:.2f} level={level} 弹幕{grp['count']}条")

    # 4. 收集评论
    print("[4/6] 收集评论...")
    try:
        _, comment_uid_map = collect_comment_data(video_info.get("aid", 0), client)
    except Exception:
        comment_uid_map = {}

    # 5. 逐个解析 + 采集 + 画像 + AI
    profiles = []
    for rank, (mid_hash, score, level) in enumerate(targets, 1):
        print(f"\n{'='*40}")
        print(f"  #{rank} mid_hash={mid_hash}")
        group = sender_groups[mid_hash]

        # 解析 UID
        uid, confidence, method, _ = resolve_sender(
            mid_hash, group["contents"], comment_uid_map, client
        )
        if not uid:
            print(f"  ❌ UID 解析失败!")
            continue
        print(f"  ✅ UID={uid} (方法: {method})")

        # 采集数据
        user_data = collect_user_data(uid, client)
        dm_stats = {"count": group["count"], "contents": group["contents"], "video_times": group.get("video_times", [])}
        spam = spam_results.get(mid_hash, {})
        profile = analyze_profile(user_data, dm_stats, spam)
        profiles.append(profile)

    # 6. AI 分析（批量）
    if profiles and os.environ.get("LLM_API_KEY"):
        print(f"\n[6/6] AI 画像分析 ({len(profiles)}人)...")
        try:
            analyzer = LLMAnalyzer()
            result = analyzer.analyze(profiles, video_info, batch_size=10)
            per_user = result.get("per_user", {})
            for p in profiles:
                uid = p.get("uid")
                if uid in per_user:
                    p["ai_analysis"] = per_user[uid]
        except Exception as e:
            print(f"   AI 分析失败: {e}")

    # 报告
    report_path = save_report(video_info, profiles)
    print(f"\n✅ 报告: {report_path}")


if __name__ == "__main__":
    main()
