"""
B站弹幕发送者用户画像分析系统 — 主控流程

用法:
    python src/main.py BVxxxxxxxx [--force]
    python src/main.py --batch videos.txt   # 批量模式：逐行读取BV号（忽略空行与 # 注释行）
    --force: 清除该视频的全部缓存（senders/孤立 users/videos），强制重新采集全部用户
"""
import sys
import os
import argparse

from config import MAX_ANALYZE_USERS, LLM_API_KEY, HISTORY_DANMAKU_ENABLED
from storage import init_db, save_video_info, save_sender, save_user_data
from storage import load_user_data, has_user_data, load_senders
from storage import clear_video_cache, update_sender_spam, save_global_uid, load_global_uid_map
from auth import get_auth_client
from danmaku import collect_danmaku_data, get_top_senders, group_by_sender, get_cid_for_page
from danmaku_history import fetch_history_danmaku
from comment import collect_comment_data, fetch_charge_uid_map
from uid_resolver import resolve_all_senders, METHOD_CRC32_CRACK, METHOD_COMMENT_VERIFY
from spam_detector import batch_detect_spam
from user_collector import collect_user_data
from profile_analyzer import analyze_profile
from llm_analyzer import LLMAnalyzer
from report import save_report
from exporter import export_csv, export_json


def print_banner():
    print("=" * 60)
    print("  B站弹幕发送者深度画像分析系统")
    print("=" * 60)
    print()


def phase_login():
    """阶段1: 扫码登录"""
    print("[Phase 1/6] 扫码登录...")
    return get_auth_client()


def phase_danmaku(bvid: str, client):
    """阶段2: 采集弹幕（实时弹幕池 + 可选历史弹幕快照合并）"""
    print("\n[Phase 2/6] 采集弹幕数据...")
    video_info, danmaku_list, sender_groups = collect_danmaku_data(bvid, client)
    save_video_info(bvid, video_info)

    # 实时弹幕池只保留最近几千条；开启历史弹幕时拉取每日弹幕池快照补全历史
    if HISTORY_DANMAKU_ENABLED:
        danmaku_list, sender_groups = _merge_history_danmaku(video_info, danmaku_list, client)

    return video_info, danmaku_list, sender_groups


def _merge_history_danmaku(video_info: dict, danmaku_list: list[dict], client):
    """拉取历史弹幕快照并与实时池合并，全局按 dmid 去重后重新聚合发送者。

    历史 seg.so 返回的是"截至某日期的最新1000条弹幕池快照"，相邻日快照大量重叠，
    原始合并结果含重复 dmid，必须先全局去重再 group_by_sender，否则发送者计数虚高。
    实时池优先：其 weight/pool 等字段更全，历史快照中重复 dmid 直接丢弃；
    dmid=0 的弹幕无法判重，按"不删除数据"约定保留。
    历史采集失败降级为仅实时池，不中断主流程。
    """
    try:
        # 多分P视频仅采集第1P的历史弹幕（历史接口按 cid 逐日拉取，逐P回溯成本高）
        cid = get_cid_for_page(video_info, 0)
        pubdate = video_info.get("pubdate", 0)
        history_list = fetch_history_danmaku(cid, client, pubdate)
    except Exception as e:
        print(f"[Main] 警告：历史弹幕采集失败，降级为仅实时弹幕池: {e}")
        return danmaku_list, group_by_sender(danmaku_list)

    if not history_list:
        print("[Main] 历史弹幕为空，使用实时弹幕池")
        return danmaku_list, group_by_sender(danmaku_list)

    merged = []
    seen_dmids = set()
    for dm in danmaku_list:  # 实时池优先入列
        merged.append(dm)
        if dm.get("dmid"):
            seen_dmids.add(dm["dmid"])
    history_new = 0
    for dm in history_list:
        dmid = dm.get("dmid", 0)
        if dmid:
            if dmid in seen_dmids:
                continue  # 与实时池或前序日快照重复，丢弃
            seen_dmids.add(dmid)
        merged.append(dm)
        history_new += 1

    print(f"[Main] 实时池 {len(danmaku_list)} 条 + 历史快照 {len(history_list)} 条"
          f"（去重后 {history_new} 条），合并后共 {len(merged)} 条弹幕")
    print("[Main] 提示：历史弹幕为每日弹幕池快照（每日上限1000条），热门期弹幕滚动快，可能不完整")

    # 覆盖率统计写入 video_info，供报告头部展示（降级/未启用历史弹幕时不设置）
    video_info["danmaku_coverage"] = {
        "realtime": len(danmaku_list),
        "history": len(history_list),
        "history_new": history_new,
        "merged": len(merged),
    }

    return merged, group_by_sender(merged)


def phase_comment(video_info: dict, client):
    """阶段3: 采集评论 + 充电名单（失败不影响后续流程）"""
    print("\n[Phase 3/6] 采集评论区数据...")
    aid = video_info.get("aid", 0)
    if not aid:
        print("[Phase 3] 警告: 未获取到有效 aid，跳过评论采集（将仅用MITM破解）")
        return [], {}, {}, {}
    comments, comment_uid_map, comment_location_map = [], {}, {}
    try:
        comments, comment_uid_map, comment_location_map = collect_comment_data(aid, client)
    except Exception as e:
        print(f"[Phase 3] 评论采集失败 (将仅用其他来源): {e}")
    # 充电名单（独立降级：评论失败也照常尝试）
    up_mid = (video_info.get("owner") or {}).get("mid", 0)
    charge_uid_map = {}
    if up_mid:
        charge_uid_map = fetch_charge_uid_map(video_info.get("bvid", ""), aid, up_mid, client)
    return comments, comment_uid_map, comment_location_map, charge_uid_map


def phase_resolve(bvid: str, sender_groups: dict, comment_uid_map: dict, client, max_users: int = MAX_ANALYZE_USERS, charge_uid_map: dict | None = None):
    """阶段4: 解析发送者UID（支持数据库缓存 + 按弹幕数取top N）"""
    print("\n[Phase 4/6] 解析发送者UID...")

    # 1. 从数据库加载已缓存的解析结果
    cached = load_senders(bvid)
    cached_map = {r["mid_hash"]: r for r in cached}
    print(f"[Phase 4] 数据库缓存: {len(cached_map)} 个已解析")

    # 1.5 全局 mid_hash→UID 映射库（跨视频累积）：与当视频评论映射合并，
    #     评论验证优先，全局库兜底；method_map 标注每个 mid_hash 的来源
    global_map = load_global_uid_map()
    plain_uid_map = dict(comment_uid_map)  # 评论映射复制为底
    method_map = {h: METHOD_COMMENT_VERIFY for h in comment_uid_map}
    # 1.6 充电名单合并（明文证据，置信度同评论验证；评论优先）
    charge_hit = 0
    for h, uid in (charge_uid_map or {}).items():
        if h not in plain_uid_map:
            plain_uid_map[h] = uid
            method_map[h] = "充电名单"
            charge_hit += 1
    if charge_hit:
        print(f"[Phase 4] 充电名单: 补充 {charge_hit} 条到交叉验证映射")
    global_hit = 0
    for h, ent in global_map.items():
        if h not in plain_uid_map:
            plain_uid_map[h] = ent["uid"]
            method_map[h] = ent["source"]
            global_hit += 1
    print(f"[Phase 4] 全局映射库: {len(global_map)} 条（补充 {global_hit} 条到交叉验证映射）")

    # 2. 筛选需要新解析的sender：不在缓存中的，以及缓存中 uid 为 NULL 的历史解析失败记录
    #    （评论交叉验证随新评论变好，失败记录值得重试；save_sender 为 INSERT OR REPLACE 会覆盖旧记录）
    unresolved = {}
    retry_failed = 0
    for mid_hash, group in sender_groups.items():
        cached_row = cached_map.get(mid_hash)
        if cached_row is None:
            unresolved[mid_hash] = group
        elif cached_row["uid"] is None:
            unresolved[mid_hash] = group
            retry_failed += 1

    if retry_failed > 0:
        print(f"[Phase 4] {retry_failed} 个历史解析失败的发送者将重试")

    # 3. 按弹幕数降序排序，只取 top max_users 个
    sorted_unresolved = sorted(unresolved.items(), key=lambda x: x[1]["count"], reverse=True)
    to_resolve = sorted_unresolved[:max_users]

    skipped = len(sorted_unresolved) - len(to_resolve)
    if skipped > 0:
        print(f"[Phase 4] 跳过 {skipped} 个低弹幕发送者（超出 --max-users 限制）")

    # 4. 只解析 top N 未缓存的发送者
    if to_resolve:
        to_resolve_dict = dict(to_resolve)
        print(f"[Phase 4] 需新解析: {len(to_resolve_dict)} 个发送者")
        new_resolved = resolve_all_senders(to_resolve_dict, plain_uid_map, client,
                                           method_map=method_map)

        # 保存新解析结果到数据库
        for mid_hash, info in new_resolved.items():
            save_sender(
                bvid=bvid,
                mid_hash=mid_hash,
                uid=info["uid"],
                confidence=info["confidence"],
                method=info["method"],
                danmaku_count=info["danmaku_count"],
                contents=info["contents"],
                spam_level=info.get("spam_level", "低"),
                spam_score=0.0
            )
            # 沉淀到全局映射库：多候选取最小的碰撞风险条目不入库（spec 4.2）
            if info["uid"] is not None and not (
                    info["method"] == METHOD_CRC32_CRACK and len(info.get("candidates", [])) > 1):
                save_global_uid(mid_hash, info["uid"], info["method"])
    else:
        new_resolved = {}
        print("[Phase 4] 无需新解析，全部命中缓存")

    # 5. 合并缓存 + 新解析结果（新解析优先；缓存中 uid 为 NULL 且本轮未重试的记录视为未解析，不并入）
    resolved = {}
    for mid_hash, group in sender_groups.items():
        if mid_hash in new_resolved:
            resolved[mid_hash] = new_resolved[mid_hash]
        elif mid_hash in cached_map and cached_map[mid_hash]["uid"] is not None:
            c = cached_map[mid_hash]
            # 缓存结果不含 collision_risk 字段，从 method 推断（不改表结构）；
            # 历史缓存中暴力破解路径可能被标"高"，按现行策略压为"中"
            is_crack = c["method"] == METHOD_CRC32_CRACK
            confidence = c["confidence"]
            if is_crack and confidence == "高":
                confidence = "中"
            resolved[mid_hash] = {
                "uid": c["uid"],
                "confidence": confidence,
                "method": c["method"],
                "user_info": {},
                "danmaku_count": c["danmaku_count"],
                "contents": c["contents"],
                "spam_level": c.get("spam_level", "低"),
                "spam_score": c.get("spam_score", 0.0),
                "collision_risk": is_crack,
            }

    # 只统计本轮 sender_groups 范围内、确实来自缓存的命中数（重试成功的不重复计入）
    cached_success = sum(1 for h, v in resolved.items()
                         if v.get("uid") and h in cached_map and h not in new_resolved)
    new_success = sum(1 for v in new_resolved.values() if v.get("uid"))
    total = len(resolved)
    print(f"\n[Phase 4] 解析完成: 缓存命中 {cached_success} + 新解析 {new_success} = {cached_success + new_success}/{total}")
    return resolved


def phase_spam(bvid: str, sender_groups: dict) -> dict:
    """阶段4.5: 刷屏检测（检测完成后将真实结果回写数据库，修正阶段4落库时的占位值）"""
    print("\n[Phase 4.5/6] 刷屏行为检测...")
    spam_results = batch_detect_spam(sender_groups)
    high_spam = sum(1 for v in spam_results.values() if v["spam_level"] == "高")
    med_spam = sum(1 for v in spam_results.values() if v["spam_level"] == "中")
    print(f"[Phase 4.5] 检测完成: 高风险 {high_spam} | 中风险 {med_spam}")

    # 回写所有 sender 的真实检测结果（阶段4 save_sender 时检测尚未运行，库中是 "低"/0.0 占位值）
    for mid_hash, result in spam_results.items():
        update_sender_spam(bvid, mid_hash, result["spam_level"], result["spam_score"])
    print(f"[Phase 4.5] 已回写 {len(spam_results)} 个发送者的刷屏检测结果")
    return spam_results


def phase_collect_users(resolved: dict, client, max_users: int = MAX_ANALYZE_USERS, force: bool = False):
    """阶段5: 深度采集用户数据（成功立即落库可断点续采；force=True 跳过缓存强制重采）"""
    print("\n[Phase 5/6] 深度采集用户信息...")

    # 筛选需要采集的用户（有UID且置信度 acceptable）
    uids_to_collect = []
    for mid_hash, info in resolved.items():
        uid = info.get("uid")
        confidence = info.get("confidence", "无")
        if uid and confidence in ("高", "中"):
            uids_to_collect.append((mid_hash, uid))

    # 按弹幕数量降序，限制最大分析数
    # 这里我们没法直接排序，因为resolved没有原始count，但我们可以 trust danmaku_count
    uids_to_collect.sort(key=lambda x: resolved[x[0]]["danmaku_count"], reverse=True)

    # 同一 uid 可能被多个 mid_hash 命中（如评论交叉验证与 CRC32 破解指向同一人），
    # 去重后只采集一次，后续 mid_hash 在阶段6复用 user_data_map 中同一份数据
    seen_uids = set()
    deduped = []
    for mid_hash, uid in uids_to_collect:
        if uid in seen_uids:
            continue
        seen_uids.add(uid)
        deduped.append((mid_hash, uid))
    skipped_dup = len(uids_to_collect) - len(deduped)
    if skipped_dup > 0:
        print(f"[Phase 5] 去重: {skipped_dup} 个 mid_hash 指向重复 UID，合并采集")
    uids_to_collect = deduped[:max_users]

    total = len(uids_to_collect)
    print(f"[Phase 5] 需采集用户: {total} 人 (上限 {max_users})")

    user_data_map = {}
    processed = set()

    for idx, (mid_hash, uid) in enumerate(uids_to_collect, 1):
        print(f"\n  [{idx}/{total}] 采集 UID:{uid}...")

        # 检查是否已缓存（--force 时跳过缓存强制重采，结果覆盖写 users 表）
        if not force and has_user_data(uid):
            cached = load_user_data(uid)
            if cached:
                user_data_map[uid] = cached[0]
                print(f"  [缓存] 使用已采集数据")
                processed.add(uid)
                continue

        try:
            data = collect_user_data(uid, client)
            if "error" not in data:
                user_data_map[uid] = data
                processed.add(uid)
                # 立即落库：Ctrl+C 中断后已采集数据不丢失，重跑时命中上方缓存跳过。
                # profile 暂存空 dict，阶段6分析后以 INSERT OR REPLACE 覆盖
                save_user_data(uid, data.get("name", ""), data.get("level", 0), data, {})
            else:
                # 失败不落库，重跑时会重新采集
                print(f"  [失败] {data['error']}")
        except Exception as e:
            print(f"  [异常] {e}")

    print(f"\n[Phase 5] 采集完成: {len(processed)}/{total}")
    return user_data_map


def phase_analyze(resolved: dict, spam_results: dict, user_data_map: dict, sender_groups: dict,
                  comment_location_map: dict | None = None):
    """阶段6: 画像分析

    comment_location_map（uid→评论IP属地）在此处注入 user_data 而非依赖落库数据：
    users 表缓存的旧 user_data 没有该字段，每次运行时注入才能保证缓存命中路径也带出属地。
    """
    print("\n[Phase 6/6] 画像分析...")
    comment_location_map = comment_location_map or {}
    profiles = []

    for mid_hash, info in resolved.items():
        uid = info.get("uid")
        if not uid or uid not in user_data_map:
            continue

        user_data = user_data_map[uid]

        # 评论IP属地贯通：原样保留 API 返回格式（如 "IP属地：江苏"），无属地则不设该键
        if uid in comment_location_map:
            user_data["ip_location"] = comment_location_map[uid]
        spam = spam_results.get(mid_hash, {})

        # 构建弹幕统计
        danmaku_stats = {
            "count": info["danmaku_count"],
            "contents": info["contents"],
            "video_times": sender_groups.get(mid_hash, {}).get("video_times", []),
        }

        # 逐人容错：单个用户分析/落库失败只跳过该用户，不中断整个阶段（对齐阶段5粒度）
        try:
            profile = analyze_profile(user_data, danmaku_stats, spam)
            # 碰撞风险标记传入报告，供"可能误识别"徽标展示
            profile["collision_risk"] = info.get("collision_risk", False)
            profiles.append(profile)

            # 保存到数据库
            save_user_data(uid, user_data.get("name", ""), user_data.get("level", 0), user_data, profile)
        except Exception as e:
            print(f"  [Phase 6] 警告: UID:{uid} 画像分析失败，已跳过: {e}")

    print(f"[Phase 6] 生成 {len(profiles)} 份画像")
    return profiles


def phase_ai_analysis(video_info: dict, profiles: list[dict]) -> dict | None:
    """阶段7: LLM 深度画像分析"""
    if not LLM_API_KEY:
        print("\n[Phase 7/7] 跳过 (未在 config.py 或环境变量中设置 LLM_API_KEY)")
        return None

    print("\n[Phase 7/7] LLM 逐人画像分析...")
    try:
        analyzer = LLMAnalyzer()
        result = analyzer.analyze(profiles, video_info)
        per_user_count = len(result.get("per_user", {}))
        print(f"[Phase 7] 完成: {per_user_count}/{len(profiles)} 人生成AI画像")
        return result
    except Exception as e:
        print(f"[Phase 7] LLM 分析失败: {e}")
        return None


def run_analysis(bvid: str, force: bool = False, max_users: int = MAX_ANALYZE_USERS):
    """
    执行完整分析流程
    """
    print_banner()

    # 初始化数据库
    init_db()

    # 阶段1: 登录
    client = phase_login()

    # --force: 登录成功后清除该视频的全部缓存，后续阶段全部重新采集
    # （放在登录后，避免登录失败/取消时缓存已清但新数据未采）
    if force:
        clear_video_cache(bvid)
        print(f"[Main] --force 已清除 {bvid} 的缓存，全部重新采集")

    # 阶段2: 弹幕
    video_info, danmaku_list, sender_groups = phase_danmaku(bvid, client)
    aid = video_info.get("aid", 0)

    # 弹幕为空时提前终止：后续评论/解析/画像均无意义，避免白跑全流程产出空报告
    if not danmaku_list:
        print("[Main] 弹幕为空，终止分析")
        return

    # 阶段3: 评论 + 充电名单（comment_location_map 为 uid→IP属地，阶段6贯通进画像）
    comments, comment_uid_map, comment_location_map, charge_uid_map = phase_comment(video_info, client)

    # 阶段4: UID解析
    resolved = phase_resolve(bvid, sender_groups, comment_uid_map, client,
                             max_users=max_users, charge_uid_map=charge_uid_map)

    # 阶段4.5: 刷屏检测
    spam_results = phase_spam(bvid, sender_groups)

    # 合并刷屏数据到resolved
    for mid_hash in resolved:
        if mid_hash in spam_results:
            resolved[mid_hash]["spam_level"] = spam_results[mid_hash]["spam_level"]
            resolved[mid_hash]["spam_score"] = spam_results[mid_hash]["spam_score"]

    # 阶段5: 用户采集
    user_data_map = phase_collect_users(resolved, client, max_users=max_users, force=force)

    # 阶段6: 画像分析（评论IP属地在此贯通进画像）
    profiles = phase_analyze(resolved, spam_results, user_data_map, sender_groups, comment_location_map)

    # 阶段7: LLM AI 逐人画像分析
    ai_analysis = phase_ai_analysis(video_info, profiles)

    # 将逐人AI分析注入profiles
    if ai_analysis:
        per_user = ai_analysis.get("per_user", {})
        for p in profiles:
            uid = p.get("uid")
            if uid in per_user:
                p["ai_analysis"] = per_user[uid]

    # 生成报告
    print("\n[Report] 生成HTML报告...")
    report_path = save_report(video_info, profiles)
    print(f"[Report] 报告已保存: {report_path}")

    # 同步导出 CSV/JSON：与 HTML 报告同前缀（复用同一时间戳），
    # 导出失败只打印警告降级，不影响已生成的 HTML 报告
    export_base = os.path.splitext(report_path)[0]
    try:
        csv_path = export_base + ".csv"
        export_csv(profiles, csv_path)
        print(f"[Export] CSV 已导出: {csv_path}")
    except Exception as e:
        print(f"[Export] 警告: CSV 导出失败（不影响HTML报告）: {e}")
    try:
        json_path = export_base + ".json"
        export_json(video_info, profiles, json_path)
        print(f"[Export] JSON 已导出: {json_path}")
    except Exception as e:
        print(f"[Export] 警告: JSON 导出失败（不影响HTML报告）: {e}")

    print("\n" + "=" * 60)
    print("  分析完成!")
    print(f"  视频: {video_info.get('title', '')}")
    print(f"  分析用户: {len(profiles)} 人")
    print(f"  报告: {report_path}")
    print("=" * 60)


def load_batch_bvids(path: str) -> list[str]:
    """读取批量 BV 号清单：逐行读取，忽略空行与 # 注释行，去重保持顺序"""
    bvids = []
    seen = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line in seen:
                continue
            seen.add(line)
            bvids.append(line)
    return bvids


def run_batch(batch_file: str, force: bool = False, max_users: int = MAX_ANALYZE_USERS):
    """批量分析：逐个视频调用 run_analysis，单个失败只警告不中断，最后打印汇总"""
    bvids = load_batch_bvids(batch_file)
    if not bvids:
        print(f"[Batch] 清单为空（无有效BV号）: {batch_file}")
        return

    total = len(bvids)
    print(f"[Batch] 共 {total} 个视频待分析（清单: {batch_file}）")

    succeeded = []
    failed = []
    for idx, bvid in enumerate(bvids, 1):
        print(f"\n{'=' * 60}")
        print(f"  [Batch {idx}/{total}] {bvid}")
        print(f"{'=' * 60}")
        if not bvid.startswith("BV"):
            print(f"[Batch] 警告: {bvid} 格式不正确（应以 BV 开头），跳过")
            failed.append(bvid)
            continue
        try:
            run_analysis(bvid, force=force, max_users=max_users)
            succeeded.append(bvid)
        except KeyboardInterrupt:
            # Ctrl+C 不再继续后续视频，但仍打印已完成的汇总
            print(f"\n[Batch] 用户中断，跳过后续 {total - idx} 个视频")
            failed.append(bvid)
            break
        except Exception as e:
            print(f"\n[Batch] 警告: {bvid} 分析失败，继续下一个: {e}")
            failed.append(bvid)

    # 汇总
    print(f"\n{'=' * 60}")
    print(f"  批量分析汇总: 成功 {len(succeeded)} / 失败 {len(failed)} / 共 {total}")
    if failed:
        print(f"  失败列表: {', '.join(failed)}")
    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(description="B站弹幕发送者用户画像分析")
    parser.add_argument("bvid", nargs="?", help="视频BV号，如 BV1vu4y1b7Y9")
    parser.add_argument("--force", action="store_true",
                        help="清除该视频的缓存并强制重采全部用户（忽略断点续采）")
    parser.add_argument("--max-users", type=int, default=MAX_ANALYZE_USERS,
                        help=f"最大分析用户数 (默认 {MAX_ANALYZE_USERS})")
    parser.add_argument("--batch", metavar="FILE",
                        help="批量模式：从文件逐行读取BV号（忽略空行与 # 注释行）")
    args = parser.parse_args()

    if args.batch:
        try:
            run_batch(args.batch, force=args.force, max_users=args.max_users)
        except OSError as e:
            print(f"错误: 批量清单文件不可读: {args.batch} ({e})")
            sys.exit(1)
        return

    if not args.bvid:
        print("错误: 需提供 BV号 或 --batch 清单文件")
        sys.exit(1)

    bvid = args.bvid.strip()
    if not bvid.startswith("BV"):
        print("错误: BV号格式不正确，应以 BV 开头")
        sys.exit(1)

    try:
        run_analysis(bvid, force=args.force, max_users=args.max_users)
    except KeyboardInterrupt:
        print("\n\n[Exit] 用户中断，进度已保存，可重新运行恢复")
    except Exception as e:
        print(f"\n[Error] 分析失败: {e}")
        raise


if __name__ == "__main__":
    main()
