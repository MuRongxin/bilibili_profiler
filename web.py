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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from flask import Flask, abort, jsonify, request, send_from_directory

from config import REPORT_DIR
from storage import get_db, init_db
from report import (REPORT_CSS, esc, js_json, generate_user_card, generate_summary_stats,
                    generate_chart_data, generate_cringe_board, sort_profiles_by_risk,
                    up_wordcloud_data, PROBLEM_CATEGORY_COLORS)

app = Flask(__name__)
PAGE_SIZE = 100  # 弹幕 API 固定每页条数（spec 4）


# ========== 数据加载辅助 ==========

def _load_video_row(bvid: str):
    """videos 表整行；不存在返回 None"""
    conn = get_db()
    row = conn.execute("SELECT * FROM videos WHERE bvid = ?", (bvid,)).fetchone()
    conn.close()
    return row


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
    的 cringe 字段 Python 侧解析（非 SQL）。senders 无行的 mid_hash 不在此表 → 未分析。"""
    conn = get_db()
    rows = conn.execute('''
        SELECT s.mid_hash, s.uid, s.spam_level, u.name, u.profile_json
        FROM senders s LEFT JOIN users u ON u.uid = s.uid
        WHERE s.bvid = ?
    ''', (bvid,)).fetchall()
    conn.close()
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
"""

# 弹幕浏览器样式占位（Task 6 填充）
DM_CSS = ""

VIDEO_JS = """
// 标签页切换
function switchTab(name) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
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

// 用户画像：筛选按钮 + 昵称/UID 搜索（前端过滤，spec 4）
let currentFilter = 'all';
function filter(type, el) {
    currentFilter = type;
    document.querySelectorAll('.filter-bar .filter-btn').forEach(b => b.classList.remove('active'));
    el.classList.add('active');
    applyUserFilter();
}
function searchUsers() { applyUserFilter(); }
function applyUserFilter() {
    const kw = (document.getElementById('userSearch').value || '').trim().toLowerCase();
    document.querySelectorAll('.user-card').forEach(card => {
        const level = parseInt(card.dataset.level) || 0;
        const isVip = card.dataset.vip === 'true';
        const spam = card.dataset.spam;
        const official = card.dataset.official === 'true';
        const isCreator = parseInt(card.querySelector('.stats-bar .stat:nth-child(4) .num')?.textContent || 0) > 0;
        let show = true;
        switch (currentFilter) {
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

// __DM_BROWSER_JS__
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
        <div id="dmBrowser"></div>'''

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

    <div id="wc-popup" class="wc-popup"><canvas id="wc-popup-canvas" width="276" height="216"></canvas></div>
</div>
<script>{script}</script>
</body>
</html>'''


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
    if _load_video_row(bvid) is None:
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

    meta = _sender_meta(bvid)

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
    try:
        conn = get_db()
        total = conn.execute(count_sql, full_params).fetchone()[0]
        raw_rows = conn.execute(rows_sql, full_params).fetchall()
        conn.close()
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
