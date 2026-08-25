"""
快速分析 - 只分析刷屏得分最高的前N个发送者
用法: python quick_test.py [BV号] [--top N]
"""
import sys, os
import argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# 强制行缓冲：输出被重定向/管道时也能实时看到进度（默认块缓冲会长时间无输出）
sys.stdout.reconfigure(line_buffering=True)

from config import LLM_API_KEY, HISTORY_DANMAKU_ENABLED
from auth import get_auth_client
from api_client import RiskControlError
from combo_pool import build_pool
from danmaku import collect_danmaku_data
from spam_detector import batch_detect_spam
from cringe_detector import detect_cringe_danmaku
from comment import collect_comment_data
from uid_resolver import resolve_sender
from user_collector import collect_user_data
from profile_analyzer import analyze_profile
from llm_analyzer import LLMAnalyzer
from storage import init_db, save_video_info, save_danmaku, save_comments
from main import _merge_history_danmaku
from web_autostart import maybe_launch_web


def main():
    parser = argparse.ArgumentParser(description="快速分析 - 只分析刷屏得分最高的前N个发送者")
    parser.add_argument("bvid", nargs="?", default="BV1ebg16jEhp",
                        help="视频BV号 (默认 BV1ebg16jEhp)")
    parser.add_argument("--top", "-n", type=int, default=1,
                        help="分析刷屏得分最高的前N个发送者 (默认 1)")
    args = parser.parse_args()
    bvid = args.bvid
    top_n = max(1, args.top)  # 夹紧下限：--top 0/负数无意义

    print(f"🎯 快速分析: {bvid}  (刷屏 Top {top_n})")
    print(f"   策略: 全量弹幕 → 刷屏检测 → 只解 Top{top_n} UID\n")

    # 1. 登录
    print("[1/6] 登录...")
    client = get_auth_client()
    # 账号×IP 组合池（同主流程：风控换号+切节点，故障自动降级）
    pool = build_pool(client)

    # 2. 采集全部弹幕
    print("[2/6] 采集弹幕...")
    video_info, danmaku_list, sender_groups = collect_danmaku_data(bvid, pool)
    print(f"   视频: {video_info.get('title')}")

    # 对齐主流程：开启历史弹幕时合并每日弹幕池快照，保证刷屏 top-N 口径与 run.py 一致
    if HISTORY_DANMAKU_ENABLED:
        danmaku_list, sender_groups = _merge_history_danmaku(video_info, danmaku_list, pool)

    # 弹幕落库（供 web.py 弹幕浏览器查询；失败只警告不中断冒烟流程）
    try:
        init_db()
        save_video_info(bvid, video_info)
        save_danmaku(bvid, danmaku_list)
    except Exception as e:
        print(f"   警告: 弹幕落库失败（{e}），web.py 中将无该视频数据")

    print(f"   弹幕: {len(danmaku_list)} 条, 发送者: {len(sender_groups)} 人")

    # 3. 刷屏检测 + 问题弹幕检测 → 兴趣分 Top N（对齐主流程兴趣口径）
    print("[3/6] 刷屏检测 + 问题弹幕检测...")
    spam_results = batch_detect_spam(sender_groups)
    try:
        cringe_results = detect_cringe_danmaku(danmaku_list, sender_groups, video_info) if LLM_API_KEY else {}
    except Exception as e:
        # 对齐主流程 phase_cringe：问题弹幕检测失败只警告不中断，降级为空结果
        print(f"   问题弹幕检测失败（{e}），降级跳过")
        cringe_results = {}
    scored = [
        (mid_hash, r["spam_score"], r["spam_level"])
        for mid_hash, r in spam_results.items()
    ]
    scored.sort(key=lambda x: (x[1], cringe_results.get(x[0], {}).get("max_severity", 0)),
                reverse=True)
    targets = scored[:top_n]
    for i, (mid, score, level) in enumerate(targets, 1):
        grp = sender_groups[mid]
        print(f"   #{i} mid_hash={mid} score={score:.2f} level={level} 弹幕{grp['count']}条")

    # 4. 收集评论
    print("[4/6] 收集评论...")
    comments = []
    try:
        comments, comment_uid_map, _ = collect_comment_data(video_info.get("aid", 0), pool)
    except Exception as e:
        # 对齐主流程 phase_comment：评论采集失败降级为仅用CRC32破解，只警告不中断
        print(f"   评论采集失败 (将仅用CRC32破解): {e}")
        comment_uid_map = {}
    uid_comments: dict[int, list] = {}
    for c in comments:
        uid_comments.setdefault(c["uid"], []).append(c)
    for lst in uid_comments.values():
        lst.sort(key=lambda x: x.get("like", 0), reverse=True)
    # 评论落库（跨视频足迹数据源，幂等去重；失败只警告不中断）
    try:
        save_comments(bvid, comments)
    except Exception as e:
        print(f"   警告: 评论落库失败（{e}），跨视频足迹将缺评论")

    # 5. 逐个解析 + 采集 + 画像 + AI
    profiles = []
    for rank, (mid_hash, score, level) in enumerate(targets, 1):
        print(f"\n{'='*40}")
        print(f"  #{rank} mid_hash={mid_hash}")
        group = sender_groups[mid_hash]

        # 解析 UID
        uid, confidence, method, _, collision_risk, _ = resolve_sender(
            mid_hash, group["contents"], comment_uid_map, pool
        )
        if not uid:
            print(f"  ❌ UID 解析失败!")
            continue
        risk_note = " ⚠️可能误识别" if collision_risk else ""
        print(f"  ✅ UID={uid} (方法: {method}, 置信度: {confidence}){risk_note}")

        # 采集数据（对齐主流程：采集失败跳过该用户，不生成幽灵画像）
        try:
            user_data = collect_user_data(uid, pool)
        except Exception as e:
            print(f"  ❌ 用户数据采集失败: {e}")
            continue
        if "error" in user_data:
            print(f"  ❌ 用户数据采集失败: {user_data['error']}")
            continue
        dm_stats = {"count": group["count"], "contents": group["contents"], "video_times": group.get("video_times", [])}
        spam = spam_results.get(mid_hash, {})
        profile = analyze_profile(user_data, dm_stats, spam)
        profile["collision_risk"] = collision_risk
        profile["comments"] = uid_comments.get(uid, [])[:10]
        profile["cringe"] = cringe_results.get(mid_hash, {})
        profiles.append(profile)

    # 6. AI 重点深掘（对齐主流程：7a 粗筛已砍，只保留 top K 深掘）
    if profiles and LLM_API_KEY:
        print(f"\n[6/6] AI 重点深掘 ({len(profiles)}人)...")
        try:
            analyzer = LLMAnalyzer()
            deep = analyzer.analyze_deep(profiles, video_info, top_k=top_n)
            for p in profiles:
                uid = p.get("uid")
                if uid in deep:
                    p["ai_deep"] = deep[uid]
        except Exception as e:
            print(f"   AI 分析失败: {e}")

    # 静态 HTML 报告已被 web.py 交互式报告完全替换
    print(f"\n✅ 分析完成: {len(profiles)} 人生成画像")
    # 自动启动 web.py 并打开报告页（冒烟场景不阻塞结束：失败只打印 URL 降级）
    maybe_launch_web(bvid)


if __name__ == "__main__":
    try:
        main()
    except RiskControlError as e:
        # 对齐 main.py：组合池兜底耗尽（整圈账号全风控+长冷却后仍失败）时非零退出
        print(f"\n[退出] 风控兜底耗尽（{e}），快速分析终止")
        raise SystemExit(1)
