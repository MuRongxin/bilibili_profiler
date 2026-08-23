#!/usr/bin/env python3
"""
交互式 Web 数据报告 —— Flask 本地服务

静态单文件 HTML 报告已被本服务完全替换：页面骨架/用户卡片服务端渲染
（复用 report.py 渲染函数），弹幕浏览器走 JSON API（/api/video/<bvid>/danmaku）。

用法:
    python web.py                      # 监听 127.0.0.1:8000
    PROFILER_PORT=9000 python web.py   # 环境变量覆盖端口
"""
import sys
import os
import json
import glob
import re
import argparse
import atexit
import signal
import sqlite3
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from contextlib import closing

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from flask import Flask, abort, jsonify, request, send_from_directory

from config import (REPORT_DIR, DATA_DIR, LLM_API_KEY, HISTORY_MAX_MONTHS, HISTORY_MAX_DAYS,
                     MAX_FOOTPRINT_VIDEOS, MAX_FOOTPRINT_DANMAKU_SAMPLES,
                     MAX_FOOTPRINT_COMMENT_SAMPLES,
                     HOT_COMMENT_MIN_REPLIES, HOT_COMMENT_MAX_SHOW,
                     USER_TIMELINE_MAX_VIDEOS, USER_TIMELINE_SAMPLES,
                     CROSS_VIDEO_MIN_VIDEOS, CROSS_VIDEO_MAX_USERS, DENSITY_BUCKETS,
                     COMMENT_HEAT_REPLY_WEIGHT, PROBLEM_COMMENT_TOP_N,
                     ATTACK_FOCUS_TOP_N, ATTACK_FOCUS_MAX_N, USER_CARD_URL)
from auth import load_cookie, verify_cookie, _try_refresh_cookie
from api_client import BiliAPIClient
from storage import get_db, init_db
from storage import (load_senders, load_global_uid_map, save_global_uid,
                     save_sender, save_user_data, has_user_data, load_video_info,
                     delete_video_data, toggle_false_positive, load_false_positives,
                     load_faces, load_face_cached_uids, save_face)
from main import run_analysis
from uid_resolver import resolve_sender, METHOD_CRC32_CRACK
from user_collector import collect_user_data
from profile_analyzer import analyze_profile
from spam_detector import batch_detect_spam
from llm_analyzer import LLMAnalyzer
from up_analyzer import _tokenize
from report import (REPORT_CSS, esc, js_json, generate_user_card, generate_summary_stats,
                    generate_chart_data, generate_cringe_board, sort_profiles_by_risk,
                    up_wordcloud_data, PROBLEM_CATEGORY_COLORS, _category_chips)

app = Flask(__name__)
PAGE_SIZE = 100  # 弹幕 API 默认/回退每页条数（可选 50/100/200，spec 3）

# 报告页整页 HTML 内存缓存：bvid → (数据指纹, HTML)。避免每次刷新重跑
# _load_profiles/_attach_other_videos 逐用户查询串；job 完成/删除/误报标记时主动失效，
# 指纹比对兜底外部进程（CLI run.py）写入的数据变化
_PAGE_CACHE: dict[str, tuple[tuple, str]] = {}
_PAGE_CACHE_LOCK = threading.Lock()


def _invalidate_page_cache(bvid: str):
    """使指定视频的报告页缓存失效（job 完成/删除时调用）"""
    with _PAGE_CACHE_LOCK:
        _PAGE_CACHE.pop(bvid, None)


def _page_fingerprint(bvid: str) -> tuple:
    """报告页数据指纹：覆盖 senders/users/comments/danmaku/false_positive/face_cache 六张表的
    量与最新写入时间。全部走 bvid 索引的 COUNT/MAX/SUM 聚合，本地 SQLite 毫秒级。
    外部进程（run.py 分析、--force 重跑）落库后指纹即变，下次访问自动重渲染，
    不依赖进程内主动失效（_invalidate_page_cache 仍保留作为即时手段）。"""
    with closing(get_db()) as conn:
        s_cnt, s_uid_cnt = conn.execute(
            "SELECT COUNT(*), COUNT(uid) FROM senders WHERE bvid = ?", (bvid,)).fetchone()
        u_cnt, u_max = conn.execute(
            "SELECT COUNT(*), MAX(u.collected_at) FROM senders s "
            "JOIN users u ON u.uid = s.uid WHERE s.bvid = ?", (bvid,)).fetchone()
        c_cnt, c_prob = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(problem)), 0) FROM comments WHERE bvid = ?",
            (bvid,)).fetchone()
        d_cnt = conn.execute(
            "SELECT COUNT(*) FROM danmaku WHERE bvid = ?", (bvid,)).fetchone()[0]
        f_cnt = conn.execute(
            "SELECT COUNT(*) FROM false_positive WHERE bvid = ?", (bvid,)).fetchone()[0]
        face_cnt = conn.execute("SELECT COUNT(*) FROM face_cache").fetchone()[0]
    return (s_cnt, s_uid_cnt, u_cnt, u_max, c_cnt, c_prob, d_cnt, f_cnt, face_cnt)


# ========== 手动勾选分析 job（spec B；状态存内存 dict，服务重启即失效——spec 已接受） ==========

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
_CLIENT_LOCK = threading.Lock()
_client = None          # 懒加载的 BiliAPIClient（其内部线程安全，限速全局共享）
_client_failed = False  # Cookie 校验失败过一次即记住，后续 POST 直接 503，不重复打验证请求


class CookieInvalidError(Exception):
    """Cookie 缺失/失效：POST 返回 503，job 启动即整体终止"""


def _get_client() -> BiliAPIClient:
    """懒加载创建带登录态的 BiliAPIClient（auth.load_cookie 读 data/cookie.json +
    verify_cookie 联网校验）；失败抛 CookieInvalidError（携带 503 文案），由路由按 503 处理。

    失效判定（对齐 auth.login_by_qrcode 口径）：Cookie 文件缺失/损坏直接判定失效；
    验证失败但有 refresh_token 时先 _try_refresh_cookie 刷新再重验——verify_cookie 对
    网络抖动也返回 False，刷新路径本身是一次额外联网确认，只有刷新后仍失败（或无
    refresh_token）才置 _client_failed 粘性标记，避免单次抖动误判为永久失效。"""
    global _client, _client_failed
    with _CLIENT_LOCK:
        if _client is not None:
            return _client
        if _client_failed:
            raise CookieInvalidError("Cookie 失效，请先运行 python login.py")
        cookie_dict = load_cookie()
        if not cookie_dict or not cookie_dict.get("SESSDATA"):
            _client_failed = True
            raise CookieInvalidError("Cookie 失效，请先运行 python login.py")
        refresh_token = cookie_dict.pop("_refresh_token", None)
        client = BiliAPIClient()
        client.update_cookies(cookie_dict)
        if refresh_token:
            client._refresh_token = refresh_token
        if verify_cookie(client):
            _client = client
            return _client
        # 验证失败但 refresh_token 可能仍有效：先尝试刷新再重验
        if refresh_token and _try_refresh_cookie(client) and verify_cookie(client):
            _client = client
            return _client
        _client_failed = True
        if refresh_token:
            raise CookieInvalidError("Cookie 失效且刷新失败，请先运行 python login.py")
        raise CookieInvalidError("Cookie 失效，请先运行 python login.py")


def _sender_danmaku_stats(bvid: str, mid_hash: str) -> dict:
    """从 danmaku 表重建该发送者的弹幕统计（web 端无内存态 sender_groups）"""
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT content, time, timestamp FROM danmaku WHERE bvid = ? AND mid_hash = ? ORDER BY time",
            (bvid, mid_hash)).fetchall()
    return {
        "count": len(rows),
        "contents": [r["content"] for r in rows],
        "timestamps": [r["timestamp"] for r in rows],
        "video_times": [r["time"] for r in rows],
    }


def _run_analysis_job(job_id: str, bvid: str, mid_hashes: list[str]):
    """后台 job：每个 mid_hash 串行 解析→强制采集→规则画像→LLM深掘→落库（串行即天然限流）。

    错误处理（spec 5）：单发送者失败记 errors（{mid_hash, error} 结构，供前端透出与重试）继续；Cookie 失效 job 整体终止并标记。
    写库全部走 storage 的 save_*（各自短事务），不持长连接。
    """
    def update(**kw):
        with JOBS_LOCK:
            JOBS[job_id].update(kw)

    def add_error(msg, mid_hash=None):
        # 失败明细结构化：mid_hash 供前端"重试失败项"按钮重新提交，msg 为人类可读摘要
        with JOBS_LOCK:
            JOBS[job_id]["errors"].append({"mid_hash": mid_hash, "error": msg})
        print(f"[Job {job_id}] 失败: {mid_hash or '-'} {msg}")

    def add_result(uid):
        with JOBS_LOCK:
            JOBS[job_id]["results"].append(uid)

    try:
        client = _get_client()
    except CookieInvalidError as e:
        add_error(f"{e}（job 已终止）")
        update(finished=True, current="")
        return
    except Exception as e:
        add_error(f"创建 API 客户端失败: {e}（job 已终止）")
        update(finished=True, current="")
        return

    cached = {r["mid_hash"]: r for r in load_senders(bvid)}
    video_info = load_video_info(bvid) or {}
    # web 端无评论映射（spec 7 明确不做）：全局映射库作为唯一明文源，CRC32 彩虹表破解兜底
    global_map = load_global_uid_map()
    plain_uid_map = {h: ent["uid"] for h, ent in global_map.items()}
    method_map = {h: ent["source"] for h, ent in global_map.items()}
    # LLM 深掘器：未配置 LLM_API_KEY 时跳过（spec 3.3）；初始化失败也只跳过深掘
    analyzer = None
    if LLM_API_KEY:
        try:
            analyzer = LLMAnalyzer()
        except Exception as e:
            print(f"[Job {job_id}] 警告: LLM 初始化失败（{e}），本次 job 跳过深掘")

    for i, mid_hash in enumerate(mid_hashes, 1):
        update(current=f"[{i}/{len(mid_hashes)}] {mid_hash}")
        try:
            row = cached.get(mid_hash)
            uid = row["uid"] if row else None
            # 已分析过（senders 有 uid 且 users 有数据）→ 直接跳过计入 results（spec 3.3）
            if uid is not None and has_user_data(uid):
                add_result(uid)
                update(done=i)
                continue

            stats = _sender_danmaku_stats(bvid, mid_hash)
            if uid is None:
                # 1. UID 解析：全局库明文命中优先，CRC32 彩虹表破解兜底；失败记 errors 继续
                uid, confidence, method, _info, collision_risk, candidates = resolve_sender(
                    mid_hash, stats["contents"], plain_uid_map, client, method_map=method_map)
                if uid is None:
                    add_error(f"UID 解析失败（{method}）", mid_hash)
                    update(done=i)
                    continue
                # 落库保留真实置信度/方法（"低"/碰撞在画像页有"可能误识别"徽标）
                save_sender(bvid, mid_hash, uid, confidence, method,
                            stats["count"], stats["contents"],
                            (row["spam_level"] if row else None) or "低",
                            (row["spam_score"] if row else None) or 0.0)
                # 沉淀全局映射库：多候选碰撞条目不沉淀（对齐 main.phase_resolve 口径）
                if not (method == METHOD_CRC32_CRACK and len(candidates) > 1):
                    save_global_uid(mid_hash, uid, method)
            else:
                # senders 有 uid 但 users 无数据的中间态：跳过解析直接采集；
                # collision_risk 从 method 推断（缓存无该字段，口径同 main.py 阶段4）
                collision_risk = row["method"] == METHOD_CRC32_CRACK

            # 2. 强制采集（无视置信度，含"低"/碰撞）
            user_data = collect_user_data(uid, client)
            if "error" in user_data:
                add_error(f"UID:{uid} 采集失败 {user_data['error']}", mid_hash)
                update(done=i)
                continue

            # 3. 规则画像：刷屏统计从 danmaku 表现算（web 端无内存态 spam_results）
            spam = batch_detect_spam({mid_hash: stats}).get(mid_hash, {})
            profile = analyze_profile(user_data,
                                      {"count": stats["count"], "contents": stats["contents"],
                                       "video_times": stats["video_times"]},
                                      spam)
            profile["collision_risk"] = collision_risk
            profile["comments"] = []   # web 端无评论数据（spec 7 不做评论采集）
            profile["cringe"] = {}     # 手动分析不重跑问题弹幕 LLM 检测

            # 4. LLM 深掘（top_k=1 单人单调用，llm_cache 命中零 token；失败只影响深掘）
            if analyzer is not None:
                try:
                    deep = analyzer.analyze_deep([profile], video_info, top_k=1)
                    if uid in deep:
                        profile["ai_deep"] = deep[uid]
                except Exception as e:
                    print(f"[Job {job_id}] 警告: UID:{uid} LLM 深掘失败（{e}），仅跳过深掘")

            save_user_data(uid, user_data.get("name", ""), user_data.get("level", 0),
                           user_data, profile)
            add_result(uid)
            update(done=i)
            print(f"[Job {job_id}] [{i}/{len(mid_hashes)}] {mid_hash} → UID:{uid} 完成")
        except Exception as e:
            add_error(str(e), mid_hash)
            update(done=i)

    update(finished=True, current="")
    _invalidate_page_cache(bvid)   # 该视频画像已变化，报告页缓存失效
    print(f"[Job {job_id}] 完成: 成功 {len(JOBS[job_id]['results'])}/{len(mid_hashes)}")


# ========== 数据加载辅助 ==========

def _load_video_row(bvid: str):
    """videos 表整行；不存在返回 None"""
    with closing(get_db()) as conn:
        return conn.execute("SELECT * FROM videos WHERE bvid = ?", (bvid,)).fetchone()


def _load_profiles(bvid: str) -> list[dict]:
    """该视频已解析发送者的画像（senders JOIN users；同 uid 多 mid_hash 按 uid 去重）。

    附带注入渲染期键（不落库）：resolve_method/resolve_confidence 来自 senders 表
    （卡片解析徽标 tooltip），collected_at 来自 users 表（基础信息采集时间），
    school 在旧缓存画像缺省时从 data_json 回退提取（毕业院校徽标）。
    GROUP BY u.uid 下 method/confidence 取该 uid 任一行（同 uid 多 mid_hash 极少见，可接受）。"""
    conn = get_db()
    rows = conn.execute('''
        SELECT u.profile_json, u.data_json, s.method, s.confidence, u.collected_at
        FROM senders s JOIN users u ON u.uid = s.uid
        WHERE s.bvid = ? AND s.uid IS NOT NULL
        GROUP BY u.uid
    ''', (bvid,)).fetchall()
    conn.close()
    profiles = []
    for r in rows:
        try:
            p = json.loads(r["profile_json"])
        except Exception:
            continue
        p["resolve_method"] = r["method"] or ""
        p["resolve_confidence"] = r["confidence"] or ""
        p["collected_at"] = r["collected_at"] or ""
        # 毕业院校回退：本特性之前的缓存画像无 school 键，从采集原始数据 data_json 补
        if not p.get("school"):
            try:
                p["school"] = json.loads(r["data_json"]).get("school", "")
            except Exception:
                pass
        profiles.append(p)
    return profiles


def _attach_other_videos(bvid: str, profiles: list[dict]):
    """跨视频足迹：把「该用户在其他已分析视频中的出现、弹幕与评论」批量注入每个
    profile 的 other_videos 渲染期键（不落库），供 report.generate_user_card 渲染。

    数据源：senders 跨视频关联（uid 在其它 bvid 的行，JOIN videos 取标题）+
    danmaku 表弹幕样本 + comments 表评论样本。旧版本分析（danmaku/comments 无行）
    降级为只显示计数或「未留存」提示。

    批量策略：全部 uid 分块 IN 查询（SQLite 变量上限防护）只跑一轮，
    每 uid 按活跃度（弹幕数+评论数）取前 MAX_FOOTPRINT_VIDEOS 个视频，
    其余计 more；样本查询只针对保留的视频（每卡片至多 5×5 行，量级可控）。
    """
    uids: list[int] = []
    for p in profiles:
        try:
            u = int(p.get("uid"))
        except (TypeError, ValueError):
            continue
        if u and u not in uids:
            uids.append(u)
    if not uids:
        return

    by_uid: dict[int, dict[str, dict]] = {}      # uid → bvid → {mid_hashes, danmaku_count, title}
    comment_counts: dict[tuple[int, str], int] = {}
    with closing(get_db()) as conn:
        for i in range(0, len(uids), 500):       # 分块规避 SQLite 变量数上限
            chunk = uids[i:i + 500]
            qmarks = ",".join("?" * len(chunk))
            for r in conn.execute(f'''
                    SELECT s.uid, s.bvid, s.mid_hash, s.danmaku_count, v.title
                    FROM senders s JOIN videos v ON v.bvid = s.bvid
                    WHERE s.uid IN ({qmarks}) AND s.bvid != ?
            ''', (*chunk, bvid)).fetchall():
                ent = by_uid.setdefault(r["uid"], {}).setdefault(r["bvid"], {
                    "mid_hashes": [], "danmaku_count": r["danmaku_count"] or 0,
                    "title": r["title"] or ""})
                if r["mid_hash"] not in ent["mid_hashes"]:
                    ent["mid_hashes"].append(r["mid_hash"])
            for row in conn.execute(f'''
                    SELECT uid, bvid, COUNT(*) AS cnt FROM comments
                    WHERE uid IN ({qmarks}) AND bvid != ?
                    GROUP BY uid, bvid
            ''', (*chunk, bvid)).fetchall():
                comment_counts[(row["uid"], row["bvid"])] = row["cnt"]

        for p in profiles:
            try:
                uid = int(p.get("uid"))
            except (TypeError, ValueError):
                continue
            vids = by_uid.get(uid)
            if not vids:
                continue
            ranked = sorted(
                ((vb, ent["danmaku_count"] + comment_counts.get((uid, vb), 0), ent)
                 for vb, ent in vids.items()),
                key=lambda x: x[1], reverse=True)
            kept, rest = ranked[:MAX_FOOTPRINT_VIDEOS], ranked[MAX_FOOTPRINT_VIDEOS:]
            items = []
            for vb, _score, ent in kept:
                # 该视频 danmaku 表完全无行 → 旧版本分析（弹幕/评论均未留存），
                # 前端据此显示「未留存」而非「无」，语义更准确
                legacy = conn.execute(
                    "SELECT 1 FROM danmaku WHERE bvid = ? LIMIT 1", (vb,)).fetchone() is None
                dm_samples: list[str] = []
                for mh in ent["mid_hashes"]:
                    dm_samples.extend(r["content"] for r in conn.execute(
                        "SELECT content FROM danmaku WHERE bvid = ? AND mid_hash = ? "
                        "ORDER BY timestamp DESC LIMIT ?",
                        (vb, mh, MAX_FOOTPRINT_DANMAKU_SAMPLES)).fetchall())
                    if len(dm_samples) >= MAX_FOOTPRINT_DANMAKU_SAMPLES:
                        break
                cmt_samples = [
                    {"content": r["content"], "ctime": r["ctime"],
                     "like": r["like"], "is_sub": r["is_sub"], "problem": r["problem"]}
                    for r in conn.execute(
                        "SELECT content, ctime, like, is_sub, problem FROM comments "
                        "WHERE bvid = ? AND uid = ? ORDER BY like DESC, ctime DESC LIMIT ?",
                        (vb, uid, MAX_FOOTPRINT_COMMENT_SAMPLES)).fetchall()]
                items.append({
                    "bvid": vb,
                    "title": ent["title"] or vb,
                    "danmaku_count": ent["danmaku_count"],
                    "danmaku_samples": dm_samples[:MAX_FOOTPRINT_DANMAKU_SAMPLES],
                    "comment_count": comment_counts.get((uid, vb), 0),
                    "comment_samples": cmt_samples,
                    "legacy": legacy,
                })
            p["other_videos"] = {"items": items, "more": len(rest)}


def _sender_meta(bvid: str) -> dict:
    """发送者联查（spec 5）：mid_hash → {uid, name, spam_level, categories}。
    uid/name/spam_level 来自 senders LEFT JOIN users；categories 从 users.profile_json
    的 cringe 字段 Python 侧解析（非 SQL）。senders 无行的 mid_hash 不在此表 → 未分析。
    连接用 closing 保证异常路径也关闭（参照 storage.py 模式）。"""
    with closing(get_db()) as conn:
        rows = conn.execute('''
        SELECT s.mid_hash, s.uid, s.spam_level, u.name, u.profile_json
        FROM senders s LEFT JOIN users u ON u.uid = s.uid
        WHERE s.bvid = ?
    ''', (bvid,)).fetchall()
    meta = {}
    for r in rows:
        categories = []
        if r["profile_json"]:
            try:
                categories = json.loads(r["profile_json"]).get("cringe", {}).get("categories", []) or []
            except Exception:
                categories = []
        meta[r["mid_hash"]] = {"uid": r["uid"], "name": r["name"],
                               "spam_level": r["spam_level"], "categories": categories}
    return meta


def _danmaku_panel_stats(bvid: str) -> dict:
    """弹幕浏览器统计面板（spec 4）：总弹幕数/合并后行数/独立发送者数/已解析发送者数
    + 问题弹幕类别分布 + 发送者弹幕数 Top10。无弹幕数据时只返回 {"total": 0}。"""
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM danmaku WHERE bvid = ?", (bvid,)).fetchone()[0]
    if total == 0:
        conn.close()
        return {"total": 0}
    merged = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM danmaku WHERE bvid = ? GROUP BY mid_hash, content)",
        (bvid,)).fetchone()[0]
    senders_total = conn.execute(
        "SELECT COUNT(DISTINCT mid_hash) FROM danmaku WHERE bvid = ?", (bvid,)).fetchone()[0]
    resolved = conn.execute(
        "SELECT COUNT(*) FROM senders WHERE bvid = ? AND uid IS NOT NULL", (bvid,)).fetchone()[0]
    top10 = conn.execute('''
        SELECT mid_hash, COUNT(*) AS cnt FROM danmaku WHERE bvid = ?
        GROUP BY mid_hash ORDER BY cnt DESC LIMIT 10
    ''', (bvid,)).fetchall()
    conn.close()
    meta = _sender_meta(bvid)
    cat_counts = {}
    for m in meta.values():
        for c in m["categories"]:
            cat_counts[c] = cat_counts.get(c, 0) + 1
    return {
        "total": total, "merged": merged, "senders": senders_total, "resolved": resolved,
        "categories": cat_counts,
        "top10": [{"mid_hash": r["mid_hash"], "count": r["cnt"],
                   "name": (meta.get(r["mid_hash"]) or {}).get("name") or r["mid_hash"]}
                  for r in top10],
    }


def _danmaku_density(bvid: str, duration) -> dict | None:
    """概览页弹幕密度时间轴：按视频内时间分桶计数（一眼看到哪个片段弹幕爆发）。

    桶数约每 10 秒一桶，上限 DENSITY_BUCKETS；无弹幕数据或时长未知返回 None（不渲染）。"""
    duration = int(duration or 0)
    if duration <= 0:
        return None
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT time FROM danmaku WHERE bvid = ?", (bvid,)).fetchall()
    if not rows:
        return None
    buckets = min(DENSITY_BUCKETS, max(10, duration // 10))
    size = duration / buckets
    counts = [0] * buckets
    for r in rows:
        i = int(r["time"] // size)
        counts[min(max(i, 0), buckets - 1)] += 1
    labels = [_fmt_video_time(i * size) for i in range(buckets)]
    # starts：每桶起始秒数（前端点击柱条跳转视频对应时段，P1-a）
    return {"labels": labels, "data": counts, "starts": [int(i * size) for i in range(buckets)]}


def _danmaku_attr_stats(bvid: str) -> dict | None:
    """弹幕属性分布（mode/color 已入库）：滚动/顶部/底部占比 + 颜色 Top12。
    滚动弹幕占比、颜色聚集度、顶/底弹幕对识别刷屏与水军有区分度。无数据返回 None。"""
    with closing(get_db()) as conn:
        modes = conn.execute(
            "SELECT mode, COUNT(*) AS cnt FROM danmaku WHERE bvid = ? GROUP BY mode",
            (bvid,)).fetchall()
        if not modes:
            return None
        colors = conn.execute(
            "SELECT color, COUNT(*) AS cnt FROM danmaku WHERE bvid = ? AND color != '' "
            "GROUP BY color ORDER BY cnt DESC LIMIT 12", (bvid,)).fetchall()
    mode_map = {"滚动": 0, "顶部": 0, "底部": 0, "其他": 0}
    for r in modes:
        if r["mode"] in (1, 2, 3):
            mode_map["滚动"] += r["cnt"]
        elif r["mode"] == 4:
            mode_map["底部"] += r["cnt"]
        elif r["mode"] == 5:
            mode_map["顶部"] += r["cnt"]
        else:
            mode_map["其他"] += r["cnt"]
    return {
        "mode": {k: v for k, v in mode_map.items() if v},
        "colors": [[r["color"], r["cnt"]] for r in colors],
    }


def _resolve_quality(bvid: str) -> dict | None:
    """概览页「解析质量」区块：用户身份可信度——解析方式分布（评论验证/全局库/CRC32反查
    等）、置信度分布、碰撞风险人数（CRC32 反查存在撞库误识别风险）。

    报告结论可靠度判断依据：高置信+明文验证占比越高越可信；碰撞风险人数多需谨慎解读。"""
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT method, confidence FROM senders WHERE bvid = ? AND uid IS NOT NULL",
            (bvid,)).fetchall()
        total_senders = conn.execute(
            "SELECT COUNT(DISTINCT mid_hash) FROM danmaku WHERE bvid = ?", (bvid,)).fetchone()[0]
    if not rows:
        return None
    methods: dict[str, int] = {}
    confs: dict[str, int] = {}
    collision = 0
    for r in rows:
        m = r["method"] or "未知"
        methods[m] = methods.get(m, 0) + 1
        c = r["confidence"] or "无"
        confs[c] = confs.get(c, 0) + 1
        if m == METHOD_CRC32_CRACK:
            collision += 1
    # 置信度按 高/中/低/无 固定序输出（图表颜色语义稳定）
    conf_order = ["高", "中", "低", "无"]
    return {
        "method_labels": list(methods.keys()),
        "method_data": list(methods.values()),
        "conf_labels": [c for c in conf_order if c in confs],
        "conf_data": [confs[c] for c in conf_order if c in confs],
        "collision": collision,
        "resolved": len(rows),
        "total_senders": total_senders,
    }


# ========== 误报标记（P2-a）/ 争执焦点（P0-b）/ 问题评论榜（P1-b） ==========

def _fp_btn(kind: str, target, marked: bool) -> str:
    """误报标记按钮（P2-a）：点击切换标记，标记后该条不再计入聚合，可再点撤销"""
    label = "撤销误报" if marked else "误报"
    cls = "fp-btn fp-btn-marked" if marked else "fp-btn"
    return (f'<button class="{cls}" data-kind="{esc(kind)}" data-target="{esc(str(target))}" '
            f'onclick="fpToggle(this)" '
            f'title="人工标记该条为 LLM 误报；标记后不计入聚合与疑似分，可撤销">{label}</button>')


def _apply_danmaku_fp(profiles: list[dict], fp_dm: set[str]) -> list[str]:
    """弹幕误报扣除（P2-a）：按内容把人工标记误报的问题弹幕从每个画像的 cringe 聚合中
    剔除（count/max_severity/categories/examples 全部重算，风险排序随之降级）。

    判定按内容去重（llm_cache 同源同罪），故误报粒度=内容，一次标记对所有发送者生效。
    旧 llm_cache 结果无全量 items 时回退用 examples（≤5 条）重算，count 可能偏小，
    --force 重跑后即为精确值。返回实际被扣除的误报内容列表（供榜单底部撤销入口）。"""
    if not fp_dm:
        return []
    used: list[str] = []
    for p in profiles:
        cr = p.get("cringe") or {}
        if not cr.get("count"):
            continue
        items = cr.get("items") or cr.get("examples") or []
        kept = [it for it in items if it.get("content") not in fp_dm]
        for it in items:
            c = it.get("content")
            if c in fp_dm and c not in used:
                used.append(c)
        if len(kept) == len(items):
            continue
        cr["count"] = len(kept)
        cr["max_severity"] = max((it.get("severity", 1) for it in kept), default=0)
        cats: list[str] = []
        for it in kept:
            if it.get("category") and it["category"] not in cats:
                cats.append(it["category"])
        cr["categories"] = cats
        cr["examples"] = kept[:5]
        cr["items"] = kept
        p["cringe"] = cr
    return used


def _fp_dm_block(fp_contents: list[str]) -> str:
    """问题弹幕榜底部：已人工标记误报的弹幕内容（不计入上方聚合），点撤销恢复"""
    if not fp_contents:
        return ""
    items = "".join(f'<li>{esc(c)} {_fp_btn("dm", c, True)}</li>' for c in fp_contents)
    return (f'<details class="fp-block"><summary>🚫 已标记误报 {len(fp_contents)} 条'
            f'（不计入上方聚合，展开可撤销）</summary><ul class="fp-list">{items}</ul></details>')


# ========== 头像后台补采（争执焦点关系图：face_cache 缺失的 uid 异步补齐） ==========

_FACE_BACKFILL_RUNNING: set[int] = set()   # 在采 uid，防并发重复补采
_FACE_BACKFILL_LOCK = threading.Lock()


def _backfill_faces(uids: list[int]):
    """后台线程：逐个补采头像（名片接口很轻量），走 BiliAPIClient 限速；
    Cookie 失效/触发风控即整体放弃本次，下次渲染再试"""
    with _FACE_BACKFILL_LOCK:
        todo = [u for u in uids if u not in _FACE_BACKFILL_RUNNING]
        _FACE_BACKFILL_RUNNING.update(todo)
    if not todo:
        return
    try:
        client = _get_client()
    except Exception as e:
        print(f"[Face] 补采放弃（无可用登录态）: {e}")
        with _FACE_BACKFILL_LOCK:
            _FACE_BACKFILL_RUNNING.difference_update(todo)
        return

    def work():
        try:
            for uid in todo:
                data = client.get(USER_CARD_URL, params={"mid": uid, "photo": "false"})
                if data.get("code") == -412:
                    print(f"[Face] 触发风控，停止本次补采（剩 {len(todo) - todo.index(uid)} 个下次再采）")
                    break
                face = ((data.get("data") or {}).get("card") or {}).get("face", "")
                save_face(uid, face)   # 空串也落库：标记查过，避免每次渲染重复请求
            print(f"[Face] 头像补采完成 {len(todo)} 个")
        finally:
            with _FACE_BACKFILL_LOCK:
                _FACE_BACKFILL_RUNNING.difference_update(todo)

    threading.Thread(target=work, daemon=True).start()


def _faces_for(uids: list[int]) -> dict[int, str]:
    """节点头像汇总：face_cache 优先，users.data_json 兜底（已采集用户），
    两侧都缺的触发后台补采（本次渲染先缺省，下次刷新带上）"""
    faces = load_faces(uids)
    miss_cache = [u for u in uids if u not in faces]
    if miss_cache:
        with closing(get_db()) as conn:
            for i in range(0, len(miss_cache), 500):
                chunk = miss_cache[i:i + 500]
                qm = ",".join("?" * len(chunk))
                for r in conn.execute(
                        f"SELECT uid, data_json FROM users WHERE uid IN ({qm})", chunk):
                    try:
                        f = json.loads(r["data_json"]).get("face", "")
                        if f:
                            faces[r["uid"]] = f
                    except Exception:
                        pass
    cached = load_face_cached_uids(uids)
    need = [u for u in uids if u not in faces and u not in cached]
    if need:
        _backfill_faces(need)
    return faces


def _attack_focus(bvid: str, fp_cmt: set[str]) -> dict:
    """争执焦点（P0-b）：问题回复按 parent_rpid 还原 A→B 攻击边，聚合挑事分/被攻击分。

    只统计问题回复（problem 非空且 parent_rpid>0、父级在库、非自回）；已标记误报的不参与。
    每侧各留至多 3 条代表原文（挑事者=其问题回复原文及攻击对象；被围攻者=受害原评+攻击者
    及其攻击原文成对，能直接看出谁攻击了ta、攻击了什么）。
    展示名额随攻击边数浮动（保底 ATTACK_FOCUS_TOP_N，每10条攻击边+1，封顶 ATTACK_FOCUS_MAX_N）。
    返回 {"attackers": [{uid,name,count,categories,examples}],
           "victims": [{uid,name,count,examples}], "names": {uid: 昵称},
           "top_n": 动态名额, "edges": 攻击边数}，无攻击边时 attackers 为空列表。"""
    with closing(get_db()) as conn:
        rows = conn.execute('''
            SELECT c.rpid, c.uid AS attacker, c.uname AS aname, c.problem,
                   c.content AS reply_content,
                   p.uid AS victim, p.uname AS vname, p.content AS parent_content,
                   ua.name AS a_dbname, uv.name AS v_dbname
            FROM comments c
            JOIN comments p ON p.bvid = c.bvid AND p.rpid = c.parent_rpid
            LEFT JOIN users ua ON ua.uid = c.uid
            LEFT JOIN users uv ON uv.uid = p.uid
            WHERE c.bvid = ? AND c.problem != '' AND c.parent_rpid > 0 AND p.uid != c.uid
            ORDER BY c.like DESC
        ''', (bvid,)).fetchall()

    def _nm(uname, dbname, uid) -> str:
        return uname or dbname or f"UID:{uid}"

    attackers: dict[int, dict] = {}
    victims: dict[int, dict] = {}
    edge_cnt = 0
    for r in rows:
        if str(r["rpid"]) in fp_cmt:
            continue
        edge_cnt += 1
        a, v = r["attacker"], r["victim"]
        a_name = _nm(r["aname"], r["a_dbname"], a)
        v_name = _nm(r["vname"], r["v_dbname"], v)
        ea = attackers.setdefault(a, {"uid": a, "name": a_name,
                                      "count": 0, "victims": Counter(), "categories": [],
                                      "examples": []})
        ea["count"] += 1
        ea["victims"][v] += 1
        if r["problem"] not in ea["categories"]:
            ea["categories"].append(r["problem"])
        if len(ea["examples"]) < 3:
            ea["examples"].append({"content": r["reply_content"], "target": v_name,
                                   "category": r["problem"]})
        ev = victims.setdefault(v, {"uid": v, "name": v_name,
                                    "count": 0, "attackers": Counter(), "examples": []})
        ev["count"] += 1
        ev["attackers"][a] += 1
        if len(ev["examples"]) < 3:
            # 被围攻者证据要成对：自己的原评 + 攻击者的攻击原文（单放原评看不出"被谁攻击了什么"）
            ev["examples"].append({"parent": r["parent_content"], "attack": r["reply_content"],
                                   "attacker": a_name, "category": r["problem"]})
    names = {u: e["name"] for u, e in attackers.items()}
    names.update({u: e["name"] for u, e in victims.items()})
    # 头像（关系图节点用）：face_cache + users.data_json 双源汇总，缺的异步补采
    uids = list(set(attackers) | set(victims))
    faces = _faces_for(uids)
    # 展示名额随攻击边数浮动：保底 ATTACK_FOCUS_TOP_N，每 10 条攻击边 +1，封顶 ATTACK_FOCUS_MAX_N
    top_n = min(ATTACK_FOCUS_MAX_N, max(ATTACK_FOCUS_TOP_N, 5 + edge_cnt // 10))
    return {
        "attackers": sorted(attackers.values(), key=lambda e: -e["count"]),
        "victims": sorted(victims.values(), key=lambda e: -e["count"]),
        "names": names,
        "faces": faces,
        "top_n": top_n,
        "edges": edge_cnt,
    }


def _truncate(text: str, limit: int = 80) -> str:
    """原文摘录截断（争执焦点/楼中楼引用展示用）"""
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _attack_focus_html(data: dict) -> str:
    """争执焦点区块：左列挑事者（发起问题回复最多，附问题回复原文），
    右列被围攻者（被问题回复命中最多，附被攻击的原评原文）"""
    if not data or not data["attackers"]:
        return ""
    names = data["names"]

    def _opp_text(counter: Counter) -> str:
        return "、".join(f'{esc(names.get(u, f"UID:{u}"))}×{n}' for u, n in counter.most_common(3))

    def _quotes(examples: list[dict], arrow: str) -> str:
        return "".join(
            f'<div class="af-quote">{arrow} {esc(e["target"])}：「{esc(_truncate(e["content"]))}」'
            f'{_problem_chip(e["category"])}</div>'
            for e in examples)

    def _victim_quotes(examples: list[dict]) -> str:
        """被围攻者证据成对展示：受害原评 + 攻击者及其攻击原文（看出谁攻击了ta、攻击了什么）"""
        out = []
        for e in examples:
            out.append(
                f'<div class="af-quote">原评：「{esc(_truncate(e["parent"]))}」</div>'
                f'<div class="af-quote af-quote-atk">⚔ {esc(e["attacker"])} 攻击：'
                f'「{esc(_truncate(e["attack"]))}」{_problem_chip(e["category"])}</div>')
        return "".join(out)

    def _item(e: dict, badge: str, opp_label: str, opp: str, arrow: str) -> str:
        return f'''<div class="af-item" data-side="a" data-uid="{e["uid"]}">
            <div class="af-line">
                <a href="/user/{e["uid"]}" title="查看用户互动时间线">{esc(e["name"])}</a>
                <span class="hot-badge">{badge.format(e["count"])}</span>
                {_category_chips(e.get("categories", []))}
                <span class="af-targets">{opp_label} {opp}</span>
            </div>{_quotes(e["examples"], arrow)}</div>'''

    def _victim_item(e: dict) -> str:
        return f'''<div class="af-item" data-side="v" data-uid="{e["uid"]}">
            <div class="af-line">
                <a href="/user/{e["uid"]}" title="查看用户互动时间线">{esc(e["name"])}</a>
                <span class="hot-badge">被攻击 {e["count"]} 次</span>
                <span class="af-targets">主要来源： {_opp_text(e["attackers"])}</span>
            </div>{_victim_quotes(e["examples"])}</div>'''

    top_n = data.get("top_n", ATTACK_FOCUS_TOP_N)
    shown_attackers = data["attackers"][:top_n]
    shown_victims = data["victims"][:top_n]
    # 关系图画布数据（P3-a 重做为二分图）：仅保留两侧都上榜的配对；
    # links 带权重（攻击次数），nodes 带阵营/计数，供 SVG 画布渲染节点与边
    shown_v_uids = {e["uid"] for e in shown_victims}
    names = data["names"]
    faces = data.get("faces", {})
    graph_nodes = ([{"id": e["uid"], "name": e["name"], "side": "a", "n": e["count"],
                     "face": faces.get(e["uid"], "")} for e in shown_attackers]
                   + [{"id": e["uid"], "name": e["name"], "side": "v", "n": e["count"],
                       "face": faces.get(e["uid"], "")} for e in shown_victims])
    graph_links = [{"s": e["uid"], "t": v, "w": n}
                   for e in shown_attackers
                   for v, n in e["victims"].most_common() if v in shown_v_uids]
    graph = {"nodes": graph_nodes, "links": graph_links}
    graph_html = ""
    if graph_links:
        graph_html = (f'<div class="af-graph" data-af-graph=\'{json.dumps(graph, ensure_ascii=False)}\'>'
                      f'</div>')
    a_html = "".join(_item(e, "攻击 {} 次", "主要对象：", _opp_text(e["victims"]), "攻击")
                     for e in shown_attackers)
    v_html = "".join(_victim_item(e) for e in shown_victims)
    return f'''
    <div class="cringe-board af-board">
        <h3>⚔️ 争执焦点（谁攻击谁：问题回复按 parent_rpid 还原攻击边 {data.get("edges", 0)} 条；
            环状画布：被围攻者居中、挑事者环绕，箭头线指向受害者，节点大小/线粗映射攻击次数，悬停节点高亮其关系，点击定位到下方明细）</h3>
        {graph_html}
        <div class="af-cols">
            <div class="af-col"><h4>🗡 挑事者 Top{top_n}</h4>{a_html}</div>
            <div class="af-col"><h4>🛡 被围攻者 Top{top_n}</h4>{v_html}</div>
        </div>
    </div>'''


def _problem_comment_board(bvid: str, fp_cmt: set[str]) -> tuple[list[dict], list[dict]]:
    """问题评论榜（P1-b）：全部问题评论按热度（点赞 + 回复数×权重）降序，高热度优先。

    楼中楼评论（is_sub）附被回复的父评论原文/作者，供判断语境。
    返回 (未标记误报的 Top N 条目, 已标记误报的条目)；后者供底部撤销入口。"""
    with closing(get_db()) as conn:
        rows = conn.execute('''
            SELECT c.rpid, c.uid, c.uname, c.content, c.ctime, c.like, c.reply_count,
                   c.is_sub, c.problem, u.name AS db_name,
                   p.content AS parent_content, p.uname AS parent_uname, pu.name AS parent_db_name,
                   p.uid AS parent_uid
            FROM comments c
            LEFT JOIN users u ON u.uid = c.uid
            LEFT JOIN comments p ON p.bvid = c.bvid AND p.rpid = c.parent_rpid
            LEFT JOIN users pu ON pu.uid = p.uid
            WHERE c.bvid = ? AND c.problem != ''
        ''', (bvid,)).fetchall()
    items, marked = [], []
    for r in rows:
        it = {
            "rpid": r["rpid"], "uid": r["uid"],
            "name": r["uname"] or r["db_name"] or f"UID:{r['uid']}",
            "content": r["content"], "ctime": r["ctime"], "like": r["like"] or 0,
            "reply_count": r["reply_count"] or 0, "is_sub": r["is_sub"],
            "problem": r["problem"],
            "heat": (r["like"] or 0) + (r["reply_count"] or 0) * COMMENT_HEAT_REPLY_WEIGHT,
            # 楼中楼语境：被回复的父评论（父级未采集到时为空，前端不展示）
            "parent_content": r["parent_content"] or "",
            "parent_name": (r["parent_uname"] or r["parent_db_name"]
                            or (f"UID:{r['parent_uid']}" if r["parent_uid"] else "")),
        }
        (marked if str(r["rpid"]) in fp_cmt else items).append(it)
    items.sort(key=lambda x: -x["heat"])
    marked.sort(key=lambda x: -x["heat"])
    return items[:PROBLEM_COMMENT_TOP_N], marked


def _problem_comment_board_html(items: list[dict], marked: list[dict], aid, up_mid: int) -> str:
    """问题评论榜 HTML（高回复评论页顶部区块）"""
    if not items and not marked:
        return ""

    def _row(it: dict, is_marked: bool) -> str:
        date = datetime.fromtimestamp(it["ctime"]).strftime("%Y-%m-%d") if it["ctime"] else ""
        sub_mark = '<span class="dm-time">回复</span> ' if it["is_sub"] else ""
        origin = (f'<a class="hot-origin" href="https://www.bilibili.com/video/av{aid}#reply{it["rpid"]}" '
                  f'target="_blank" rel="noopener">原文 ↗</a>') if aid else ""
        # 楼中楼语境（fix）：子评论贴出被回复的父评论原文，便于判断攻击对象
        parent_html = ""
        if it["is_sub"] and it["parent_content"]:
            parent_html = (f'<div class="pcb-parent">回复 @{esc(it["parent_name"])}：'
                           f'「{esc(_truncate(it["parent_content"]))}」</div>')
        return f'''<li class="pcb-item">
            <a class="hot-author" href="/user/{esc(it["uid"])}"
               title="查看该用户在已分析视频中的互动时间线">{esc(it["name"])}</a>{_role_badges(it["uid"], 0, up_mid)}
            {_problem_chip(it["problem"], is_marked)}{_fp_btn("cmt", it["rpid"], is_marked)}
            <span class="dm-time">热度 {it["heat"]:,}（👍{it["like"]:,} + 💬{it["reply_count"]:,}×{COMMENT_HEAT_REPLY_WEIGHT}）· {date}</span>
            {origin}{parent_html}<br>{sub_mark}{esc(it["content"])}</li>'''

    body = "".join(_row(it, False) for it in items)
    marked_html = ""
    if marked:
        mrows = "".join(_row(it, True) for it in marked)
        marked_html = (f'<details class="fp-block"><summary>🚫 已标记误报 {len(marked)} 条'
                       f'（不计入榜单与聚合，展开可撤销）</summary><ul class="fp-list">{mrows}</ul></details>')
    return f'''
    <div class="cringe-board">
        <h3>🔥 问题评论榜（按热度 = 点赞 + 回复数×{COMMENT_HEAT_REPLY_WEIGHT} 加权，最多 {PROBLEM_COMMENT_TOP_N} 条）</h3>
        <ul class="pcb-list">{body}</ul>
        {marked_html}
    </div>'''


def _cross_video_overlaps() -> list[dict]:
    """首页跨视频重叠用户面板：在 >= CROSS_VIDEO_MIN_VIDEOS 个已分析视频中都发过弹幕的
    发送者（全局 UID 映射沉淀后此查询很便宜），找水军/情绪带节奏用户比单视频报告强。

    每个视频附弹幕/评论明细样本（前端 <details> 展开查看「TA 在该视频说了什么」）：
    弹幕按发送时间倒序、评论按点赞降序各取 MAX_FOOTPRINT_*_SAMPLES 条；旧版本分析
    （danmaku 表无行）标注明细未留存。
    返回 [{uid, name, video_count, total_dm, videos: [{bvid,title,dm_count,dm_samples,
    cmt_count,cmt_samples,legacy}]}]，按视频数/弹幕数降序。"""
    with closing(get_db()) as conn:
        rows = conn.execute('''
            SELECT s.uid, u.name, COUNT(DISTINCT s.bvid) AS vcnt, SUM(s.danmaku_count) AS total_dm
            FROM senders s LEFT JOIN users u ON u.uid = s.uid
            WHERE s.uid IS NOT NULL
            GROUP BY s.uid HAVING vcnt >= ?
            ORDER BY vcnt DESC, total_dm DESC LIMIT ?
        ''', (CROSS_VIDEO_MIN_VIDEOS, CROSS_VIDEO_MAX_USERS)).fetchall()
        if not rows:
            return []
        uids = [r["uid"] for r in rows]
        qmarks = ",".join("?" * len(uids))
        # (uid, bvid) → 视频标题、该用户在该视频的 mid_hash 列表与弹幕计数
        vinfo: dict[tuple[int, str], dict] = {}
        for r in conn.execute(f'''
                SELECT s.uid, s.bvid, s.mid_hash, s.danmaku_count, v.title FROM senders s
                JOIN videos v ON v.bvid = s.bvid
                WHERE s.uid IN ({qmarks})
        ''', uids).fetchall():
            ent = vinfo.setdefault((r["uid"], r["bvid"]), {
                "title": r["title"] or r["bvid"], "mid_hashes": [], "dm_count": 0})
            if r["mid_hash"] not in ent["mid_hashes"]:
                ent["mid_hashes"].append(r["mid_hash"])
            ent["dm_count"] += r["danmaku_count"] or 0

        # 明细样本：逐 (uid,bvid) 取弹幕/评论（50 用户×数视频，本地 SQLite 量级可控）
        legacy_cache: dict[str, bool] = {}
        videos_by_uid: dict[int, list] = {}
        for (uid, bvid), ent in vinfo.items():
            if bvid not in legacy_cache:
                legacy_cache[bvid] = conn.execute(
                    "SELECT 1 FROM danmaku WHERE bvid = ? LIMIT 1", (bvid,)).fetchone() is None
            dm_samples: list[str] = []
            for mh in ent["mid_hashes"]:
                dm_samples.extend(r["content"] for r in conn.execute(
                    "SELECT content FROM danmaku WHERE bvid = ? AND mid_hash = ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (bvid, mh, MAX_FOOTPRINT_DANMAKU_SAMPLES)).fetchall())
                if len(dm_samples) >= MAX_FOOTPRINT_DANMAKU_SAMPLES:
                    break
            cmt_count = conn.execute(
                "SELECT COUNT(*) FROM comments WHERE bvid = ? AND uid = ?",
                (bvid, uid)).fetchone()[0]
            cmt_samples = [
                {"content": r["content"], "like": r["like"], "problem": r["problem"] or ""}
                for r in conn.execute(
                    "SELECT content, like, problem FROM comments WHERE bvid = ? AND uid = ? "
                    "ORDER BY like DESC, ctime DESC LIMIT ?",
                    (bvid, uid, MAX_FOOTPRINT_COMMENT_SAMPLES)).fetchall()]
            videos_by_uid.setdefault(uid, []).append({
                "bvid": bvid, "title": ent["title"],
                "dm_count": ent["dm_count"], "dm_samples": dm_samples[:MAX_FOOTPRINT_DANMAKU_SAMPLES],
                "cmt_count": cmt_count, "cmt_samples": cmt_samples,
                "legacy": legacy_cache[bvid],
            })
    return [{
        "uid": r["uid"],
        "name": r["name"] or f"UID:{r['uid']}",
        "video_count": r["vcnt"],
        "total_dm": r["total_dm"] or 0,
        # 弹幕多的视频排前，展开列表一眼看到主战场
        "videos": sorted(videos_by_uid.get(r["uid"], []),
                         key=lambda v: -(v["dm_count"] + v["cmt_count"])),
    } for r in rows]


def _fmt_duration(sec) -> str:
    """秒 → mm:ss 或 h:mm:ss 时长文本（首页视频列表时长列）"""
    sec = int(sec or 0)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _fmt_video_time(sec) -> str:
    """视频内时间（秒）→ mm:ss（用户时间线弹幕样本用）"""
    sec = int(sec or 0)
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"


def _export_links(bvid: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """data/reports/ 下 report_{bvid}_*.csv/.json 下载链接，分（最新一组, 历史组）。

    文件名含时间戳，按文件名倒序即时间倒序；每种格式第一个为最新，其余收进历史折叠块。"""
    latest, history = [], []
    for ext in ("csv", "json"):
        files = sorted(glob.glob(os.path.join(REPORT_DIR, f"report_{bvid}_*.{ext}")), reverse=True)
        for i, f in enumerate(files):
            (latest if i == 0 else history).append((os.path.basename(f), ext.upper()))
    return latest, history


def _hot_comments(bvid: str, aid: int | None) -> dict:
    """高回复评论页数据（潜在争执热点）：回复数 >= HOT_COMMENT_MIN_REPLIES 的主评论，
    按回复数降序；子回复全量取出（不截断），附 parent_rpid 供前端构建回复树、
    problem 供问题评论标注。

    昵称优先级：comments.uname（采集时落库）→ users.name（已分析用户）→ UID 回退。
    附 up_mid（视频 UP 主 uid），供渲染层标注「UP主」；「楼主」按主评论 uid 标注。

    返回 {"legacy": bool, "items": [...], "total_replies": int, "up_mid": int}；
    legacy=True 表示该视频评论回复数未留存（旧版本分析），页面显示对应空态。"""
    with closing(get_db()) as conn:
        if conn.execute("SELECT 1 FROM comments WHERE bvid = ? LIMIT 1", (bvid,)).fetchone() is None:
            return {"legacy": True, "items": [], "total_replies": 0, "up_mid": 0}
        # 视频 UP 主 uid（标注用；解析失败回退 0 即不标注）
        vrow = conn.execute("SELECT video_info_json FROM videos WHERE bvid = ?", (bvid,)).fetchone()
        try:
            up_mid = int((json.loads(vrow["video_info_json"]).get("owner") or {}).get("mid", 0)) if vrow else 0
        except Exception:
            up_mid = 0
        rows = conn.execute('''
            SELECT c.rpid, c.uid, c.uname, c.content, c.ctime, c.like, c.reply_count, c.problem, u.name
            FROM comments c LEFT JOIN users u ON u.uid = c.uid
            WHERE c.bvid = ? AND c.is_sub = 0 AND c.reply_count >= ?
            ORDER BY c.reply_count DESC, c.like DESC
            LIMIT ?
        ''', (bvid, HOT_COMMENT_MIN_REPLIES, HOT_COMMENT_MAX_SHOW)).fetchall()

        def _name(uname, db_name, uid) -> str:
            return uname or db_name or f"UID:{uid}"

        items = []
        for r in rows:
            # 完整回复树（不截断）：按发送时间升序，按 parent_rpid 嵌套缩进
            subs = conn.execute('''
                SELECT c.rpid, c.uid, c.uname, c.content, c.like, c.parent_rpid, c.problem, u.name
                FROM comments c LEFT JOIN users u ON u.uid = c.uid
                WHERE c.bvid = ? AND c.is_sub = 1 AND c.root_rpid = ?
                ORDER BY c.ctime ASC
            ''', (bvid, r["rpid"])).fetchall()
            items.append({
                "rpid": r["rpid"],
                "uid": r["uid"],
                "name": _name(r["uname"], r["name"], r["uid"]),
                "content": r["content"],
                "ctime": r["ctime"],
                "like": r["like"],
                "problem": r["problem"] or "",
                "reply_count": r["reply_count"],
                "subs": [{"rpid": s["rpid"], "uid": s["uid"],
                          "name": _name(s["uname"], s["name"], s["uid"]),
                          "content": s["content"], "like": s["like"],
                          "parent_rpid": s["parent_rpid"],
                          "problem": s["problem"] or ""} for s in subs],
            })
    return {"legacy": False, "items": items, "up_mid": up_mid,
            "total_replies": sum(i["reply_count"] for i in items)}


def _problem_chip(problem: str, marked: bool = False) -> str:
    """问题评论标注（LLM 判定类别，分色 chip 与问题弹幕榜视觉一致）；
    marked=True 表示已被人工标记误报（划线样式，不计入聚合）"""
    if not problem:
        return ""
    color = PROBLEM_CATEGORY_COLORS.get(problem, "#999999")
    cls = "problem-chip fp-chip-marked" if marked else "problem-chip"
    tip = "已被人工标记为误报（不计入聚合，点右侧「撤销误报」恢复）" if marked else "LLM 判定的问题评论类别"
    return (f'<span class="{cls}" style="background:{color}" '
            f'title="{tip}">{esc(problem)}</span>')


def _role_badges(uid: int, root_uid: int, up_mid: int) -> str:
    """评论区身份标注：UP主（视频作者）/ 楼主（主评论作者，root_uid=0 时不标注）"""
    badges = ""
    if up_mid and uid == up_mid:
        badges += '<span class="role-badge role-up">UP主</span>'
    if root_uid and uid == root_uid:
        badges += '<span class="role-badge role-op">楼主</span>'
    return badges


def _reply_tree_html(subs: list[dict], root_uid: int, up_mid: int, fp_cmt: set[str] = frozenset()) -> str:
    """把扁平子回复列表按 parent_rpid 嵌套成回复树 HTML（<ul> 嵌套即缩进层级）。

    楼主（root_uid）/UP主（up_mid）出现时加身份徽标；问题评论带误报标记按钮（P2-a）。
    parent_rpid 指向的父级不在已采集集合内（补采截断/旧库无该字段为 0）时挂到主楼下，
    保证不丢任何一条回复。"""
    nodes = {s["rpid"]: s for s in subs}
    children: dict[int, list[dict]] = {}
    roots = []
    for s in subs:
        p = s["parent_rpid"]
        if p and p in nodes:
            children.setdefault(p, []).append(s)
        else:
            roots.append(s)   # 直接回复主楼（parent=0/=root）或父级缺失

    def render(node: dict) -> str:
        kids = "".join(render(ch) for ch in children.get(node["rpid"], []))
        sub_ul = f"<ul>{kids}</ul>" if kids else ""
        marked = str(node["rpid"]) in fp_cmt
        fp = _fp_btn("cmt", node["rpid"], marked) if node["problem"] else ""
        return (f'<li><div class="hot-reply"><span class="hot-sub-author">{esc(node["name"])}</span>'
                f'{_role_badges(node["uid"], root_uid, up_mid)}'
                f'：{esc(node["content"])} <span class="dm-time">👍{node["like"]:,}</span>'
                f'{_problem_chip(node["problem"], marked)}{fp}</div>{sub_ul}</li>')

    return "<ul class=\"hot-tree\">" + "".join(render(s) for s in roots) + "</ul>"


def _attack_focus_tab_html(bvid: str, fp_cmt: set[str] = frozenset()) -> str:
    """「争执焦点」标签页（独立成页，不与问题评论榜混排——连线需要宽敞的两列排布）"""
    return _attack_focus_html(_attack_focus(bvid, fp_cmt)) \
        or '<p class="empty-note">本视频无问题评论攻击边</p>'


def _cmt_war_html(bvid: str, aid: int | None, fp_cmt: set[str] = frozenset()) -> str:
    """「问题评论榜」标签页（独立成页）：全部问题评论按热度加权排序，楼中楼附父评原文。
    （争执焦点已独立为「争执焦点」标签页，见 _attack_focus_tab_html）"""
    up_mid = 0
    with closing(get_db()) as conn:
        vrow = conn.execute("SELECT video_info_json FROM videos WHERE bvid = ?", (bvid,)).fetchone()
    try:
        up_mid = int((json.loads(vrow["video_info_json"]).get("owner") or {}).get("mid", 0)) if vrow else 0
    except Exception:
        up_mid = 0
    return _problem_comment_board_html(*_problem_comment_board(bvid, fp_cmt), aid, up_mid) \
        or '<p class="empty-note">本视频无问题评论命中</p>'


def _group_wordcloud(texts: list[str], top: int = 30) -> list[list]:
    """评论组词云数据：简单分词（2+连续中文字）词频 Top N；过滤只出现1次的噪声，
    全部只出现一次时退化为 Top N（保证有内容可看）。返回 [[词, 次数], ...]"""
    cnt: Counter = Counter()
    for t in texts:
        t = (t or "").strip()
        # 楼中楼原文带「回复 @xx :」前缀，先剥离避免「回复」成为噪声高频词
        t = re.sub(r"^回复\s*@[^\s:：]+\s*[:：]", "", t)
        # 剥离B站表情占位符（[笑哭]/[给心心]/[装扮名_xxx] 等方括号段），否则分词产出
        # 「笑哭」「应援装扮」这类表情噪声词压过真实话题词
        t = re.sub(r"\[[^\[\]]{1,30}\]", " ", t)
        # 剥离正文中的 @提及（含「回复 @xx :」前缀以外的裸 @昵称），昵称不是话题
        t = re.sub(r"@[^\s@:：，,\[\]]+", " ", t)
        cnt.update(w for w in _tokenize(t) if w != "回复")
    words = [(w, c) for w, c in cnt.most_common() if c >= 2][:top]
    if len(words) < 8:
        # 高频词太少的组用词频1的词补齐到至少8个，避免词云只有一两个词
        seen = {w for w, _ in words}
        words += [(w, c) for w, c in cnt.most_common() if c < 2 and w not in seen][:8 - len(words)]
    return [[w, c] for w, c in words]


def _hot_comments_html(bvid: str, aid: int | None, fp_cmt: set[str] = frozenset()) -> str:
    """高回复评论标签页 HTML（服务端渲染，spec：单独成页的潜在争执热点）

    回复树完整展示不截断：默认折叠到固定高度，「展开/折叠」按钮切换（hotToggle）。
    树内直接显示用户名（comments.uname 落库），楼主/UP主出现时分色标注；
    问题评论带误报标记按钮（P2-a），已标记误报的不计入统计。
    （争执焦点/问题评论榜已各自独立成页，见 _attack_focus_tab_html / _cmt_war_html）"""
    data = _hot_comments(bvid, aid)
    up_mid = data.get("up_mid", 0)
    if data["legacy"]:
        return ('<p class="empty-note">该视频为旧版本分析，评论回复数未留存；'
                '点「🔄 重新生成报告」重跑后可查看高回复评论</p>')
    if not data["items"]:
        return (f'<p class="empty-note">本视频没有回复数 ≥ {HOT_COMMENT_MIN_REPLIES} 的评论'
                f'（阈值 HOT_COMMENT_MIN_REPLIES 可在 config.py 调整）</p>')
    items_html = []
    problem_total = 0
    for it in data["items"]:
        date = datetime.fromtimestamp(it["ctime"]).strftime("%Y-%m-%d") if it["ctime"] else ""
        # 误报标记（P2-a）：已标记的不计入问题评论统计，chip 划线并带撤销按钮
        it_marked = str(it["rpid"]) in fp_cmt
        problem_total += (1 if it["problem"] and not it_marked else 0) + sum(
            1 for s in it["subs"] if s["problem"] and str(s["rpid"]) not in fp_cmt)
        if it["subs"]:
            tree_html = _reply_tree_html(it["subs"], it["uid"], up_mid, fp_cmt)
            subs_html = f'''
                <div class="hot-subs collapsed">
                    <div class="hot-sub-label">回复树（已采集 {len(it["subs"]):,} 条）</div>
                    {tree_html}
                </div>
                <button class="hot-toggle" onclick="hotToggle(this)">展开全部回复 ▾</button>'''
        else:
            subs_html = '<div class="hot-subs"><div class="hot-sub-label ov-none">暂无子回复被采集</div></div>'
        origin = (f'<a class="hot-origin" href="https://www.bilibili.com/video/av{aid}#reply{it["rpid"]}" '
                  f'target="_blank" rel="noopener">去B站围观 ↗</a>') if aid else ""
        # 评论组讨论主题词云（主楼+全部回复的词频，悬停静止弹窗用；分词只产出纯中文词，JSON 注入单引号属性安全）
        wc = _group_wordcloud([it["content"]] + [s["content"] for s in it["subs"]])
        wc_attr = f" data-wc='{json.dumps(wc, ensure_ascii=False)}'" if wc else ""
        items_html.append(f'''
            <div class="hot-item"{wc_attr}>
                <div class="hot-head">
                    <a class="hot-author" href="/user/{esc(it["uid"])}"
                       title="查看该用户在已分析视频中的互动时间线">{esc(it["name"])}</a>
                    {_role_badges(it["uid"], 0, up_mid)}
                    <span class="hot-badge">💬 {it["reply_count"]:,} 条回复</span>
                    <span class="dm-time">👍{it["like"]:,} · {date}</span>
                    {_problem_chip(it["problem"], it_marked)}
                    {_fp_btn("cmt", it["rpid"], it_marked) if it["problem"] else ""}
                    {origin}
                </div>
                <div class="hot-content">{esc(it["content"])}</div>
                {subs_html}
            </div>''')
    problem_note = f' · 其中问题评论 {problem_total} 条已标注' if problem_total else ""
    return f'''
        <div class="hot-note">💥 回复数特别多的评论往往意味着争执：以下为该视频回复数 ≥
            {HOT_COMMENT_MIN_REPLIES} 的评论（按回复数降序，最多 {HOT_COMMENT_MAX_SHOW} 条）。
            悬停在某个评论组上静止片刻，可弹出该组讨论主题词云。</div>
        <div class="hot-stats">共 {len(data["items"])} 条高回复评论 · 合计 {data["total_replies"]:,} 条回复{problem_note}</div>
        <div class="hot-list">{"".join(items_html)}</div>'''


def _user_timeline(uid: int) -> dict:
    """用户互动时间线数据（/user/<uid> 页）：该用户在全部已分析视频中的弹幕/评论足迹，
    按最近互动时间倒序——「近期在哪些视频上有互动」的直接答案。

    与卡片「其他视频足迹」（按活跃度、限 5 个、无时间）互补而非重复：
    时间线覆盖全部视频、带发送时间、含评论-only 用户（senders 无行只有 comments 行）。
    弹幕计数取 senders.danmaku_count 与 danmaku 表行数两者较大值（旧版本视频只有前者）；
    时间戳只在新版 danmaku 表有，旧视频 last_ts=0（排序垫底并标注时间未留存）。"""
    with closing(get_db()) as conn:
        u = conn.execute("SELECT name, level FROM users WHERE uid = ?", (uid,)).fetchone()
        name = (u["name"] if u and u["name"] else f"UID:{uid}")
        level = (u["level"] or 0) if u else 0

        # 弹幕侧：senders 表覆盖全部解析用户（含旧版本视频）；时间戳与样本只在 danmaku 表有
        dm_agg: dict[str, dict] = {}
        for r in conn.execute(
                "SELECT bvid, SUM(danmaku_count) AS cnt FROM senders WHERE uid = ? GROUP BY bvid",
                (uid,)).fetchall():
            dm_agg[r["bvid"]] = {"dm_count": r["cnt"] or 0, "dm_last": 0}
        for r in conn.execute('''
                SELECT d.bvid, MAX(d.timestamp) AS last_ts, COUNT(*) AS cnt
                FROM senders s JOIN danmaku d ON d.bvid = s.bvid AND d.mid_hash = s.mid_hash
                WHERE s.uid = ? GROUP BY d.bvid
        ''', (uid,)).fetchall():
            ent = dm_agg.setdefault(r["bvid"], {"dm_count": 0, "dm_last": 0})
            ent["dm_last"] = r["last_ts"] or 0
            ent["dm_count"] = max(ent["dm_count"], r["cnt"])

        # 评论侧（comments 表；评论-only 用户只有这一侧数据）
        cmt_agg: dict[str, dict] = {}
        for r in conn.execute(
                "SELECT bvid, COUNT(*) AS cnt, MAX(ctime) AS last_ts FROM comments WHERE uid = ? GROUP BY bvid",
                (uid,)).fetchall():
            cmt_agg[r["bvid"]] = {"cmt_count": r["cnt"] or 0, "cmt_last": r["last_ts"] or 0}

        bvids = set(dm_agg) | set(cmt_agg)
        result = {"uid": uid, "name": name, "level": level, "items": [],
                  "total_dm": sum(e["dm_count"] for e in dm_agg.values()),
                  "total_cmt": sum(e["cmt_count"] for e in cmt_agg.values()),
                  "total_videos": len(bvids), "more": 0}
        if not bvids:
            return result

        titles = {r["bvid"]: r["title"] or "" for r in conn.execute(
            f"SELECT bvid, title FROM videos WHERE bvid IN ({','.join('?' * len(bvids))})",
            tuple(sorted(bvids))).fetchall()}
        mid_hashes_by_bvid: dict[str, list[str]] = {}
        for r in conn.execute("SELECT bvid, mid_hash FROM senders WHERE uid = ?", (uid,)).fetchall():
            mid_hashes_by_bvid.setdefault(r["bvid"], []).append(r["mid_hash"])

        ranked = sorted(bvids, key=lambda b: max(
            dm_agg.get(b, {}).get("dm_last", 0), cmt_agg.get(b, {}).get("cmt_last", 0)), reverse=True)
        kept, rest = ranked[:USER_TIMELINE_MAX_VIDEOS], ranked[USER_TIMELINE_MAX_VIDEOS:]
        result["more"] = len(rest)

        for b in kept:
            dm = dm_agg.get(b, {})
            cm = cmt_agg.get(b, {})
            dm_samples: list[dict] = []
            for mh in mid_hashes_by_bvid.get(b, []):
                dm_samples.extend(
                    {"content": r["content"], "ts": r["timestamp"], "vt": r["time"]}
                    for r in conn.execute(
                        "SELECT content, timestamp, time FROM danmaku "
                        "WHERE bvid = ? AND mid_hash = ? ORDER BY timestamp DESC LIMIT ?",
                        (b, mh, USER_TIMELINE_SAMPLES)).fetchall())
                if len(dm_samples) >= USER_TIMELINE_SAMPLES:
                    break
            cmt_samples = [{"content": r["content"], "ts": r["ctime"]} for r in conn.execute(
                "SELECT content, ctime FROM comments WHERE bvid = ? AND uid = ? "
                "ORDER BY ctime DESC LIMIT ?", (b, uid, USER_TIMELINE_SAMPLES)).fetchall()]
            result["items"].append({
                "bvid": b, "title": titles.get(b) or b,
                "dm_count": dm.get("dm_count", 0), "dm_samples": dm_samples[:USER_TIMELINE_SAMPLES],
                "cmt_count": cm.get("cmt_count", 0), "cmt_samples": cmt_samples,
                "last_ts": max(dm.get("dm_last", 0), cm.get("cmt_last", 0)),
            })
    return result



# ========== 路由 ==========

@app.route("/")
def index():
    """首页：已分析视频列表（标题/BV号/时长/播放量/分析时间/弹幕数/画像人数/高/中风险人数）。

    搜索/列头排序/分页由 static/index.js 前端实现：每行带 data-title（标题+BV号小写）、
    data-time/data-dm/data-profiles 供排序；超 20 条分页。"""
    conn = get_db()
    rows = conn.execute('''
        SELECT v.bvid, v.title, v.created_at, v.duration, v.view_count,
               (SELECT COUNT(*) FROM danmaku d WHERE d.bvid = v.bvid) AS dm_count,
               (SELECT COUNT(DISTINCT s.uid) FROM senders s
                WHERE s.bvid = v.bvid AND s.uid IS NOT NULL) AS profile_count,
               (SELECT COUNT(*) FROM senders s WHERE s.bvid = v.bvid AND s.spam_level = '高') AS spam_high,
               (SELECT COUNT(*) FROM senders s WHERE s.bvid = v.bvid AND s.spam_level = '中') AS spam_mid
        FROM videos v ORDER BY v.created_at DESC
    ''').fetchall()
    conn.close()
    items = "".join(f'''<tr data-title="{esc(((r["title"] or "") + " " + r["bvid"]).lower())}"
        data-time="{esc(r["created_at"])}" data-dm="{r["dm_count"]}" data-profiles="{r["profile_count"]}">
        <td><a href="/video/{esc(r["bvid"])}">{esc(r["title"] or r["bvid"])}</a></td>
        <td>{esc(r["bvid"])}</td>
        <td>{_fmt_duration(r["duration"])}</td>
        <td>{(r["view_count"] or 0):,}</td>
        <td>{esc(r["created_at"])}</td>
        <td>{r["dm_count"]:,}</td>
        <td>{r["profile_count"]}</td>
        <td>{r["spam_high"]} / {r["spam_mid"]}</td>
    </tr>''' for r in rows)
    body = items or '<tr><td colspan="8" class="empty-note">暂无已分析视频，先运行 python run.py &lt;BV号&gt;</td></tr>'

    # 跨视频重叠用户面板：在多个已分析视频中都出现过的发送者（水军/情绪带节奏视角）
    overlaps = _cross_video_overlaps()
    overlap_html = ""
    if overlaps:
        # 每个视频一个 <details>：标题+弹幕/评论计数为摘要，展开可见 TA 在该视频的
        # 弹幕/评论明细样本（含问题评论标注）；旧版本分析标注明细未留存
        def _xv_video_block(v: dict) -> str:
            dm_lis = "".join(f"<li>{esc(c)}</li>" for c in v["dm_samples"])
            if not dm_lis:
                dm_lis = ('<li class="ov-none">弹幕明细未留存（该视频为旧版本分析）</li>'
                          if v["legacy"] and v["dm_count"] else '<li class="ov-none">无弹幕样本</li>')
            cmt_lis = "".join(
                f'<li>{esc(c["content"])} <span class="dm-time">👍{c["like"]:,}</span>'
                f'{_problem_chip(c["problem"])}</li>' for c in v["cmt_samples"])
            if not cmt_lis:
                cmt_lis = ('<li class="ov-none">评论未留存（该视频为旧版本分析）</li>'
                           if v["legacy"] else '<li class="ov-none">无评论</li>')
            return f'''<details class="xv-detail">
                <summary><a href="/video/{esc(v["bvid"])}">《{esc(v["title"])}》</a>
                    <span class="xv-counts">弹幕 {v["dm_count"]:,} · 评论 {v["cmt_count"]:,}</span></summary>
                <div class="xv-sec">💬 弹幕样本</div><ul class="xv-list">{dm_lis}</ul>
                <div class="xv-sec">📝 评论样本</div><ul class="xv-list">{cmt_lis}</ul>
            </details>'''

        rows_html = "".join(f'''<tr>
            <td><a href="/user/{esc(o["uid"])}">{esc(o["name"])}</a></td>
            <td>{o["video_count"]}</td>
            <td>{o["total_dm"]:,}</td>
            <td class="xv-videos">{"".join(_xv_video_block(v) for v in o["videos"])}</td>
        </tr>''' for o in overlaps)
        overlap_html = f'''
    <div class="xv-panel">
        <h2>🔁 跨视频重叠用户（{len(overlaps)} 人）</h2>
        <p class="xv-note">在 ≥ {CROSS_VIDEO_MIN_VIDEOS} 个已分析视频中都发过弹幕的发送者——跨视频重复出现的账号是水军/带节奏的重点嫌疑对象。点视频条目可展开 TA 在该视频里的弹幕/评论明细。</p>
        <table class="video-table xv-table">
            <thead><tr><th>用户</th><th>涉及视频数</th><th>总弹幕数</th><th>出现的视频（点条目展开明细）</th></tr></thead>
            <tbody>{rows_html}</tbody>
        </table>
    </div>'''
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>B站弹幕用户画像分析 - 视频列表</title>
<style>{REPORT_CSS}</style>
<link rel="stylesheet" href="/static/index.css">
</head>
<body>
<div class="container">
    <div class="header"><h1>🎬 B站弹幕用户画像分析</h1><div class="meta">已分析视频列表</div></div>
    <div class="idx-controls">
        <input id="idxSearch" class="search-input" placeholder="搜索标题 / BV号...">
    </div>
    <table class="video-table">
        <thead><tr><th>标题</th><th>BV号</th><th>时长</th><th>播放量</th>
            <th data-sort="time">分析时间</th><th data-sort="dm">弹幕数</th><th data-sort="profiles">画像人数</th>
            <th>高/中风险</th></tr></thead>
        <tbody id="videoTbody">{body}</tbody>
    </table>
    <div class="idx-pager">
        <button id="idxPrev" class="pager-btn">上一页</button>
        <span id="idxPageInfo"></span>
        <button id="idxNext" class="pager-btn">下一页</button>
    </div>
    {overlap_html}
</div>
<script src="/static/index.js"></script>
</body>
</html>'''


@app.route("/video/<bvid>")
def video_page(bvid: str):
    """报告页：概览/用户画像/弹幕浏览器/问题弹幕榜/争执焦点/问题评论榜/高回复评论 七个标签页。

    整页 HTML 按 bvid 内存缓存（_PAGE_CACHE）：避免每次刷新重跑 _load_profiles/
    _attach_other_videos 逐用户查询；job（手动分析/重新生成）完成或删除时主动失效，
    且每次请求比对数据指纹，外部进程（run.py）落库导致的变化也能检出。"""
    page_fp = _page_fingerprint(bvid)
    with _PAGE_CACHE_LOCK:
        cached = _PAGE_CACHE.get(bvid)
    if cached is not None and cached[0] == page_fp:
        return cached[1]

    row = _load_video_row(bvid)
    if row is None:
        abort(404)
    try:
        video_info = json.loads(row["video_info_json"]) if row["video_info_json"] else {}
    except Exception:
        video_info = {}
    title = video_info.get("title") or row["title"] or bvid

    profiles = _load_profiles(bvid)
    _attach_other_videos(bvid, profiles)   # 跨视频足迹（渲染期注入，不落库）
    # 误报标记（P2-a）：弹幕侧按内容从 cringe 聚合扣除（用户疑似分随之降级）；
    # 评论侧传入高回复页（争执焦点/问题评论榜/回复树渲染时剔除或划线）
    fp = load_false_positives(bvid)
    fp_dm = {t for k, t in fp if k == "dm"}
    fp_cmt = {t for k, t in fp if k == "cmt"}
    fp_dm_used = _apply_danmaku_fp(profiles, fp_dm)
    profiles = sort_profiles_by_risk(profiles)
    stats = generate_summary_stats(profiles)
    chart = generate_chart_data(profiles)
    cards_html = "".join(generate_user_card(p) for p in profiles) or '<p class="empty-note">暂无画像数据</p>'
    hot_tab = _hot_comments_html(bvid, row["aid"], fp_cmt)   # 高回复评论页（潜在争执热点）
    attack_tab = _attack_focus_tab_html(bvid, fp_cmt)        # 争执焦点页（谁攻击谁+连线）
    cmtwar_tab = _cmt_war_html(bvid, row["aid"], fp_cmt)     # 问题评论榜（热度加权）
    board_html = (generate_cringe_board(profiles, fp_renderer=lambda c: _fp_btn("dm", c, False))
                  or '<p class="empty-note">本视频无问题弹幕命中</p>')
    board_html += _fp_dm_block(fp_dm_used)   # 已标记误报弹幕的撤销入口
    panel = _danmaku_panel_stats(bvid)
    density = _danmaku_density(bvid, row["duration"])   # 概览页弹幕密度时间轴
    rq = _resolve_quality(bvid)                          # 概览页解析质量区块
    dm_attrs = _danmaku_attr_stats(bvid)                 # 弹幕属性分布（mode/color）

    # CSV/JSON 导出下载链接：默认只显示最新一组，历史导出收进 <details> 折叠块（spec 6）
    latest, history = _export_links(bvid)

    def _dl_link(fname: str, ext: str) -> str:
        return f'<a class="filter-btn" href="/download/{esc(fname)}">{esc(ext)} 下载</a>'

    links = " ".join(_dl_link(f, e) for f, e in latest)
    if history:
        links += (f' <details class="dl-history"><summary>历史导出（{len(history)} 个）</summary>'
                  f'{" ".join(_dl_link(f, e) for f, e in history)}</details>')

    ai_count = sum(1 for p in profiles if p.get("ai_deep") or p.get("ai_analysis"))
    lv5_count = sum(1 for p in profiles if p.get("level", 0) >= 5)

    # 弹幕覆盖率（阶段2历史合并时写入 video_info；旧数据没有则不显示）
    coverage = video_info.get("danmaku_coverage")
    coverage_line = ""
    if coverage:
        coverage_line = (f" · 弹幕覆盖: 实时池 {coverage['realtime']:,} + "
                         f"历史新增 {coverage['history_new']:,} = {coverage['merged']:,} 条")

    # 弹幕覆盖率说明条（spec 7）：明示数据边界——实时池容量有限、历史快照回溯窗口有限、
    # 已解析发送者为按兴趣分阈值入选的子集
    coverage_note = (
        f'<div class="coverage-note">📊 数据边界：实时弹幕池仅保留最近若干条（容量由B站限定，'
        f'超出部分被顶出）；历史弹幕按日快照回溯，最多 {HISTORY_MAX_MONTHS} 个月 / {HISTORY_MAX_DAYS} 天，'
        f'更早的弹幕不在统计范围内。已解析发送者是按兴趣分阈值入选的子集，非全部弹幕发送者。</div>'
    )

    # 无属地数据时不渲染地域图
    region_canvas = ('<div class="chart-card"><h3>地域分布 Top10</h3><canvas id="regionChart"></canvas></div>'
                     if chart["region_labels"] else "")

    # 弹幕密度时间轴（概览页，宽幅）：无全量弹幕数据（旧版本分析）或时长未知时不渲染；
    # 点击柱条跳转视频对应时段（P1-a，前端 report.js onClick 处理）
    density_canvas = ('<div class="chart-card chart-wide"><h3>弹幕密度时间轴'
                      '<span class="chart-hint">（点击柱条跳转对应时段核验）</span></h3>'
                      '<canvas id="densityChart"></canvas></div>' if density else "")

    # 解析质量区块（概览页）：用户身份可信度——解析方式/置信度分布 + 碰撞风险人数
    rq_block = ""
    if rq:
        collision_cls = "rq-danger" if rq["collision"] else "rq-ok"
        rq_block = f'''
        <div class="rq-block">
            <h3>🔍 解析质量（用户身份可信度）</h3>
            <div class="rq-summary">已解析发送者 <b>{rq["resolved"]:,}</b> / 全部发送者
                <b>{rq["total_senders"]:,}</b> · 碰撞风险（CRC32反查）
                <b class="{collision_cls}">{rq["collision"]} 人</b></div>
            <div class="charts-grid rq-charts">
                <div class="chart-card"><h3>解析方式分布</h3><canvas id="rqMethodChart"></canvas></div>
                <div class="chart-card"><h3>置信度分布</h3><canvas id="rqConfChart"></canvas></div>
            </div>
        </div>'''

    # 弹幕浏览器标签页：统计面板服务端渲染；表格容器由 Task 6 前端填充
    if panel["total"] == 0:
        # 旧数据兼容（spec 3）：历史视频 danmaku 表无数据
        danmaku_tab = '<p class="empty-note">该视频为旧版本分析，无全量弹幕数据，--force 重采后可浏览</p>'
    else:
        top10_html = "、".join(
            f"""<a onclick="filterSender('{esc(t["mid_hash"])}')">{esc(t["name"])}({t["count"]})</a>"""
            for t in panel["top10"])
        # 弹幕属性分布（mode/color 已入库）：颜色 Top12 色块并入「问题弹幕类别分布」卡片
        # （同属内容特征维度，合并展示）；模式分布单独成卡
        color_chips = ""
        mode_card = ""
        if dm_attrs:
            if dm_attrs["colors"]:
                color_chips = ('<h4 class="dm-sub-h">弹幕颜色 Top12</h4><div class="dm-color-chips">'
                               + "".join(
                                   f'<span class="dm-color-chip"><i style="background:{esc(c)}"></i>{esc(c)} ×{n:,}</span>'
                                   for c, n in dm_attrs["colors"]) + '</div>')
            if dm_attrs["mode"]:
                mode_card = '<div class="chart-card"><h3>弹幕模式分布</h3><canvas id="dmModeChart"></canvas></div>'
        danmaku_tab = f'''
        <div class="dm-panel">
            <div class="stat-card"><div class="num">{panel["total"]:,}</div><div class="label">总弹幕数</div></div>
            <div class="stat-card"><div class="num">{panel["merged"]:,}</div><div class="label">合并后行数</div></div>
            <div class="stat-card"><div class="num">{panel["senders"]:,}</div><div class="label">独立发送者</div></div>
            <div class="stat-card"><div class="num">{panel["resolved"]:,}</div><div class="label">已解析发送者</div></div>
        </div>
        <div class="charts-grid">
            <div class="chart-card"><h3>问题弹幕类别分布</h3><canvas id="dmCatChart"></canvas>{color_chips}</div>
            <div class="chart-card"><h3>发送者弹幕数 Top10（点击筛选）</h3><div class="top10-list">{top10_html}</div></div>
            {mode_card}
        </div>
        <div class="dm-controls">
            <input id="dmSearch" class="search-input" placeholder="搜索弹幕内容...">
            <input id="dmSender" class="search-input" placeholder="发送者（昵称/UID/mid_hash）">
            <select id="dmCategory"><option value="">全部类别</option>{"".join(f'<option value="{esc(c)}">{esc(c)}</option>' for c in PROBLEM_CATEGORY_COLORS)}</select>
            <select id="dmSpam"><option value="">全部风险</option><option>高</option><option>中</option><option>低</option><option>未分析</option></select>
            <label><input type="checkbox" id="dmAnalyzed"> 只看已解析</label>
            <select id="dmSort">
                <option value="video_time">视频时间</option>
                <option value="send_time">发送时间</option>
                <option value="dup_count">重复次数</option>
                <option value="sender_count">发送者弹幕数</option>
            </select>
            <select id="dmOrder"><option value="asc">升序</option><option value="desc">降序</option></select>
            <button id="dmAnalyzeBtn" class="filter-btn" onclick="startAnalysis()">分析选中发送者</button>
            <span id="dmAnalyzeStatus"></span>
        </div>
        <div id="dmFailBar" class="fail-detail" style="display:none">
            <div>部分发送者分析失败：</div>
            <ul id="dmFailList"></ul>
            <button id="dmRetryBtn" class="filter-btn">重试失败项</button>
            <button id="dmReloadBtn" class="filter-btn">刷新查看结果</button>
        </div>
        <div class="dm-table-wrap">
            <table class="dm-table">
                <thead><tr><th><input type="checkbox" id="dmCheckAll" title="全选当前页"></th><th>弹幕内容</th><th>发送者</th><th>视频时间</th><th>发送时间</th><th>类别</th><th>刷屏</th></tr></thead>
                <tbody id="dmTbody"></tbody>
            </table>
            <div id="dmSpinner" class="dm-spinner" style="display:none">弹幕加载中…</div>
            <div class="dm-pager">
                <button id="dmPrev" onclick="dmPage(-1)">上一页</button>
                <span id="dmPageInfo"></span>
                <button id="dmNext" onclick="dmPage(1)">下一页</button>
                <input id="dmGoto" type="number" min="1" placeholder="页码">
                <button onclick="dmGotoPage()">跳转</button>
                <select id="dmPageSize">
                    <option value="50">50 条/页</option>
                    <option value="100" selected>100 条/页</option>
                    <option value="200">200 条/页</option>
                </select>
            </div>
            <div id="dmError" class="dm-error-bar" style="display:none">
                <span id="dmErrorText"></span>
                <button class="dm-retry-btn" onclick="loadDanmaku()">重试</button>
            </div>
        </div>'''

    # 数据经内联 <script>window.__DATA__</script> 注入，静态 /static/report.js 读取（spec 8）
    page_data = {
        "chart": chart,
        "categories": panel.get("categories", {}),
        "categoryColors": PROBLEM_CATEGORY_COLORS,
        "upWordcloud": up_wordcloud_data(profiles),
        "bvid": bvid,
        "density": density,
        "resolveQuality": rq,
        "dmMode": dm_attrs["mode"] if dm_attrs else None,
    }

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)} - B站弹幕用户画像分析</title>
<script src="/static/chart.umd.min.js"></script>
<script src="/static/wordcloud2.min.js"></script>
<style>{REPORT_CSS}</style>
<link rel="stylesheet" href="/static/report.css">
</head>
<body>
<div class="container">
    <div class="header">
        <h1><a href="/" style="color:white;text-decoration:none">🎬 B站弹幕用户画像分析</a></h1>
        <div class="meta">
            <strong>{esc(title)}</strong> · BV: {esc(bvid)} · 播放: {video_info.get('stat', {}).get('view', 0):,} ·
            弹幕: {video_info.get('stat', {}).get('danmaku', 0):,} ·
            评论: {video_info.get('stat', {}).get('reply', 0):,} ·
            分析用户数: {stats['total']} · 大会员: {stats['vip_count']} ·
            刷屏用户: {stats['spam_levels'].get('高', 0) + stats['spam_levels'].get('中', 0)} ·
            AI画像: {ai_count}{coverage_line}
        </div>
    </div>

    <div class="tab-bar">
        <button class="tab-btn active" data-tab="overview" onclick="switchTab('overview')">概览</button>
        <button class="tab-btn" data-tab="users" onclick="switchTab('users')">用户画像</button>
        <button class="tab-btn" data-tab="danmaku" onclick="switchTab('danmaku')">弹幕浏览器</button>
        <button class="tab-btn" data-tab="cringe" onclick="switchTab('cringe')">问题弹幕榜</button>
        <button class="tab-btn" data-tab="attack" onclick="switchTab('attack')">争执焦点</button>
        <button class="tab-btn" data-tab="cmtwar" onclick="switchTab('cmtwar')">问题评论榜</button>
        <button class="tab-btn" data-tab="hot" onclick="switchTab('hot')">高回复评论</button>
    </div>

    <div id="tab-overview" class="tab-pane active">
        <div class="ov-actions">
            <a class="filter-btn" href="/">← 返回首页</a>
            <button class="filter-btn" onclick="reportRegen()">🔄 重新生成报告</button>
            <button class="filter-btn btn-danger" onclick="reportDelete()">🗑 删除报告</button>
            <span class="ov-dl">{links}</span>
            <span id="reportJobStatus"></span>
        </div>
        {coverage_note}
        <div class="stats-grid">
            <div class="stat-card"><div class="num">{stats['total']}</div><div class="label">分析用户</div></div>
            <div class="stat-card"><div class="num">{stats['vip_count']}</div><div class="label">大会员</div></div>
            <div class="stat-card"><div class="num">{lv5_count}</div><div class="label">Lv.5+</div></div>
            <div class="stat-card"><div class="num">{stats['spam_levels'].get('高', 0)}</div><div class="label">重度刷屏</div></div>
            <div class="stat-card"><div class="num">{stats['spam_levels'].get('中', 0)}</div><div class="label">中度刷屏</div></div>
            <div class="stat-card"><div class="num">{ai_count}</div><div class="label">AI画像</div></div>
        </div>
        {density_canvas}
        <div class="charts-grid">
            <div class="chart-card"><h3>用户等级分布</h3><canvas id="levelChart"></canvas></div>
            <div class="chart-card"><h3>刷屏风险分布</h3><canvas id="spamChart"></canvas></div>
            <div class="chart-card"><h3>用户标签 Top10</h3><canvas id="tagChart"></canvas></div>
            {region_canvas}
        </div>
        {rq_block}
    </div>

    <div id="tab-users" class="tab-pane">
        <div class="filter-bar">
            <button class="filter-btn active" data-filter="all" onclick="userFilter('all', this)">全部</button>
            <button class="filter-btn" data-filter="high-level" onclick="userFilter('high-level', this)">Lv.5+</button>
            <button class="filter-btn" data-filter="vip" onclick="userFilter('vip', this)">大会员</button>
            <button class="filter-btn" data-filter="official" onclick="userFilter('official', this)">认证用户</button>
            <button class="filter-btn" data-filter="spam" onclick="userFilter('spam', this)">刷屏用户</button>
            <button class="filter-btn" data-filter="creator" onclick="userFilter('creator', this)">UP主</button>
            <input id="userSearch" class="search-input" placeholder="搜索昵称/UID...">
            <select id="userSort" class="sort-select">
                <option value="risk">默认（风险序）</option>
                <option value="spam-score">刷屏分</option>
                <option value="danmaku">弹幕数</option>
                <option value="fans">粉丝数</option>
            </select>
            <span id="userResultCount" class="result-count"></span>
        </div>
        <div class="user-grid" id="userGrid">{cards_html}</div>
        <div id="userEmpty" class="empty-filter" style="display:none">没有符合条件的用户，请调整筛选或搜索词</div>
        <div id="userPager" class="user-pager"></div>
    </div>

    <div id="tab-danmaku" class="tab-pane">{danmaku_tab}</div>

    <div id="tab-cringe" class="tab-pane">{board_html}</div>

    <div id="tab-attack" class="tab-pane">{attack_tab}</div>

    <div id="tab-cmtwar" class="tab-pane">{cmtwar_tab}</div>

    <div id="tab-hot" class="tab-pane">{hot_tab}</div>

    <div id="wc-popup" class="wc-popup"><canvas id="wc-popup-canvas" width="276" height="216"></canvas></div>
</div>
<script>window.__DATA__ = {js_json(page_data)};</script>
<script src="/static/report.js"></script>
</body>
</html>'''
    with _PAGE_CACHE_LOCK:
        _PAGE_CACHE[bvid] = (page_fp, html)
    return html


@app.route("/user/<int:uid>")
def user_page(uid: int):
    """用户互动时间线页：该用户在全部已分析视频中的弹幕/评论足迹，按最近互动倒序
    （与卡片「其他视频足迹」互补：全量视频 + 时间维度 + 覆盖评论-only 用户）。

    数据边界：仅覆盖本系统已分析视频；B站无按用户查询互动历史的公开接口，
    弹幕本身匿名，无法从 UID 出发反查站内全部弹幕。"""
    data = _user_timeline(uid)
    name, level = data["name"], data["level"]

    def _fmt_ts(ts: int) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "时间未留存"

    if data["total_videos"] == 0:
        items_html = ('<p class="empty-note">该用户在本系统已分析视频中暂无互动记录。'
                      '注意：弹幕需该用户的 mid_hash 被解析进画像才会归户；'
                      '评论仅在评论落库之后的运行中留存。</p>')
        stats_line = ""
        more_html = ""
    else:
        items = []
        for it in data["items"]:
            dm_html = "".join(
                f'<li>{esc(s["content"])} <span class="tl-time">视频 {_fmt_video_time(s["vt"])} · {_fmt_ts(s["ts"])}</span></li>'
                for s in it["dm_samples"]) or (
                '<li class="ov-none">弹幕明细未留存（该视频为旧版本分析）</li>'
                if it["dm_count"] else '<li class="ov-none">无弹幕样本</li>')
            cmt_html = "".join(
                f'<li>{esc(s["content"])} <span class="tl-time">{_fmt_ts(s["ts"])}</span></li>'
                for s in it["cmt_samples"]) or '<li class="ov-none">无评论</li>'
            items.append(f'''
                <div class="tl-item">
                    <div class="tl-head">
                        <a class="tl-video" href="/video/{esc(it["bvid"])}">《{esc(it["title"])}》</a>
                        <span class="tl-last">最近互动 {_fmt_ts(it["last_ts"])}</span>
                    </div>
                    <div class="tl-sub">🎤 弹幕 {it["dm_count"]:,} 条（样本）</div>
                    <ul class="ov-list">{dm_html}</ul>
                    <div class="tl-sub">📝 评论 {it["cmt_count"]:,} 条（样本）</div>
                    <ul class="ov-list">{cmt_html}</ul>
                </div>''')
        items_html = "".join(items)
        more_html = (f'<div class="tl-more">另有 {data["more"]} 个视频也出现过（按最近互动排序，未展示）</div>'
                     if data["more"] else "")
        stats_line = (f'<div class="tl-stats">涉及视频 {data["total_videos"]} 个 · '
                      f'弹幕 {data["total_dm"]:,} 条 · 评论 {data["total_cmt"]:,} 条</div>')

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(name)} 互动时间线 - B站弹幕用户画像分析</title>
<style>{REPORT_CSS}</style>
<link rel="stylesheet" href="/static/report.css">
</head>
<body>
<div class="container">
    <div class="header">
        <h1><a href="/" style="color:white;text-decoration:none">🎬 B站弹幕用户画像分析</a></h1>
        <div class="meta">
            <strong>🕐 {esc(name)} 的互动时间线</strong><br>
            UID: {uid} | Lv.{esc(level)} |
            <a style="color:#ffd" href="https://space.bilibili.com/{uid}" target="_blank" rel="noopener">B站空间 ↗</a><br>
            <span style="opacity:0.8">仅覆盖本系统已分析视频；按最近互动时间倒序。</span>
        </div>
    </div>
    {stats_line}
    <div class="tl-list">{items_html}{more_html}</div>
</div>
</body>
</html>'''


@app.route("/api/video/<bvid>/analyze", methods=["POST"])
def api_analyze(bvid: str):
    """启动手动勾选发送者分析 job（spec 3.2）。起后台线程立即返回 job_id。

    未知 bvid → 404；空列表 → 400；无有效 Cookie → 503。
    """
    if _load_video_row(bvid) is None:
        return jsonify({"error": "未知视频"}), 404
    body = request.get_json(silent=True) or {}
    # 去重保序；非字符串/空串项丢弃
    mid_hashes = list(dict.fromkeys(
        h for h in body.get("mid_hashes", []) if isinstance(h, str) and h))
    if not mid_hashes:
        return jsonify({"error": "mid_hashes 为空"}), 400
    try:
        _get_client()
    except CookieInvalidError as e:
        return jsonify({"error": str(e) or "Cookie 失效，请先运行 python login.py"}), 503
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"kind": "analyze", "bvid": bvid, "total": len(mid_hashes), "done": 0, "current": "",
                        "errors": [], "finished": False, "results": []}
    threading.Thread(target=_run_analysis_job,
                     args=(job_id, bvid, mid_hashes), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/job/<job_id>")
def api_job(job_id: str):
    """轮询 job 进度（spec 3.2）：total/done/current/errors/finished/results"""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "未知 job"}), 404
        # errors/results 列表深拷一层：job 线程仍在 append，浅拷贝会把运行中的列表引用交给序列化
        return jsonify(dict(job, errors=list(job["errors"]), results=list(job["results"])))


def _has_running_job(bvid: str) -> bool:
    """该视频是否有未完成任务（手动分析/重新生成），有则拒绝删除与重复发起（spec 9）"""
    with JOBS_LOCK:
        return any(j.get("bvid") == bvid and not j.get("finished") for j in JOBS.values())


@app.route("/api/video/<bvid>/delete", methods=["POST"])
def api_delete_video(bvid: str):
    """删除该视频的全部分析数据与导出文件（spec 9：含共享缓存，不可恢复；有任务在跑则 409）"""
    if _load_video_row(bvid) is None:
        return jsonify({"error": "未知视频"}), 404
    if _has_running_job(bvid):
        return jsonify({"error": "该视频有正在运行的任务，请等待完成后再删除"}), 409
    counts = delete_video_data(bvid)
    _invalidate_page_cache(bvid)
    removed_files = 0
    for ext in ("csv", "json"):
        for f in glob.glob(os.path.join(REPORT_DIR, f"report_{bvid}_*.{ext}")):
            os.remove(f)
            removed_files += 1
    return jsonify({"ok": True, "removed_files": removed_files, **counts})


def _run_regen_job(job_id: str, bvid: str):
    """后台完整重跑分析流水线（等价 run.py --force；launch_web=False 防止再起 web 服务/开浏览器）"""
    try:
        run_analysis(bvid, force=True, max_users=None, launch_web=False)
        with JOBS_LOCK:
            JOBS[job_id].update(done=1, current="", finished=True)
        print(f"[RegenJob {job_id}] 完成: {bvid}")
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["errors"].append({"mid_hash": None, "error": str(e)})
            JOBS[job_id]["finished"] = True
        print(f"[RegenJob {job_id}] 失败: {e}")
    finally:
        _invalidate_page_cache(bvid)   # 无论成败都重取数据，报告页缓存失效


@app.route("/api/video/<bvid>/regenerate", methods=["POST"])
def api_regenerate(bvid: str):
    """重新生成报告：后台完整重跑分析流水线（spec 9；已有任务在跑则 409）"""
    if _load_video_row(bvid) is None:
        return jsonify({"error": "未知视频"}), 404
    if _has_running_job(bvid):
        return jsonify({"error": "该视频已有任务在运行，请等待完成"}), 409
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"kind": "regen", "bvid": bvid, "total": 1, "done": 0,
                        "current": "重新生成中（完整流水线）", "errors": [],
                        "finished": False, "results": []}
    threading.Thread(target=_run_regen_job, args=(job_id, bvid), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/video/<bvid>/false_positive", methods=["POST"])
def api_false_positive(bvid: str):
    """误报标记切换（P2-a）：body {kind: dm|cmt, target: 弹幕内容或评论rpid字符串}。

    幂等切换（已标记则撤销），返回 {"ok": true, "marked": bool}；报告页缓存即时失效。
    llm_cache 不动——误报是人工覆盖层，跨 --force 重跑保留。"""
    if _load_video_row(bvid) is None:
        return jsonify({"error": "未知视频"}), 404
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    target = str(data.get("target") or "")
    if kind not in ("dm", "cmt") or not target:
        return jsonify({"error": "参数错误：kind 须为 dm/cmt，target 不能为空"}), 400
    marked = toggle_false_positive(bvid, kind, target)
    _invalidate_page_cache(bvid)
    return jsonify({"ok": True, "marked": marked})


@app.route("/api/video/<bvid>/danmaku")
def api_danmaku(bvid: str):
    """弹幕 JSON API（spec 4）。

    合并规则：同一 mid_hash 相同 content 合并为一行带 dup_count（GROUP BY mid_hash, content）；
    不同 mid_hash 的相同内容不合并。
    参数：search（内容 LIKE）、sender（mid_hash 或昵称/UID 精确）、category（7类之一，
    命中该发送者的问题弹幕类别）、spam（高/中/低/未分析）、analyzed=1（只看已解析用户）、
    sort（video_time/send_time/dup_count/sender_count）、order（asc/desc）、page、page_size（50/100/200，默认100）。
    返回 {rows: [...], total: int, page: int}；每行 content/dup_count/mid_hash/uid/name/
    first_video_time/first_send_time/categories/spam_level/mode/color。
    """
    # 数据库锁定/查询异常 → 500 JSON（spec 7），与下方主查询同一降级口径
    try:
        video_row = _load_video_row(bvid)
    except sqlite3.Error as e:
        return jsonify({"error": f"数据库查询失败: {e}"}), 500
    # 返回 None 是"未知视频→404"的正常路径，不能与异常混淆
    if video_row is None:
        return jsonify({"error": "未知视频"}), 404

    args = request.args
    search = args.get("search", "").strip()
    sender = args.get("sender", "").strip()
    category = args.get("category", "").strip()
    spam = args.get("spam", "").strip()
    analyzed = args.get("analyzed") == "1"
    order = "DESC" if args.get("order", "asc").lower() == "desc" else "ASC"
    try:
        page = max(1, int(args.get("page", "1")))
    except ValueError:
        page = 1

    # 每页条数：白名单 50/100/200，非法值回退默认 PAGE_SIZE（spec 3 每页条数选择）
    try:
        page_size = int(args.get("page_size", str(PAGE_SIZE)))
    except ValueError:
        page_size = PAGE_SIZE
    if page_size not in (50, 100, 200):
        page_size = PAGE_SIZE

    # 数据库锁定 → 500 JSON（与主查询同一降级口径）
    try:
        meta = _sender_meta(bvid)
    except sqlite3.Error as e:
        return jsonify({"error": f"数据库查询失败: {e}"}), 500

    where = ["d.bvid = ?"]
    params: list = [bvid]

    if search:
        where.append("d.content LIKE ?")
        params.append(f"%{search}%")

    if sender:
        # mid_hash 精确（8位hex小写），否则按昵称/UID 精确匹配反查 mid_hash 集合
        hashes = set()
        if len(sender) == 8 and all(c in "0123456789abcdef" for c in sender.lower()):
            hashes.add(sender.lower())
        for h, m in meta.items():
            if m["name"] == sender or (m["uid"] is not None and str(m["uid"]) == sender):
                hashes.add(h)
        if not hashes:
            return jsonify({"rows": [], "total": 0, "page": page})
        where.append("d.mid_hash IN (%s)" % ",".join("?" * len(hashes)))
        params.extend(sorted(hashes))

    if category:
        # 命中该类别的发送者集合（categories 是 Python 侧解析，先求集合再 SQL 过滤）
        hashes = [h for h, m in meta.items() if category in m["categories"]]
        if not hashes:
            return jsonify({"rows": [], "total": 0, "page": page})
        where.append("d.mid_hash IN (%s)" % ",".join("?" * len(hashes)))
        params.extend(hashes)

    if spam in ("高", "中", "低"):
        where.append("s.spam_level = ?")
        params.append(spam)
    elif spam == "未分析":
        # senders 无行（未进解析名单）或旧缓存 spam_level 为 NULL 均属未分析
        where.append("s.spam_level IS NULL")

    if analyzed:
        where.append("s.uid IS NOT NULL")

    sort_col = {
        "video_time": "first_video_time",
        "send_time": "first_send_time",
        "dup_count": "dup_count",
        "sender_count": "sender_count",
    }.get(args.get("sort", "video_time"), "first_video_time")

    where_sql = " AND ".join(where)
    # sender_count：发送者在本视频的总弹幕数，子查询按 mid_hash 预聚合
    # 注意参数顺序：子查询的 bvid=? 在 SQL 文本中最先出现，绑定参数也要最先放
    base_sql = f'''
        FROM danmaku d
        LEFT JOIN senders s ON s.bvid = d.bvid AND s.mid_hash = d.mid_hash
        LEFT JOIN users u ON u.uid = s.uid
        LEFT JOIN (
            SELECT mid_hash, COUNT(*) AS cnt FROM danmaku WHERE bvid = ? GROUP BY mid_hash
        ) sc ON sc.mid_hash = d.mid_hash
        WHERE {where_sql}
        GROUP BY d.mid_hash, d.content
    '''
    count_sql = f"SELECT COUNT(*) FROM (SELECT d.mid_hash, d.content {base_sql})"
    rows_sql = f'''
        SELECT d.mid_hash, d.content, COUNT(*) AS dup_count,
               MIN(d.time) AS first_video_time, MIN(d.timestamp) AS first_send_time,
               MIN(d.mode) AS mode, MIN(d.color) AS color,
               s.uid AS uid, u.name AS name, s.spam_level AS spam_level,
               sc.cnt AS sender_count
        {base_sql}
        ORDER BY {sort_col} {order}, d.mid_hash, d.content
        LIMIT {page_size} OFFSET {(page - 1) * page_size}
    '''
    full_params = [bvid] + params

    # 数据库锁定/查询异常 → 500 JSON（spec 7），前端显示错误提示不崩溃
    # closing 保证异常路径连接也关闭，不泄漏
    try:
        with closing(get_db()) as conn:
            total = conn.execute(count_sql, full_params).fetchone()[0]
            raw_rows = conn.execute(rows_sql, full_params).fetchall()
    except sqlite3.Error as e:
        return jsonify({"error": f"数据库查询失败: {e}"}), 500

    rows = []
    for r in raw_rows:
        m = meta.get(r["mid_hash"], {})
        rows.append({
            "content": r["content"],
            "dup_count": r["dup_count"],
            "mid_hash": r["mid_hash"],
            "uid": r["uid"],
            "name": r["name"],
            "first_video_time": r["first_video_time"],
            "first_send_time": r["first_send_time"],
            "categories": m.get("categories", []),
            "spam_level": r["spam_level"] or "未分析",
            "sender_count": r["sender_count"],
            "mode": r["mode"] or 1,
            "color": r["color"] or "",
        })
    return jsonify({"rows": rows, "total": total, "page": page, "page_size": page_size})


@app.route("/download/<path:filename>")
def download(filename: str):
    """CSV/JSON 导出文件下载（仅允许 report_ 前缀文件，防目录外文件被下载）"""
    if not filename.startswith("report_") or "/" in filename or ".." in filename:
        abort(404)
    return send_from_directory(REPORT_DIR, filename, as_attachment=True)


@app.errorhandler(404)
def not_found(e):
    """未知 bvid / 未知路径 → 中文 404 页面（带站点样式与返回首页按钮，spec 6）"""
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>404 - B站弹幕用户画像分析</title>
<style>{REPORT_CSS}</style>
<link rel="stylesheet" href="/static/report.css">
</head>
<body>
<div class="container">
    <div class="header"><h1>404</h1><div class="meta">页面或视频不存在</div></div>
    <div class="nf-card">
        <p>视频不存在或尚未分析，请先运行 <code>python run.py &lt;BV号&gt;</code></p>
        <p><a class="filter-btn" href="/">返回首页</a></p>
    </div>
</div>
</body>
</html>''', 404


def _pid_file(port: int) -> str:
    """web 服务 pidfile 路径（按端口区分，PROFILER_PORT 多实例互不干扰）"""
    return os.path.join(DATA_DIR, f"web_{port}.pid")


def _write_pid(port: int):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_pid_file(port), "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def _clear_pid(port: int):
    try:
        os.remove(_pid_file(port))
    except OSError:
        pass


def _pid_is_webpy(pid: int) -> bool:
    """进程存活且命令行含 web.py：防止 PID 复用导致 --stop 误杀无关进程"""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return "web.py" in f.read().decode(errors="ignore")
    except OSError:
        return False


def _stop_server(port: int):
    """python web.py --stop：停止该端口的后台 web 服务并释放端口。

    读 pidfile → 校验进程确为本项目 web.py（否则只清残留 pidfile）→ SIGTERM
    优雅停止，1 秒内未退出则 SIGKILL 兜底；最后清理 pidfile。"""
    pid_path = _pid_file(port)
    try:
        with open(pid_path, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        print(f"[Web] 未发现 {port} 端口的服务记录（{pid_path} 不存在），服务可能未在运行")
        return
    if not _pid_is_webpy(pid):
        print(f"[Web] 记录中的进程 {pid} 已不存在或不是本项目的 web.py，清理残留 pidfile")
        _clear_pid(port)
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(10):
        if not _pid_is_webpy(pid):
            break
        time.sleep(0.1)
    if _pid_is_webpy(pid):
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.1)
    _clear_pid(port)
    print(f"[Web] 已停止 {port} 端口的 web 服务 (pid={pid})，端口已释放")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="B站弹幕用户画像 Web 报告服务")
    parser.add_argument("--stop", action="store_true",
                        help="停止本端口（PROFILER_PORT）后台运行的 web 服务并释放端口")
    args = parser.parse_args()
    port = int(os.environ.get("PROFILER_PORT", "8000"))
    if args.stop:
        _stop_server(port)
        sys.exit(0)

    init_db()

    # 端口占用检测提前于 pidfile 写入：若先写 pidfile 再 app.run 撞端口失败，
    # atexit 的 _clear_pid 会误删健康实例的 pidfile（--stop 随即失效）
    import socket
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            print(f"[Web] 端口 {port} 已被占用，请先 python web.py --stop 或用 PROFILER_PORT 换端口")
            sys.exit(1)

    _write_pid(port)
    atexit.register(lambda: _clear_pid(port))

    def _on_term(signum, frame):
        # SIGTERM（来自 web.py --stop）：清 pidfile 后退出，端口随即释放
        _clear_pid(port)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)
    print(f"[Web] 交互式报告服务已启动: http://127.0.0.1:{port}")
    print(f"[Web] 停止服务: python web.py --stop")
    app.run(host="127.0.0.1", port=port, debug=False)
