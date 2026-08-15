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
import sqlite3
import threading
import uuid
from contextlib import closing

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from flask import Flask, abort, jsonify, request, send_from_directory

from config import REPORT_DIR, LLM_API_KEY
from auth import load_cookie, verify_cookie
from api_client import BiliAPIClient
from storage import get_db, init_db
from storage import (load_senders, load_global_uid_map, save_global_uid,
                     save_sender, save_user_data, has_user_data, load_video_info)
from uid_resolver import resolve_sender, METHOD_CRC32_CRACK
from user_collector import collect_user_data
from profile_analyzer import analyze_profile
from spam_detector import batch_detect_spam
from llm_analyzer import LLMAnalyzer
from report import (REPORT_CSS, esc, js_json, generate_user_card, generate_summary_stats,
                    generate_chart_data, generate_cringe_board, sort_profiles_by_risk,
                    up_wordcloud_data, PROBLEM_CATEGORY_COLORS)

app = Flask(__name__)
PAGE_SIZE = 100  # 弹幕 API 固定每页条数（spec 4）


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
    verify_cookie 联网校验一次）；失败抛 CookieInvalidError，由路由按 503 处理"""
    global _client, _client_failed
    with _CLIENT_LOCK:
        if _client is not None:
            return _client
        if _client_failed:
            raise CookieInvalidError()
        cookie_dict = load_cookie()
        if not cookie_dict or not cookie_dict.get("SESSDATA"):
            _client_failed = True
            raise CookieInvalidError()
        refresh_token = cookie_dict.pop("_refresh_token", None)
        client = BiliAPIClient()
        client.update_cookies(cookie_dict)
        if refresh_token:
            client._refresh_token = refresh_token
        if not verify_cookie(client):
            _client_failed = True
            raise CookieInvalidError()
        _client = client
        return _client


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

    错误处理（spec 5）：单发送者失败记 errors 继续；Cookie 失效 job 整体终止并标记。
    写库全部走 storage 的 save_*（各自短事务），不持长连接。
    """
    def update(**kw):
        with JOBS_LOCK:
            JOBS[job_id].update(kw)

    def add_error(msg):
        with JOBS_LOCK:
            JOBS[job_id]["errors"].append(msg)
        print(f"[Job {job_id}] 失败: {msg}")

    def add_result(uid):
        with JOBS_LOCK:
            JOBS[job_id]["results"].append(uid)

    try:
        client = _get_client()
    except CookieInvalidError:
        add_error("Cookie 失效，请先运行 python login.py（job 已终止）")
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
                    add_error(f"{mid_hash}: UID 解析失败（{method}）")
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
                add_error(f"{mid_hash} (UID:{uid}): 采集失败 {user_data['error']}")
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
            add_error(f"{mid_hash}: {e}")
            update(done=i)

    update(finished=True, current="")
    print(f"[Job {job_id}] 完成: 成功 {len(JOBS[job_id]['results'])}/{len(mid_hashes)}")


# ========== 数据加载辅助 ==========

def _load_video_row(bvid: str):
    """videos 表整行；不存在返回 None"""
    with closing(get_db()) as conn:
        return conn.execute("SELECT * FROM videos WHERE bvid = ?", (bvid,)).fetchone()


def _load_profiles(bvid: str) -> list[dict]:
    """该视频已解析发送者的画像（senders.uid JOIN users.profile_json；同 uid 多 mid_hash 去重）"""
    conn = get_db()
    rows = conn.execute('''
        SELECT DISTINCT u.profile_json
        FROM senders s JOIN users u ON u.uid = s.uid
        WHERE s.bvid = ? AND s.uid IS NOT NULL
    ''', (bvid,)).fetchall()
    conn.close()
    profiles = []
    for r in rows:
        try:
            profiles.append(json.loads(r["profile_json"]))
        except Exception:
            continue
    return profiles


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


def _export_links(bvid: str) -> list[tuple[str, str]]:
    """data/reports/ 下 report_{bvid}_*.csv/.json 下载链接（spec 2：存在才显示，按时间倒序）"""
    links = []
    for ext in ("csv", "json"):
        files = sorted(glob.glob(os.path.join(REPORT_DIR, f"report_{bvid}_*.{ext}")), reverse=True)
        links.extend((os.path.basename(f), ext.upper()) for f in files)
    return links


# ========== 页面模板 CSS/JS（字面量全放常量，避免 f-string 大括号转义） ==========

INDEX_EXTRA_CSS = """
.video-table { width:100%; border-collapse:collapse; background:white; border-radius:12px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.04); }
.video-table th, .video-table td { text-align:left; padding:12px 16px; border-bottom:1px solid #f0f0f0; font-size:15px; }
.video-table th { color:#999; font-weight:500; }
.video-table a { color:#00a1d6; text-decoration:none; }
"""

VIDEO_EXTRA_CSS = """
.tab-bar { display:flex; gap:4px; margin-bottom:24px; border-bottom:2px solid #e0e0e0; flex-wrap:wrap; }
.tab-btn { padding:10px 24px; border:none; background:none; cursor:pointer; font-size:16px; color:#666; border-bottom:3px solid transparent; margin-bottom:-2px; }
.tab-btn.active { color:#00a1d6; border-bottom-color:#00a1d6; font-weight:600; }
.tab-pane { display:none; }
.tab-pane.active { display:block; }
.search-input { padding:8px 14px; border:2px solid #e0e0e0; border-radius:25px; font-size:14px; width:220px; outline:none; }
.search-input:focus { border-color:#00a1d6; }
.empty-note { color:#999; text-align:center; padding:40px; }
.dm-panel { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:15px; margin-bottom:20px; }
.top10-list { font-size:14px; color:#555; line-height:2; }
.top10-list a { color:#00a1d6; cursor:pointer; text-decoration:none; }
.flash-highlight { box-shadow:0 0 0 3px #fb7299 !important; transition:box-shadow .3s; }
"""

# 弹幕浏览器样式（Task 6）
DM_CSS = """
.dm-controls { display:flex; gap:10px; margin-bottom:15px; flex-wrap:wrap; align-items:center; }
.dm-controls select { padding:7px 10px; border:2px solid #e0e0e0; border-radius:8px; font-size:14px; background:white; }
.dm-table-wrap { background:white; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,0.04); padding:15px; overflow-x:auto; }
.dm-table { width:100%; border-collapse:collapse; font-size:14px; }
.dm-table th, .dm-table td { text-align:left; padding:8px 10px; border-bottom:1px solid #f0f0f0; vertical-align:top; }
.dm-table th { color:#999; font-weight:500; white-space:nowrap; }
.dm-table a { color:#00a1d6; text-decoration:none; cursor:pointer; }
.dm-pager { display:flex; gap:15px; align-items:center; justify-content:center; padding:15px; }
.dm-pager button { padding:6px 18px; border:2px solid #e0e0e0; border-radius:20px; background:white; cursor:pointer; }
.dm-pager button:disabled { opacity:0.4; cursor:default; }
.dm-error { color:#d32f2f; padding:15px; text-align:center; }
"""

VIDEO_JS = """
// 标签页切换
function switchTab(name) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
    if (name === 'full') initFullCharts();
}

// 概览图表
const chartData = __CHART_JSON__;
new Chart(document.getElementById('levelChart'), {type:'bar',
    data:{labels:chartData.level_labels, datasets:[{label:'人数', data:chartData.level_data, backgroundColor:'#00a1d6', borderRadius:6}]},
    options:{responsive:true, plugins:{legend:{display:false}}}});
new Chart(document.getElementById('spamChart'), {type:'doughnut',
    data:{labels:['低风险','中风险','高风险'], datasets:[{data:chartData.spam_data, backgroundColor:['#4caf50','#ff9800','#f44336']}]},
    options:{responsive:true}});
new Chart(document.getElementById('tagChart'), {type:'bar',
    data:{labels:chartData.tag_labels, datasets:[{label:'出现次数', data:chartData.tag_data, backgroundColor:'#ff9f43', borderRadius:6}]},
    options:{responsive:true, indexAxis:'y', plugins:{legend:{display:false}}}});
if (chartData.region_labels.length) {
    new Chart(document.getElementById('regionChart'), {type:'bar',
        data:{labels:chartData.region_labels, datasets:[{label:'人数', data:chartData.region_data, backgroundColor:'#fb7299', borderRadius:6}]},
        options:{responsive:true, indexAxis:'y', plugins:{legend:{display:false}}}});
}

// 问题弹幕类别分布小图（弹幕浏览器统计面板；无弹幕数据的旧视频无此 canvas）
const dmCatData = __CAT_JSON__;
const dmCatColors = __CAT_COLORS__;
const dmCatCanvas = document.getElementById('dmCatChart');
if (dmCatCanvas) {
    const catLabels = Object.keys(dmCatData);
    new Chart(dmCatCanvas, {type:'bar',
        data:{labels:catLabels, datasets:[{label:'命中人数', data:catLabels.map(k => dmCatData[k]),
              backgroundColor:catLabels.map(k => dmCatColors[k] || '#999'), borderRadius:6}]},
        options:{responsive:true, indexAxis:'y', plugins:{legend:{display:false}}}});
}

// UP主悬停词云弹窗
const upWcData = __UPWC_JSON__;
const popup = document.getElementById('wc-popup');
const popupCanvas = document.getElementById('wc-popup-canvas');
document.querySelectorAll('.up-chip').forEach(chip => {
    chip.addEventListener('mouseenter', function() {
        const upId = this.dataset.upid;
        const data = upWcData[upId];
        if (!data || data.length === 0) return;
        const rect = this.getBoundingClientRect();
        popup.style.display = 'block';
        popup.style.left = Math.min(rect.left, window.innerWidth - 320) + 'px';
        popup.style.top = (rect.bottom + 8) + 'px';
        const maxW = Math.max(...data.map(d => d[1]));
        const minW = Math.min(...data.map(d => d[1]));
        const scaled = data.map(d => [d[0], 10 + (d[1] - minW) / Math.max(maxW - minW, 1) * 50]);
        WordCloud(popupCanvas, {list: scaled, gridSize: 10, weightFactor: 1, fontFamily: 'sans-serif',
            color: () => ['#00a1d6','#fb7299','#ff9f43','#6c5ce7','#2e7d32'][Math.floor(Math.random()*5)],
            rotateRatio: 0, backgroundColor: '#ffffff', shape: 'circle', clearCanvas: true});
    });
    chip.addEventListener('mouseleave', function() { popup.style.display = 'none'; });
});

// 用户画像/完整报告：筛选按钮 + 昵称/UID 搜索（前端过滤，两页各一份卡片 DOM、状态独立）
const userFilterState = {users: 'all', full: 'all'};
const USER_SCOPE = {
    users: {grid: 'userGrid', input: 'userSearch'},
    full: {grid: 'fullUserGrid', input: 'fullSearch'},
};
function filter(type, el, scope) {
    scope = scope || 'users';
    userFilterState[scope] = type;
    document.querySelectorAll('#tab-' + scope + ' .filter-bar .filter-btn').forEach(b => b.classList.remove('active'));
    el.classList.add('active');
    applyUserFilter(scope);
}
function searchUsers(scope) { applyUserFilter(scope || 'users'); }
function applyUserFilter(scope) {
    scope = scope || 'users';
    const cfg = USER_SCOPE[scope];
    const kw = (document.getElementById(cfg.input).value || '').trim().toLowerCase();
    document.querySelectorAll('#' + cfg.grid + ' .user-card').forEach(card => {
        const level = parseInt(card.dataset.level) || 0;
        const isVip = card.dataset.vip === 'true';
        const spam = card.dataset.spam;
        const official = card.dataset.official === 'true';
        const isCreator = parseInt(card.querySelector('.stats-bar .stat:nth-child(4) .num')?.textContent || 0) > 0;
        let show = true;
        switch (userFilterState[scope]) {
            case 'all': show = true; break;
            case 'high-level': show = level >= 5; break;
            case 'vip': show = isVip; break;
            case 'official': show = official; break;
            case 'spam': show = spam !== '低'; break;
            case 'creator': show = isCreator; break;
        }
        if (show && kw) {
            const uname = (card.querySelector('.username')?.textContent || '').toLowerCase();
            const uid = (card.querySelector('.uid')?.textContent || '').toLowerCase();
            show = uname.includes(kw) || uid.includes(kw);
        }
        card.style.display = show ? '' : 'none';
    });
}

// 完整报告页图表：克隆概览页四个图（canvas 独立 id，数据复用 chartData），首次切入时懒初始化
let fullChartsInit = false;
function initFullCharts() {
    if (fullChartsInit || !document.getElementById('levelChart2')) return;
    fullChartsInit = true;
    new Chart(document.getElementById('levelChart2'), {type:'bar',
        data:{labels:chartData.level_labels, datasets:[{label:'人数', data:chartData.level_data, backgroundColor:'#00a1d6', borderRadius:6}]},
        options:{responsive:true, plugins:{legend:{display:false}}}});
    new Chart(document.getElementById('spamChart2'), {type:'doughnut',
        data:{labels:['低风险','中风险','高风险'], datasets:[{data:chartData.spam_data, backgroundColor:['#4caf50','#ff9800','#f44336']}]},
        options:{responsive:true}});
    new Chart(document.getElementById('tagChart2'), {type:'bar',
        data:{labels:chartData.tag_labels, datasets:[{label:'出现次数', data:chartData.tag_data, backgroundColor:'#ff9f43', borderRadius:6}]},
        options:{responsive:true, indexAxis:'y', plugins:{legend:{display:false}}}});
    if (chartData.region_labels.length && document.getElementById('regionChart2')) {
        new Chart(document.getElementById('regionChart2'), {type:'bar',
            data:{labels:chartData.region_labels, datasets:[{label:'人数', data:chartData.region_data, backgroundColor:'#fb7299', borderRadius:6}]},
            options:{responsive:true, indexAxis:'y', plugins:{legend:{display:false}}}});
    }
}

// 弹幕浏览器点击发送者跳转到用户画像卡片（锚点 id="uid-{uid}"，spec 4）
function gotoUser(uid) {
    switchTab('users');
    const el = document.getElementById('uid-' + uid);
    if (el) {
        el.scrollIntoView({behavior: 'smooth', block: 'center'});
        el.style.boxShadow = '0 0 0 3px #00a1d6';
        setTimeout(() => { el.style.boxShadow = ''; }, 2000);
    }
}

// 弹幕浏览器（JSON API + 前端渲染当前页，spec 4）
const BVID = "__BVID__";
const dmState = {page: 1};
let dmTimer = null;

function dmParams() {
    const p = new URLSearchParams();
    const search = document.getElementById('dmSearch').value.trim();
    const sender = document.getElementById('dmSender').value.trim();
    const cat = document.getElementById('dmCategory').value;
    const spam = document.getElementById('dmSpam').value;
    if (search) p.set('search', search);
    if (sender) p.set('sender', sender);
    if (cat) p.set('category', cat);
    if (spam) p.set('spam', spam);
    if (document.getElementById('dmAnalyzed').checked) p.set('analyzed', '1');
    p.set('sort', document.getElementById('dmSort').value);
    p.set('order', document.getElementById('dmOrder').value);
    p.set('page', dmState.page);
    return p.toString();
}

function escHtml(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function fmtVideoTime(sec) {
    const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
    return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

function loadDanmaku() {
    const err = document.getElementById('dmError');
    err.style.display = 'none';
    fetch('/api/video/' + encodeURIComponent(BVID) + '/danmaku?' + dmParams())
        .then(r => {
            if (!r.ok) return r.json().then(j => Promise.reject(new Error(j.error || ('HTTP ' + r.status))));
            return r.json();
        })
        .then(data => {
            const tbody = document.getElementById('dmTbody');
            tbody.innerHTML = data.rows.map(row => {
                const sender = row.uid
                    ? '<a onclick="gotoUser(' + row.uid + ')">' + escHtml(row.name || row.uid) + '</a><br><span class="dm-time">UID:' + row.uid + '</span>'
                    : '<span class="dm-time">' + escHtml(row.mid_hash) + '</span>';
                const dup = row.dup_count > 1 ? ' <span class="dm-time">×' + row.dup_count + '</span>' : '';
                const cats = (row.categories || []).map(c =>
                    '<span style="display:inline-block;background:' + (dmCatColors[c] || '#999') +
                    ';color:#fff;font-size:12px;border-radius:4px;padding:1px 8px;margin:1px 2px;">' +
                    escHtml(c) + '</span>').join('');
                const chk = '<input type="checkbox" class="dm-check" data-mid="' + escHtml(row.mid_hash) + '"' +
                    (dmSelected.has(row.mid_hash) ? ' checked' : '') + '>';
                return '<tr><td>' + chk + '</td><td>' + escHtml(row.content) + dup + '</td><td>' + sender + '</td><td>' +
                    fmtVideoTime(row.first_video_time) + '</td><td>' +
                    new Date(row.first_send_time * 1000).toLocaleString() + '</td><td>' + cats + '</td><td>' +
                    escHtml(row.spam_level) + '</td></tr>';
            }).join('') || '<tr><td colspan="7" class="empty-note">无匹配弹幕</td></tr>';
            dmBindChecks();
            const pages = Math.max(1, Math.ceil(data.total / 100));
            document.getElementById('dmPageInfo').textContent =
                '第 ' + data.page + ' / ' + pages + ' 页（共 ' + data.total + ' 行）';
            document.getElementById('dmPrev').disabled = data.page <= 1;
            document.getElementById('dmNext').disabled = data.page >= pages;
        })
        .catch(e => {
            err.textContent = '弹幕加载失败: ' + e.message;
            err.style.display = 'block';
        });
}

function dmPage(delta) { dmState.page = Math.max(1, dmState.page + delta); loadDanmaku(); }
function dmReload() { dmState.page = 1; loadDanmaku(); }

// 统计面板 Top10 点击 → 切到弹幕浏览器并筛选该发送者（spec 4）
function filterSender(midHash) {
    switchTab('danmaku');
    document.getElementById('dmSender').value = midHash;
    dmReload();
}

// 事件绑定（旧视频无全量弹幕时无 dmTbody，跳过）
if (document.getElementById('dmTbody')) {
    document.getElementById('dmSearch').addEventListener('input', () => { clearTimeout(dmTimer); dmTimer = setTimeout(dmReload, 400); });
    document.getElementById('dmSender').addEventListener('input', () => { clearTimeout(dmTimer); dmTimer = setTimeout(dmReload, 400); });
    ['dmCategory', 'dmSpam', 'dmSort', 'dmOrder', 'dmAnalyzed'].forEach(id =>
        document.getElementById(id).addEventListener('change', dmReload));
    document.getElementById('dmCheckAll').addEventListener('change', function() {
        document.querySelectorAll('.dm-check').forEach(c => {
            c.checked = this.checked;
            if (this.checked) dmSelected.add(c.dataset.mid); else dmSelected.delete(c.dataset.mid);
        });
        dmUpdateAnalyzeStatus();
    });
    loadDanmaku();
}

// 手动勾选分析（spec B）：勾选状态跨页保留在 dmSelected（key=mid_hash，天然去重）；
// job_id 存 sessionStorage，刷新页面后可继续轮询（spec 5；服务重启 job 丢失则提示后清除）
const dmSelected = new Set();
const DM_JOB_KEY = 'dmJob_' + BVID;

function dmUpdateAnalyzeStatus(text) {
    const el = document.getElementById('dmAnalyzeStatus');
    if (!el) return;  // 旧视频无弹幕面板时无此元素
    el.textContent = text !== undefined ? text
        : (dmSelected.size ? '已选 ' + dmSelected.size + ' 个发送者' : '');
}
function dmBindChecks() {
    document.querySelectorAll('.dm-check').forEach(c =>
        c.addEventListener('change', function() {
            if (this.checked) dmSelected.add(this.dataset.mid); else dmSelected.delete(this.dataset.mid);
            dmUpdateAnalyzeStatus();
        }));
}
function startAnalysis() {
    const mids = Array.from(dmSelected);
    if (!mids.length) { dmUpdateAnalyzeStatus('请先勾选弹幕行'); return; }
    dmUpdateAnalyzeStatus('正在启动分析...');
    fetch('/api/video/' + encodeURIComponent(BVID) + '/analyze', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mid_hashes: mids})
    }).then(r => r.json().then(j => ({ok: r.ok, j})))
      .then(({ok, j}) => {
          if (!ok) { dmUpdateAnalyzeStatus('启动失败: ' + (j.error || ('HTTP 未知错误'))); return; }
          sessionStorage.setItem(DM_JOB_KEY, j.job_id);
          dmSelected.clear();
          document.querySelectorAll('.dm-check').forEach(c => c.checked = false);
          pollJob(j.job_id);
      })
      .catch(e => dmUpdateAnalyzeStatus('启动失败: ' + e.message));
}
function pollJob(jobId) {
    fetch('/api/job/' + jobId)
        .then(r => r.json())
        .then(j => {
            if (j.error) {  // 服务重启 job 丢失
                dmUpdateAnalyzeStatus('任务状态查询失败: ' + j.error + '（数据可能已部分落库）');
                sessionStorage.removeItem(DM_JOB_KEY);
                return;
            }
            dmUpdateAnalyzeStatus('分析中 ' + j.done + '/' + j.total + (j.current ? '　' + j.current : ''));
            if (j.finished) {
                sessionStorage.removeItem(DM_JOB_KEY);
                // 新卡片需服务端渲染才有 DOM：记录待高亮 UID 后重载页面，
                // 加载恢复逻辑跳「完整报告」标签页并闪烁高亮（复用 gotoUser 高亮思路）
                sessionStorage.setItem('dmFlash_' + BVID, JSON.stringify(j.results || []));
                const errs = (j.errors && j.errors.length) ? '（' + j.errors.length + ' 条失败，详见服务端日志）' : '';
                sessionStorage.setItem('dmFlashMsg_' + BVID,
                    '手动分析完成: 成功 ' + (j.results || []).length + '/' + j.total + errs);
                location.reload();
            } else {
                setTimeout(() => pollJob(jobId), 2000);
            }
        })
        .catch(() => setTimeout(() => pollJob(jobId), 2000));  // 网络抖动重试，不丢 job
}

// 页面加载恢复：优先处理"分析完成重载"的跳转+高亮；否则有未完成 job 则继续轮询
(function() {
    const flashMsg = sessionStorage.getItem('dmFlashMsg_' + BVID);
    if (flashMsg) {
        sessionStorage.removeItem('dmFlashMsg_' + BVID);
        const uids = JSON.parse(sessionStorage.getItem('dmFlash_' + BVID) || '[]');
        sessionStorage.removeItem('dmFlash_' + BVID);
        switchTab('full');
        dmUpdateAnalyzeStatus(flashMsg);
        uids.forEach(uid => {
            const el = document.getElementById('full-uid-' + uid);
            if (el) {
                el.classList.add('flash-highlight');
                setTimeout(() => el.classList.remove('flash-highlight'), 3000);
            }
        });
        const first = document.getElementById('full-uid-' + uids[0]);
        if (first) first.scrollIntoView({behavior: 'smooth', block: 'center'});
        return;
    }
    const jobId = sessionStorage.getItem(DM_JOB_KEY);
    if (jobId) pollJob(jobId);
})();
"""


# ========== 路由 ==========

@app.route("/")
def index():
    """首页：已分析视频列表（标题/BV号/分析时间/弹幕数/画像人数/高/中风险人数）"""
    conn = get_db()
    rows = conn.execute('''
        SELECT v.bvid, v.title, v.created_at,
               (SELECT COUNT(*) FROM danmaku d WHERE d.bvid = v.bvid) AS dm_count,
               (SELECT COUNT(DISTINCT s.uid) FROM senders s
                WHERE s.bvid = v.bvid AND s.uid IS NOT NULL) AS profile_count,
               (SELECT COUNT(*) FROM senders s WHERE s.bvid = v.bvid AND s.spam_level = '高') AS spam_high,
               (SELECT COUNT(*) FROM senders s WHERE s.bvid = v.bvid AND s.spam_level = '中') AS spam_mid
        FROM videos v ORDER BY v.created_at DESC
    ''').fetchall()
    conn.close()
    items = "".join(f'''<tr>
        <td><a href="/video/{esc(r["bvid"])}">{esc(r["title"] or r["bvid"])}</a></td>
        <td>{esc(r["bvid"])}</td>
        <td>{esc(r["created_at"])}</td>
        <td>{r["dm_count"]:,}</td>
        <td>{r["profile_count"]}</td>
        <td>{r["spam_high"]} / {r["spam_mid"]}</td>
    </tr>''' for r in rows)
    body = items or '<tr><td colspan="6" class="empty-note">暂无已分析视频，先运行 python run.py &lt;BV号&gt;</td></tr>'
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>B站弹幕用户画像分析 - 视频列表</title>
<style>{REPORT_CSS}
{INDEX_EXTRA_CSS}</style>
</head>
<body>
<div class="container">
    <div class="header"><h1>🎬 B站弹幕用户画像分析</h1><div class="meta">已分析视频列表</div></div>
    <table class="video-table">
        <thead><tr><th>标题</th><th>BV号</th><th>分析时间</th><th>弹幕数</th><th>画像人数</th><th>高/中风险</th></tr></thead>
        <tbody>{body}</tbody>
    </table>
</div>
</body>
</html>'''


@app.route("/video/<bvid>")
def video_page(bvid: str):
    """报告页：概览/用户画像/弹幕浏览器/问题弹幕榜 四个标签页（spec 4）"""
    row = _load_video_row(bvid)
    if row is None:
        abort(404)
    try:
        video_info = json.loads(row["video_info_json"]) if row["video_info_json"] else {}
    except Exception:
        video_info = {}
    title = video_info.get("title") or row["title"] or bvid

    profiles = sort_profiles_by_risk(_load_profiles(bvid))
    stats = generate_summary_stats(profiles)
    chart = generate_chart_data(profiles)
    cards_html = "".join(generate_user_card(p) for p in profiles) or '<p class="empty-note">暂无画像数据</p>'
    board_html = generate_cringe_board(profiles) or '<p class="empty-note">本视频无问题弹幕命中</p>'
    panel = _danmaku_panel_stats(bvid)

    # 完整报告标签页（spec A）：卡片第二份 DOM 的锚点 id 改写为 full-uid- 防 DOM id 冲突
    full_cards_html = cards_html.replace('id="uid-', 'id="full-uid-')
    region_canvas2 = ('<div class="chart-card"><h3>地域分布 Top10</h3><canvas id="regionChart2"></canvas></div>'
                      if chart["region_labels"] else "")

    # CSV/JSON 导出下载链接（spec 2：指向 data/reports/ 同名前缀文件，存在才显示）
    links = " ".join(f'<a class="filter-btn" href="/download/{esc(fname)}">{esc(ext)} 下载</a>'
                     for fname, ext in _export_links(bvid))

    ai_count = sum(1 for p in profiles if p.get("ai_deep") or p.get("ai_analysis"))
    lv5_count = sum(1 for p in profiles if p.get("level", 0) >= 5)

    # 弹幕覆盖率（阶段2历史合并时写入 video_info；旧数据没有则不显示）
    coverage = video_info.get("danmaku_coverage")
    coverage_line = ""
    if coverage:
        coverage_line = (f"<br>弹幕覆盖: 实时池 {coverage['realtime']:,} 条 + "
                         f"历史快照去重后新增 {coverage['history_new']:,} 条 = 合并共 {coverage['merged']:,} 条")

    # 无属地数据时不渲染地域图
    region_canvas = ('<div class="chart-card"><h3>地域分布 Top10</h3><canvas id="regionChart"></canvas></div>'
                     if chart["region_labels"] else "")

    # 弹幕浏览器标签页：统计面板服务端渲染；表格容器由 Task 6 前端填充
    if panel["total"] == 0:
        # 旧数据兼容（spec 3）：历史视频 danmaku 表无数据
        danmaku_tab = '<p class="empty-note">该视频为旧版本分析，无全量弹幕数据，--force 重采后可浏览</p>'
    else:
        top10_html = "、".join(
            f"""<a onclick="filterSender('{esc(t["mid_hash"])}')">{esc(t["name"])}({t["count"]})</a>"""
            for t in panel["top10"])
        danmaku_tab = f'''
        <div class="dm-panel">
            <div class="stat-card"><div class="num">{panel["total"]:,}</div><div class="label">总弹幕数</div></div>
            <div class="stat-card"><div class="num">{panel["merged"]:,}</div><div class="label">合并后行数</div></div>
            <div class="stat-card"><div class="num">{panel["senders"]:,}</div><div class="label">独立发送者</div></div>
            <div class="stat-card"><div class="num">{panel["resolved"]:,}</div><div class="label">已解析发送者</div></div>
        </div>
        <div class="charts-grid">
            <div class="chart-card"><h3>问题弹幕类别分布</h3><canvas id="dmCatChart"></canvas></div>
            <div class="chart-card"><h3>发送者弹幕数 Top10（点击筛选）</h3><div class="top10-list">{top10_html}</div></div>
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
        <div class="dm-table-wrap">
            <table class="dm-table">
                <thead><tr><th><input type="checkbox" id="dmCheckAll" title="全选当前页"></th><th>弹幕内容</th><th>发送者</th><th>视频时间</th><th>发送时间</th><th>类别</th><th>刷屏</th></tr></thead>
                <tbody id="dmTbody"></tbody>
            </table>
            <div class="dm-pager">
                <button id="dmPrev" onclick="dmPage(-1)">上一页</button>
                <span id="dmPageInfo"></span>
                <button id="dmNext" onclick="dmPage(1)">下一页</button>
            </div>
            <div id="dmError" class="dm-error" style="display:none"></div>
        </div>'''

    script = (VIDEO_JS
              .replace("__CHART_JSON__", js_json(chart))
              .replace("__CAT_JSON__", js_json(panel.get("categories", {})))
              .replace("__CAT_COLORS__", js_json(PROBLEM_CATEGORY_COLORS))
              .replace("__UPWC_JSON__", js_json(up_wordcloud_data(profiles)))
              .replace("__BVID__", bvid))

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)} - B站弹幕用户画像分析</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/wordcloud@1.2.2/src/wordcloud2.min.js"></script>
<style>{REPORT_CSS}
{VIDEO_EXTRA_CSS}
{DM_CSS}</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1><a href="/" style="color:white;text-decoration:none">🎬 B站弹幕用户画像分析</a></h1>
        <div class="meta">
            <strong>{esc(title)}</strong><br>
            BV: {esc(bvid)} | 播放: {video_info.get('stat', {}).get('view', 0):,} |
            弹幕: {video_info.get('stat', {}).get('danmaku', 0):,} |
            评论: {video_info.get('stat', {}).get('reply', 0):,}<br>
            分析用户数: {stats['total']} | 大会员: {stats['vip_count']} |
            刷屏用户: {stats['spam_levels'].get('高', 0) + stats['spam_levels'].get('中', 0)} |
            AI画像: {ai_count}{coverage_line}
        </div>
        <div style="margin-top:10px">{links}</div>
    </div>

    <div class="tab-bar">
        <button class="tab-btn active" data-tab="overview" onclick="switchTab('overview')">概览</button>
        <button class="tab-btn" data-tab="users" onclick="switchTab('users')">用户画像</button>
        <button class="tab-btn" data-tab="danmaku" onclick="switchTab('danmaku')">弹幕浏览器</button>
        <button class="tab-btn" data-tab="cringe" onclick="switchTab('cringe')">问题弹幕榜</button>
        <button class="tab-btn" data-tab="full" onclick="switchTab('full')">完整报告</button>
    </div>

    <div id="tab-overview" class="tab-pane active">
        <div class="stats-grid">
            <div class="stat-card"><div class="num">{stats['total']}</div><div class="label">分析用户</div></div>
            <div class="stat-card"><div class="num">{stats['vip_count']}</div><div class="label">大会员</div></div>
            <div class="stat-card"><div class="num">{lv5_count}</div><div class="label">Lv.5+</div></div>
            <div class="stat-card"><div class="num">{stats['spam_levels'].get('高', 0)}</div><div class="label">重度刷屏</div></div>
            <div class="stat-card"><div class="num">{stats['spam_levels'].get('中', 0)}</div><div class="label">中度刷屏</div></div>
            <div class="stat-card"><div class="num">{ai_count}</div><div class="label">AI画像</div></div>
        </div>
        <div class="charts-grid">
            <div class="chart-card"><h3>用户等级分布</h3><canvas id="levelChart"></canvas></div>
            <div class="chart-card"><h3>刷屏风险分布</h3><canvas id="spamChart"></canvas></div>
            <div class="chart-card"><h3>用户标签 Top10</h3><canvas id="tagChart"></canvas></div>
            {region_canvas}
        </div>
    </div>

    <div id="tab-users" class="tab-pane">
        <div class="filter-bar">
            <button class="filter-btn active" onclick="filter('all', this)">全部</button>
            <button class="filter-btn" onclick="filter('high-level', this)">Lv.5+</button>
            <button class="filter-btn" onclick="filter('vip', this)">大会员</button>
            <button class="filter-btn" onclick="filter('official', this)">认证用户</button>
            <button class="filter-btn" onclick="filter('spam', this)">刷屏用户</button>
            <button class="filter-btn" onclick="filter('creator', this)">UP主</button>
            <input id="userSearch" class="search-input" placeholder="搜索昵称/UID..." oninput="searchUsers()">
        </div>
        <div class="user-grid" id="userGrid">{cards_html}</div>
    </div>

    <div id="tab-danmaku" class="tab-pane">{danmaku_tab}</div>

    <div id="tab-cringe" class="tab-pane">{board_html}</div>

    <div id="tab-full" class="tab-pane">
        <div class="stats-grid">
            <div class="stat-card"><div class="num">{stats['total']}</div><div class="label">分析用户</div></div>
            <div class="stat-card"><div class="num">{stats['vip_count']}</div><div class="label">大会员</div></div>
            <div class="stat-card"><div class="num">{lv5_count}</div><div class="label">Lv.5+</div></div>
            <div class="stat-card"><div class="num">{stats['spam_levels'].get('高', 0)}</div><div class="label">重度刷屏</div></div>
            <div class="stat-card"><div class="num">{stats['spam_levels'].get('中', 0)}</div><div class="label">中度刷屏</div></div>
            <div class="stat-card"><div class="num">{ai_count}</div><div class="label">AI画像</div></div>
        </div>
        <div class="charts-grid">
            <div class="chart-card"><h3>用户等级分布</h3><canvas id="levelChart2"></canvas></div>
            <div class="chart-card"><h3>刷屏风险分布</h3><canvas id="spamChart2"></canvas></div>
            <div class="chart-card"><h3>用户标签 Top10</h3><canvas id="tagChart2"></canvas></div>
            {region_canvas2}
        </div>
        <div class="filter-bar">
            <button class="filter-btn active" onclick="filter('all', this, 'full')">全部</button>
            <button class="filter-btn" onclick="filter('high-level', this, 'full')">Lv.5+</button>
            <button class="filter-btn" onclick="filter('vip', this, 'full')">大会员</button>
            <button class="filter-btn" onclick="filter('official', this, 'full')">认证用户</button>
            <button class="filter-btn" onclick="filter('spam', this, 'full')">刷屏用户</button>
            <button class="filter-btn" onclick="filter('creator', this, 'full')">UP主</button>
            <input id="fullSearch" class="search-input" placeholder="搜索昵称/UID..." oninput="searchUsers('full')">
        </div>
        <div class="user-grid" id="fullUserGrid">{full_cards_html}</div>
        {board_html}
    </div>

    <div id="wc-popup" class="wc-popup"><canvas id="wc-popup-canvas" width="276" height="216"></canvas></div>
</div>
<script>{script}</script>
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
    except CookieInvalidError:
        return jsonify({"error": "Cookie 失效，请先运行 python login.py"}), 503
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"total": len(mid_hashes), "done": 0, "current": "",
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
        return jsonify(dict(job))


@app.route("/api/video/<bvid>/danmaku")
def api_danmaku(bvid: str):
    """弹幕 JSON API（spec 4）。

    合并规则：同一 mid_hash 相同 content 合并为一行带 dup_count（GROUP BY mid_hash, content）；
    不同 mid_hash 的相同内容不合并。
    参数：search（内容 LIKE）、sender（mid_hash 或昵称/UID 精确）、category（7类之一，
    命中该发送者的问题弹幕类别）、spam（高/中/低/未分析）、analyzed=1（只看已解析用户）、
    sort（video_time/send_time/dup_count/sender_count）、order（asc/desc）、page（page_size 固定 100）。
    返回 {rows: [...], total: int, page: int}；每行 content/dup_count/mid_hash/uid/name/
    first_video_time/first_send_time/categories/spam_level。
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
               s.uid AS uid, u.name AS name, s.spam_level AS spam_level,
               sc.cnt AS sender_count
        {base_sql}
        ORDER BY {sort_col} {order}, d.mid_hash, d.content
        LIMIT {PAGE_SIZE} OFFSET {(page - 1) * PAGE_SIZE}
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
        })
    return jsonify({"rows": rows, "total": total, "page": page})


@app.route("/download/<path:filename>")
def download(filename: str):
    """CSV/JSON 导出文件下载（仅允许 report_ 前缀文件，防目录外文件被下载）"""
    if not filename.startswith("report_") or "/" in filename or ".." in filename:
        abort(404)
    return send_from_directory(REPORT_DIR, filename, as_attachment=True)


@app.errorhandler(404)
def not_found(e):
    """未知 bvid / 未知路径 → 中文 404 页面（spec 7）"""
    return ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='UTF-8'><title>404</title></head>"
            "<body style='font-family:sans-serif;text-align:center;padding:80px'>"
            "<h1>404</h1><p>视频不存在或尚未分析，请先运行 python run.py &lt;BV号&gt;</p>"
            "<p><a href='/'>返回首页</a></p></body></html>"), 404


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PROFILER_PORT", "8000"))
    print(f"[Web] 交互式报告服务已启动: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
