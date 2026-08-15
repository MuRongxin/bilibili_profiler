# 手动弹幕分析 + 完整报告页 + 自动启动 Web 实现计划

> **REQUIRED SUB-SKILL: superpowers:subagent-driven-development**
>
> - **Goal**：三个特性——A) web.py 报告页新增第五个标签页"完整报告"（末位，恢复原静态 HTML 报告结构纵向一屏到底：统计卡片→图表→筛选+搜索→全部用户卡片→问题弹幕榜）；B) 弹幕浏览器行首勾选 mid_hash → 后台 job 强制分析（UID 解析→强制采集→规则画像→LLM 深掘→落库）+ 前端 2 秒轮询进度；C) run.py/quick_test.py 分析完毕自动起 web.py 并用浏览器打开报告页（`src/web_autostart.py` 的 `maybe_launch_web(bvid)`，`WEB_AUTOSTART=True` 可关）。
> - **Architecture**：全部改动落在 `web.py`（两个新 API + 前端 JS/CSS/标签页）、新增 `src/web_autostart.py`（单一函数，run.py 与 quick_test.py 共用）、`src/main.py`/`quick_test.py` 末尾接线、`config.example.py`+`src/config.py` 加 `WEB_AUTOSTART`。`report.py`/`storage.py`/`uid_resolver.py` 等**零改动**，全部复用现有函数签名。job 状态存内存 dict（重启失效，spec 已接受）。
> - **Tech stack**：Python 3 + Flask（现有）+ SQLite（现有 profiler.db，job 线程只走 storage 的 save_* 短事务）+ 原生 JS（无框架）+ Chart.js CDN（现有）。
> - **Spec**：`docs/superpowers/specs/2026-08-15-manual-danmaku-analysis-design.md`（全部需求以它为准；其第 5 节错误处理、第 6 节验证、第 7 节 YAGNI 均已落到对应任务）。

## 项目约定（所有任务必须遵守）

- 运行一律用 `PYTHONPATH=src .venv/bin/python ...`（`run.py`/`quick_test.py`/`web.py` 自身已处理 sys.path，可直接 `.venv/bin/python xxx.py`）。
- 注释/打印输出全中文；扁平导入 `from config import ...`（不带 `src.` 前缀）。
- `src/config.py` 被 gitignore、含真实 LLM Key，**绝不提交**（本计划 Task 3 要改它但只提交 `config.example.py`）；`data/cookie.json`、`data/profiler.db`、`data/reports/` 同理。
- 失败降级不中断：单发送者分析失败记 job errors 继续；自动启动 web/浏览器失败只打印 URL。
- 无测试框架：验证靠离线脚本（heredoc）+ Flask test_client/后台起服务 curl + 显式 PID 杀服务 + 实跑。
- **临时文件不放 /tmp**（磁盘紧张）：验证日志/假页面快照放 `data/`（已 gitignore 或验证后删除），用完即清。
- 杀后台 web 服务一律用显式 PID（`WPID=$!` 或 `ss -tlnp` 反查 pid），**不用 `kill %1`**（子 shell 场景不可靠），杀完用 `ss -tlnp | grep <端口>` 复核为空。

## 现状关键事实（实现前已核对，计划代码与之一一对应）

- `web.py`（727 行）：`VIDEO_JS`/`VIDEO_EXTRA_CSS`/`DM_CSS` 是**普通字符串常量**，其中 JS/CSS 的大括号无需转义；`video_page` 的返回值与 `danmaku_tab` 是 **f-string**，往里加含 `{`/`}` 的字面量必须 `{{`/`}}` 转义（本计划新增 HTML 不含大括号，无需转义，但执行者改动时须牢记）。VIDEO_JS 走 `.replace("__CHART_JSON__"/"__CAT_JSON__"/"__CAT_COLORS__"/"__UPWC_JSON__"/"__BVID__", ...)` 占位符链。
- 弹幕 API `/api/video/<bvid>/danmaku` 每行返回：`content/dup_count/mid_hash/uid/name/first_video_time/first_send_time/categories/spam_level/sender_count`；同一 mid_hash 不同 content 是多行（GROUP BY mid_hash, content），勾选去重在 JS 侧用 Set。
- `report.py`：`generate_user_card` 卡片最外层带锚点 `id="uid-{uid}"`，碰撞徽标读 `profile['collision_risk']`；`generate_summary_stats`/`generate_chart_data`/`generate_cringe_board`（无 DOM id，可安全渲染两份）/`sort_profiles_by_risk`/`up_wordcloud_data`/`esc`/`js_json`/`PROBLEM_CATEGORY_COLORS`/`REPORT_CSS` 均可直接复用。
- **坑：两个标签页各渲染一份卡片 → `id="uid-{uid}"` 会 DOM id 冲突**。本计划对完整报告页副本做 `cards_html.replace('id="uid-', 'id="full-uid-')`；`gotoUser`/`getElementById` 仍命中用户画像页的第一份（文档序在前），行为不变。
- `storage.py` 签名：`save_sender(bvid, mid_hash, uid, confidence, method, danmaku_count, contents, spam_level, spam_score)`；`save_user_data(uid, name, level, user_data, profile)`（INSERT OR REPLACE）；`load_senders(bvid) -> list[dict]`（键 mid_hash/uid/confidence/method/danmaku_count/contents_json/spam_level/spam_score）；`load_global_uid_map() -> {mid_hash: {"uid","source","hit_count"}}`；`save_global_uid(mid_hash, uid, source)`；`has_user_data(uid) -> bool`；`load_video_info(bvid) -> dict|None`。所有 save_* 内部都是 `with closing(get_db())` 短事务——job 线程直接用即可，**不要自持长连接**。
- `uid_resolver.resolve_sender(mid_hash, danmaku_contents, comment_uid_map, client, max_search=..., method_map=None)` 返回 6 元组 `(uid, confidence, method, user_info, collision_risk, candidates)`；web 端评论映射传**空 dict 与全局库合并后的 plain_uid_map**（`{h: ent["uid"]}`），`method_map={h: ent["source"]}`。`METHOD_CRC32_CRACK = "CRC32破解"`；多候选碰撞（`method==CRC32破解 and len(candidates)>1`）不沉淀全局库（对齐 `main.phase_resolve` 口径）。缓存路径 collision_risk 从 `method=="CRC32破解"` 推断（对齐 main.py 第 272–287 行）。
- `user_collector.collect_user_data(uid, client) -> dict`，失败返回 `{"uid": uid, "error": ...}`（判 `"error" in data`）。
- `profile_analyzer.analyze_profile(user_data, danmaku_stats, spam_stats)`：`danmaku_stats` 用 `count/contents/video_times`，`spam_stats` 用 `spam_level/spam_score/reason/repeat_rate`；web 端无内存态 spam_results，用 `spam_detector.batch_detect_spam({mid_hash: {"contents","timestamps"}})` 从 danmaku 表现算。
- `llm_analyzer.LLMAnalyzer()`；`analyze_deep(profiles, video_info, top_k=1) -> {uid: text}`，内部走 llm_cache（证据未变零 token）；`config.LLM_API_KEY` 为空时整体跳过。
- `auth.load_cookie() -> dict|None`（含 `_refresh_token` 键，用前 pop）；`auth.verify_cookie(client) -> bool`（一次网络请求）；`BiliAPIClient()` + `update_cookies(dict)` + `client._refresh_token = token`。
- `main.py` 末尾完成提示段（552–557 行）打印"运行 python web.py 查看交互式报告"；`run_batch` 逐视频调 `run_analysis`——**批量模式不能自动开浏览器**（会开 N 个标签页），计划给 `run_analysis` 加 `launch_web: bool = True` 参数，`run_batch` 传 `False`（spec 未覆盖批量，此处为最小合理决策，已在 Task 3 注明）。
- `quick_test.py` 末尾（140–142 行）打印"运行 python web.py..."；`config.example.py` 94 行，末尾是 LLM 配置节，新配置加在其前的独立小节；`src/config.py` 结构相同（79/86 行为画像/LLM 配置节标题）。
- `.gitignore` 未排除 `data/web.log`——Task 3 顺带补一行，防止 web 日志被误提交。

---

## Task 1: web.py "完整报告"标签页（spec A：原报告结构纵向一屏到底）

**文件**：`web.py`

- [ ] Step 1：`VIDEO_EXTRA_CSS` 常量末尾（`.top10-list a {...}` 行之后、`"""` 之前）追加新卡片闪烁高亮样式（Task 2 也复用）：

```python
.flash-highlight { box-shadow:0 0 0 3px #fb7299 !important; transition:box-shadow .3s; }
```

- [ ] Step 2：`video_page` 中 `panel = _danmaku_panel_stats(bvid)` 行之后插入完整报告页的两个准备变量（卡片第二份 DOM 锚点 id 改写，防与"用户画像"页冲突；地域图克隆用独立 canvas id）：

```python
    # 完整报告标签页（spec A）：卡片第二份 DOM 的锚点 id 改写为 full-uid- 防 DOM id 冲突
    full_cards_html = cards_html.replace('id="uid-', 'id="full-uid-')
    region_canvas2 = ('<div class="chart-card"><h3>地域分布 Top10</h3><canvas id="regionChart2"></canvas></div>'
                      if chart["region_labels"] else "")
```

- [ ] Step 3：返回的 f-string 中，tab-bar 的"问题弹幕榜"按钮行之后追加第五个标签按钮（末位，前四个不变）：

```python
        <button class="tab-btn" data-tab="full" onclick="switchTab('full')">完整报告</button>
```

- [ ] Step 4：返回的 f-string 中，`<div id="tab-cringe" class="tab-pane">{board_html}</div>` 行之后、`<div id="wc-popup"...>` 之前插入完整报告页（结构=原静态报告：统计卡片→图表→筛选+搜索→全部卡片→问题弹幕榜；全部为既有类名复用 REPORT_CSS，无新渲染逻辑）：

```python
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
```

注意：此块在 f-string 内但不含大括号字面量，无需转义；`{board_html}` 渲染第二份问题弹幕榜（无 DOM id，安全）。

- [ ] Step 5：`VIDEO_JS` 中 `switchTab` 函数整体替换为（完整报告页图表懒初始化：canvas 在隐藏标签页 new Chart 会得到 0 尺寸，首次切入时再建）：

```javascript
function switchTab(name) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
    if (name === 'full') initFullCharts();
}
```

- [ ] Step 6：`VIDEO_JS` 中"用户画像：筛选按钮 + 昵称/UID 搜索"一段（`let currentFilter = 'all';` 到 `applyUserFilter` 函数结束）整体替换为参数化版本（两页筛选状态独立；旧调用 `filter('all', this)`/`searchUsers()` 缺省 scope='users'，用户画像页 onclick 无需改动）：

```javascript
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
```

- [ ] Step 7：验证（假数据 + 后台服务 + curl 元素存在性检查 + 显式 PID 杀服务复核）：

```bash
.venv/bin/python -m py_compile web.py && echo "OK: 编译通过"
PYTHONPATH=src .venv/bin/python - <<'EOF'
from storage import init_db, save_video_info, save_danmaku, save_sender, save_user_data
init_db()
V = "BV_FAKEFULL1"
save_video_info(V, {"bvid": V, "title": "假视频-完整报告", "aid": 990010, "cid": 990010,
                    "duration": 60, "stat": {"view": 10, "danmaku": 1, "reply": 0}})
save_danmaku(V, [{"mid_hash": "aaaa0001", "content": "前排", "time": 1.0, "timestamp": 1700000000}])
save_sender(V, "aaaa0001", 111, "高", "评论区验证", 1, ["前排"], "高", 9.5)
save_user_data(111, "用户甲", 5, {}, {
    "uid": 111, "name": "用户甲", "level": 5,
    "danmaku": {"count": 1, "contents": ["前排"], "spam_level": "高", "spam_score": 9.5},
    "cringe": {"count": 0, "categories": [], "max_severity": 0, "examples": []}})
print("假数据就绪: BV_FAKEFULL1")
EOF
PROFILER_PORT=8123 .venv/bin/python web.py > data/web_t1.log 2>&1 &
WPID=$!
sleep 2
curl -s http://127.0.0.1:8123/video/BV_FAKEFULL1 > data/t1.html
for pat in 'data-tab="full"' 'id="tab-full"' 'levelChart2' 'spamChart2' 'tagChart2' 'fullUserGrid' 'fullSearch' 'full-uid-111' 'initFullCharts' "this, 'full'"; do
  grep -q "$pat" data/t1.html && echo "OK: $pat" || echo "MISSING: $pat"
done
grep -c 'id="uid-111"' data/t1.html   # 期望 1：用户画像页锚点保持原样未被改写
grep -c 'id="full-uid-111"' data/t1.html  # 期望 1：完整报告页锚点已改写
kill $WPID
sleep 1
ss -tlnp 2>/dev/null | grep 8123 || echo "OK: 8123 端口已释放"
```

预期输出：`OK: 编译通过`；10 个模式全部 `OK:`；两个 grep 计数均为 1；`OK: 8123 端口已释放`。然后清理：

```bash
PYTHONPATH=src .venv/bin/python -c "
from storage import clear_video_cache
clear_video_cache('BV_FAKEFULL1')
print('假数据已清理')"
rm -f data/t1.html data/web_t1.log
```

- [ ] Step 8：提交

```bash
git add web.py
git commit -m "feat: 报告页新增完整报告标签页——原静态报告结构纵向呈现 + 独立筛选/搜索/图表"
```

---

## Task 2: 弹幕勾选 UI + 分析 job 后端（spec B：POST analyze + GET job + 后台线程）

**文件**：`web.py`

- [ ] Step 1：头部导入区替换为（新增 threading/uuid 与分析流水线模块；保持扁平导入）：

```python
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
```

- [ ] Step 2：`PAGE_SIZE = 100` 行之后插入 job 基础设施（内存态 dict + 懒加载客户端；Cookie 失效语义按 spec 3.2/5）：

```python
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
```

- [ ] Step 3：`@app.route("/api/video/<bvid>/danmaku")` 路由之前插入两个新路由：

```python
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
```

- [ ] Step 4：弹幕表格加勾选列。`danmaku_tab` f-string 内 `<thead>` 行替换为（行首全选当前页框；这行在 f-string 内但无大括号，无需转义）：

```python
                <thead><tr><th><input type="checkbox" id="dmCheckAll" title="全选当前页"></th><th>弹幕内容</th><th>发送者</th><th>视频时间</th><th>发送时间</th><th>类别</th><th>刷屏</th></tr></thead>
```

同一块中 `<div class="dm-controls">` 内末尾（`dmOrder` select 之后）追加按钮与进度文本：

```python
            <button id="dmAnalyzeBtn" class="filter-btn" onclick="startAnalysis()">分析选中发送者</button>
            <span id="dmAnalyzeStatus"></span>
```

- [ ] Step 5：`VIDEO_JS` 中 `loadDanmaku` 的 `tbody.innerHTML = data.rows.map(row => {...})` 一段里，行模板改为含勾选框的 7 列。具体：把

```javascript
                return '<tr><td>' + escHtml(row.content) + dup + '</td><td>' + sender + '</td><td>' +
                    fmtVideoTime(row.first_video_time) + '</td><td>' +
                    new Date(row.first_send_time * 1000).toLocaleString() + '</td><td>' + cats + '</td><td>' +
                    escHtml(row.spam_level) + '</td></tr>';
            }).join('') || '<tr><td colspan="6" class="empty-note">无匹配弹幕</td></tr>';
```

替换为

```javascript
                const chk = '<input type="checkbox" class="dm-check" data-mid="' + escHtml(row.mid_hash) + '"' +
                    (dmSelected.has(row.mid_hash) ? ' checked' : '') + '>';
                return '<tr><td>' + chk + '</td><td>' + escHtml(row.content) + dup + '</td><td>' + sender + '</td><td>' +
                    fmtVideoTime(row.first_video_time) + '</td><td>' +
                    new Date(row.first_send_time * 1000).toLocaleString() + '</td><td>' + cats + '</td><td>' +
                    escHtml(row.spam_level) + '</td></tr>';
            }).join('') || '<tr><td colspan="7" class="empty-note">无匹配弹幕</td></tr>';
            dmBindChecks();
```

- [ ] Step 6：`VIDEO_JS` 末尾（`if (document.getElementById('dmTbody')) {...}` 块之后、收尾 `"""` 之前）追加勾选状态管理与分析轮询 JS：

```javascript
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
```

- [ ] Step 7：`VIDEO_JS` 中事件绑定块（`if (document.getElementById('dmTbody')) {` 内，`loadDanmaku();` 之前）追加全选框绑定：

```javascript
    document.getElementById('dmCheckAll').addEventListener('change', function() {
        document.querySelectorAll('.dm-check').forEach(c => {
            c.checked = this.checked;
            if (this.checked) dmSelected.add(c.dataset.mid); else dmSelected.delete(c.dataset.mid);
        });
        dmUpdateAnalyzeStatus();
    });
```

- [ ] Step 8：验证（只验证 404/400 分支与静态元素，**不触发真实 job**——真实 Cookie 存在时 POST 会真打 B 站 API；全流程 mock 验证在 Task 5）：

```bash
.venv/bin/python -m py_compile web.py && echo "OK: 编译通过"
PYTHONPATH=src .venv/bin/python - <<'EOF'
from storage import init_db, save_video_info, save_danmaku
init_db()
V = "BV_FAKEJOB1"
save_video_info(V, {"bvid": V, "title": "假视频-job", "aid": 990011, "cid": 990011,
                    "duration": 60, "stat": {"view": 10, "danmaku": 2, "reply": 0}})
save_danmaku(V, [
    {"mid_hash": "aaaa0001", "content": "前排", "time": 1.0, "timestamp": 1700000000},
    {"mid_hash": "bbbb0002", "content": "打卡", "time": 2.0, "timestamp": 1700000100},
])
print("假数据就绪: BV_FAKEJOB1")
EOF
PROFILER_PORT=8123 .venv/bin/python web.py > data/web_t2.log 2>&1 &
WPID=$!
sleep 2
curl -s http://127.0.0.1:8123/video/BV_FAKEJOB1 > data/t2.html
for pat in 'dmCheckAll' 'dmAnalyzeBtn' 'dmAnalyzeStatus' 'startAnalysis' 'pollJob' 'dmSelected' 'dmBindChecks' '全选当前页' '分析选中发送者'; do
  grep -q "$pat" data/t2.html && echo "OK: $pat" || echo "MISSING: $pat"
done
curl -s -o /dev/null -w "analyze未知视频: %{http_code}\n" -X POST -H 'Content-Type: application/json' -d '{"mid_hashes":["aaaa0001"]}' http://127.0.0.1:8123/api/video/BV_NOTEXIST/analyze
curl -s -w "\nanalyze空列表: %{http_code}\n" -X POST -H 'Content-Type: application/json' -d '{"mid_hashes":[]}' http://127.0.0.1:8123/api/video/BV_FAKEJOB1/analyze
curl -s -w "\njob未知: %{http_code}\n" http://127.0.0.1:8123/api/job/nonexistent
kill $WPID
sleep 1
ss -tlnp 2>/dev/null | grep 8123 || echo "OK: 8123 端口已释放"
```

预期输出：9 个模式全部 `OK:`；`analyze未知视频: 404`；`analyze空列表: 400`（JSON `{"error": "mid_hashes 为空"}`）；`job未知: 404`；端口释放。清理：

```bash
PYTHONPATH=src .venv/bin/python -c "
from storage import clear_video_cache
clear_video_cache('BV_FAKEJOB1')
print('假数据已清理')"
rm -f data/t2.html data/web_t2.log
```

- [ ] Step 9：提交

```bash
git add web.py
git commit -m "feat: 弹幕勾选手动分析——勾选UI + analyze/job API + 后台线程强制采集画像深掘"
```

---

## Task 3: src/web_autostart.py + main.py/quick_test.py 接线 + WEB_AUTOSTART 配置（spec C）

**文件**：`src/web_autostart.py`（新建）、`src/main.py`、`quick_test.py`、`config.example.py`、`src/config.py`（gitignored，不提交）、`.gitignore`

- [ ] Step 1：新建 `src/web_autostart.py`，完整内容：

```python
"""
分析完毕自动启动 Web 报告（run.py 与 quick_test.py 共用入口）

spec C：分析结束后探测本地服务→没有则分离启动 web.py→浏览器打开报告页；
任何一步失败都只打印 URL 提示手动访问，绝不影响主流程退出码。
"""
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser

from config import WEB_AUTOSTART, DATA_DIR


def maybe_launch_web(bvid: str):
    """分析结束后自动起 web.py 并用浏览器打开报告页。

    WEB_AUTOSTART=False 时只打印手动提示；已有服务在跑则跳过启动直接开页。
    """
    port = int(os.environ.get("PROFILER_PORT", "8000"))
    url = f"http://127.0.0.1:{port}/video/{bvid}"
    if not WEB_AUTOSTART:
        print(f"  运行 python web.py 后访问报告页: {url}")
        return

    # 1 秒超时探测：无服务时快速失败，不阻塞主流程收尾
    def _alive() -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1):
                return True
        except Exception:
            return False

    try:
        if not _alive():
            # 分离启动：start_new_session 脱离本进程生命周期，日志重定向到 data/web.log
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            log_path = os.path.join(DATA_DIR, "web.log")
            log_f = open(log_path, "a", encoding="utf-8")
            subprocess.Popen([sys.executable, os.path.join(root, "web.py")],
                             cwd=root, stdout=log_f, stderr=subprocess.STDOUT,
                             start_new_session=True)
            # 等服务就绪（最多约 5 秒）
            for _ in range(10):
                if _alive():
                    break
                time.sleep(0.5)
            if not _alive():
                print(f"  [Web] web.py 启动超时，日志见 {log_path}")
        if _alive():
            # webbrowser.open 在无图形环境返回 False/抛异常，降级打印 URL
            if webbrowser.open(url):
                print(f"  报告页已在浏览器打开: {url}")
            else:
                print(f"  浏览器打开失败，请手动访问: {url}")
        else:
            print(f"  请手动运行 python web.py 后访问: {url}")
    except Exception as e:
        # 自动启动是锦上添花：任何异常都不影响分析结果与退出码
        print(f"  [Web] 自动启动失败（{e}），请手动运行 python web.py 后访问: {url}")
```

- [ ] Step 2：`config.example.py` 的 `# ========== LLM 配置 ==========` 一节之前插入新小节；**同样内容也加到 `src/config.py` 同一位置**（该文件 gitignored，不提交）：

```python
# ========== Web 报告配置 ==========
WEB_AUTOSTART = True   # run.py/quick_test.py 分析完毕自动启动 web.py 并用浏览器打开报告页（False 关闭）
```

- [ ] Step 3：`.gitignore` 的 `# 报告输出` 一节 `data/reports/` 行之后追加一行（自动启动的 web 日志不入库）：

```
data/web.log
```

- [ ] Step 4：`src/main.py` 改动三处：
  - 导入区 `from exporter import ...` 附近（其他 storage 导入之后）加一行：`from web_autostart import maybe_launch_web`
  - `run_analysis` 签名改为 `def run_analysis(bvid: str, force: bool = False, max_users: int | None = None, launch_web: bool = True):`，docstring 补一句：`launch_web=False 供批量模式使用（避免逐视频开浏览器标签页）`。
  - 末尾完成提示段（现 552–557 行）替换为：

```python
    print("\n" + "=" * 60)
    print("  分析完成!")
    print(f"  视频: {video_info.get('title', '')}")
    print(f"  分析用户: {len(profiles)} 人")
    print("=" * 60)

    # 分析完毕自动启动 web.py 并打开报告页（WEB_AUTOSTART 可关；失败只打印 URL 降级）
    if launch_web:
        maybe_launch_web(bvid)
    else:
        print("  运行 python web.py 查看交互式报告")
```

  - `run_batch` 中 `run_analysis(bvid, force=force, max_users=max_users)` 改为 `run_analysis(bvid, force=force, max_users=max_users, launch_web=False)`（spec 未覆盖批量场景：批量逐视频开浏览器无意义，保持手动提示）。

- [ ] Step 5：`quick_test.py` 改动两处：
  - 导入区 `from main import _merge_history_danmaku` 行之后加：`from web_autostart import maybe_launch_web`
  - 末尾两行打印替换为：

```python
    print(f"\n✅ 分析完成: {len(profiles)} 人生成画像")
    # 自动启动 web.py 并打开报告页（冒烟场景不阻塞结束：失败只打印 URL 降级）
    maybe_launch_web(bvid)
```

- [ ] Step 6：验证（独立端口实起 web.py 探测链路；无图形环境下 webbrowser.open 走降级分支属预期）：

```bash
.venv/bin/python -m py_compile src/web_autostart.py src/main.py quick_test.py && echo "OK: 编译通过"
grep -n "WEB_AUTOSTART" config.example.py src/config.py   # 两处都应有
PROFILER_PORT=8123 PYTHONPATH=src .venv/bin/python -c "
from web_autostart import maybe_launch_web
maybe_launch_web('BV_NOTEXIST')"
ss -tlnp 2>/dev/null | grep 8123   # 应看到 python 在监听 8123（自动启动生效）
PID=$(ss -tlnp 2>/dev/null | grep 8123 | grep -oP 'pid=\K[0-9]+' | head -1)
[ -n "$PID" ] && kill "$PID"
sleep 1
ss -tlnp 2>/dev/null | grep 8123 || echo "OK: 8123 端口已释放"
cat data/web.log | tail -3   # 应含 [Web] 交互式报告服务已启动
```

预期输出：`OK: 编译通过`；两处 WEB_AUTOSTART grep 命中；`maybe_launch_web` 打印"报告页已在浏览器打开"或"浏览器打开失败，请手动访问: http://127.0.0.1:8123/video/BV_NOTEXIST"（无图形环境为后者，属预期降级）；ss 能查到监听再被释放。

- [ ] Step 7：提交（注意 `src/config.py` 绝不 add）

```bash
git add src/web_autostart.py src/main.py quick_test.py config.example.py .gitignore
git commit -m "feat: 分析完毕自动启动 web.py 并打开报告页（WEB_AUTOSTART 可关，批量模式不开）"
```

---

## Task 4: AGENTS.md/README 文档同步 + spec/plan 提交

**文件**：`AGENTS.md`、`README.md`、`docs/superpowers/`

- [ ] Step 1：`AGENTS.md` 更新（最小改动）：
  - 项目概述段："四标签页：概览/用户画像/弹幕浏览器/问题弹幕榜"改为"五标签页：概览/用户画像/弹幕浏览器/问题弹幕榜/完整报告（原静态报告结构纵向呈现）；弹幕浏览器支持勾选 mid_hash 手动触发强制分析（后台 job：UID解析+采集+画像+LLM深掘）；run.py/quick_test.py 分析完毕自动启动 web.py 并打开报告页（WEB_AUTOSTART 可关）"。
  - 代码结构树中 `web.py` 行注释改为 `# 交互式 Web 报告服务（Flask，首页 + 五标签页报告 + 弹幕 JSON API + 手动分析 job API）`；`src/` 树内 `main.py` 之后插一行 `├── web_autostart.py    # 分析完毕自动启动 web.py 并打开报告页（maybe_launch_web，WEB_AUTOSTART 开关）`。
  - 运行与安装代码块 `python web.py` 行之后补一行注释说明：`# run.py/quick_test.py 分析完毕会自动启动并打开报告页（config.py 中 WEB_AUTOSTART=False 关闭）`——放在代码块外紧跟一句说明即可，保持代码块可执行。

- [ ] Step 2：`README.md` 更新：
  - 特性列表加一条：`- **手动弹幕分析**：Web 弹幕浏览器勾选发送者 → 后台强制分析（UID解析+采集+画像+LLM深掘），进度轮询，完成自动跳转完整报告`。
  - "交互式Web报告"一条补："五标签页含完整报告（原静态报告纵向布局）；分析完毕自动启动并打开（WEB_AUTOSTART 可关）"。
  - 代码结构树加 `web_autostart.py # 分析完毕自动启动Web报告` 行。
  - （执行时先 Read README.md 定位实际行，以上为内容要求而非逐行指令。）

- [ ] Step 3：提交（spec/plan 一并入库）：

```bash
git add AGENTS.md README.md \
  docs/superpowers/specs/2026-08-15-manual-danmaku-analysis-design.md \
  docs/superpowers/plans/2026-08-15-manual-danmaku-analysis.md
git commit -m "docs: 手动弹幕分析/完整报告页/自动启动Web 文档同步，提交 spec 与实现计划"
```

---

## Task 5: 离线 mock 全流程验证 + 实跑验证 + quick_test 冒烟（spec 6）

**说明**：离线验证用 Flask test_client（不起真实端口、不打 B 站 API），monkeypatch 掉 `web._client`/`web.resolve_sender`/`web.collect_user_data`/`web.LLMAnalyzer`；假数据验证后全部清理。

- [ ] Step 1：离线全流程断言（job 在测试进程内线程中跑，轮询 test_client 直到 finished）：

```bash
.venv/bin/python - <<'EOF'
import sys, os, time, json
sys.path.insert(0, os.path.abspath("src"))
sys.path.insert(0, os.path.abspath("."))

from storage import init_db, save_video_info, save_danmaku, clear_video_cache
from storage import load_senders, load_user_data, get_db

init_db()
V = "BV_FAKEMAN1"
save_video_info(V, {"bvid": V, "title": "假视频-手动分析", "aid": 990012, "cid": 990012,
                    "duration": 60, "stat": {"view": 10, "danmaku": 2, "reply": 0}})
save_danmaku(V, [
    {"mid_hash": "eeee0005", "content": "这弹幕有问题", "time": 1.0, "timestamp": 1700000000},
    {"mid_hash": "eeee0005", "content": "又一条", "time": 2.0, "timestamp": 1700000100},
])

import web

# --- mock：绕过 Cookie 校验与一切真实网络/LLM 调用 ---
web._client = object()                      # _get_client 直接命中缓存分支
resolve_calls = []
def fake_resolve(mid_hash, contents, plain_map, client, max_search=None, method_map=None):
    resolve_calls.append(mid_hash)
    return 333, "中", "CRC32破解", {}, True, [333]   # 单候选 → 应沉淀全局库
web.resolve_sender = fake_resolve
def fake_collect(uid, client):
    return {"uid": uid, "name": "测试丙", "level": 4, "vip_status": 0,
            "followings": [], "favorite_folders": []}
web.collect_user_data = fake_collect
class FakeAnalyzer:
    def analyze_deep(self, profiles, video_info, top_k=1):
        return {p["uid"]: "AI深度画像文本" for p in profiles}
web.LLMAnalyzer = FakeAnalyzer
web.LLM_API_KEY = "fake-key"                # 开启深掘分支

c = web.app.test_client()

# 完整报告标签页元素存在性（spec 6 离线项；此时 users 无数据卡片为空，仅查结构）
html = c.get(f"/video/{V}").get_data(as_text=True)
for pat in ['data-tab="full"', 'id="tab-full"', 'levelChart2', 'fullUserGrid', 'fullSearch']:
    assert pat in html, f"完整报告页缺少: {pat}"

# API 错误分支
assert c.post("/api/video/BV_UNKNOWN/analyze", json={"mid_hashes": ["x"]}).status_code == 404
assert c.post(f"/api/video/{V}/analyze", json={"mid_hashes": []}).status_code == 400
assert c.get("/api/job/nope").status_code == 404

# 启动 job 并轮询到完成
r = c.post(f"/api/video/{V}/analyze", json={"mid_hashes": ["eeee0005", "eeee0005"]})  # 重复应去重
assert r.status_code == 200, r.get_json()
job_id = r.get_json()["job_id"]
for _ in range(50):
    st = c.get(f"/api/job/{job_id}").get_json()
    if st["finished"]:
        break
    time.sleep(0.2)
assert st["finished"] and st["total"] == 1 and st["done"] == 1, st
assert st["results"] == [333] and st["errors"] == [], st

# 落库断言：senders（真实置信度）+ users（含 ai_deep 与 collision_risk）
s = [r for r in load_senders(V) if r["mid_hash"] == "eeee0005"]
assert s and s[0]["uid"] == 333 and s[0]["confidence"] == "中", s
ud, profile = load_user_data(333)
assert profile["name"] == "测试丙" and profile["ai_deep"] == "AI深度画像文本"
assert profile["collision_risk"] is True
assert profile["danmaku"]["count"] == 2     # danmaku 表现算统计
gm = get_db().execute("SELECT uid, source FROM global_uid_map WHERE mid_hash='eeee0005'").fetchone()
assert gm and gm["uid"] == 333, "单候选破解应沉淀全局库"

# 卡片出现在完整报告页且锚点改写
html = c.get(f"/video/{V}").get_data(as_text=True)
assert 'id="full-uid-333"' in html and 'id="uid-333"' in html

# 重复分析 → 命中"已分析过"跳过路径（resolve/collect 不再调用）
r = c.post(f"/api/video/{V}/analyze", json={"mid_hashes": ["eeee0005"]})
job_id2 = r.get_json()["job_id"]
for _ in range(50):
    st2 = c.get(f"/api/job/{job_id2}").get_json()
    if st2["finished"]:
        break
    time.sleep(0.2)
assert st2["results"] == [333] and len(resolve_calls) == 1, (st2, resolve_calls)

# Cookie 失效分支：模拟 _client_failed → 503
web._client = None
web._client_failed = True
r = c.post(f"/api/video/{V}/analyze", json={"mid_hashes": ["eeee0005"]})
assert r.status_code == 503 and "Cookie 失效" in r.get_json()["error"]

# 清理假数据（含全局库条目，避免污染）
clear_video_cache(V)
conn = get_db()
conn.execute("DELETE FROM global_uid_map WHERE mid_hash='eeee0005'")
conn.execute("DELETE FROM users WHERE uid=333")
conn.commit()
print("OK: 手动分析 job 全流程断言通过（启动/去重/解析/采集/画像/深掘/落库/跳过/503）")
EOF
```

预期输出最后一行：`OK: 手动分析 job 全流程断言通过（启动/去重/解析/采集/画像/深掘/落库/跳过/503）`。

- [ ] Step 2：实跑验证（真实网络 + 有效 Cookie；目标视频此前 0 画像，全量重采耗时较长，放后台跑）：

```bash
.venv/bin/python run.py BV1ebg16jEhp
```

预期：末尾打印"分析完成"后，出现"报告页已在浏览器打开: http://127.0.0.1:8000/video/BV1ebg16jEhp"（无图形环境则为"浏览器打开失败，请手动访问"降级行）；`ss -tlnp | grep 8000` 能看到 python 监听（自动启动的 web.py）。随后**人工浏览器检查**（spec 6）：弹幕浏览器勾选 2–3 个发送者 → 点"分析选中发送者" → 进度文本每 2 秒更新 → 完成后自动跳"完整报告"标签页且新卡片闪烁高亮。检查完用显式 PID 杀服务：

```bash
PID=$(ss -tlnp 2>/dev/null | grep ':8000' | grep -oP 'pid=\K[0-9]+' | head -1)
[ -n "$PID" ] && kill "$PID"
sleep 1
ss -tlnp 2>/dev/null | grep ':8000' || echo "OK: 8000 端口已释放"
```

- [ ] Step 3：quick_test 冒烟（自动启动不阻塞冒烟结束）：

```bash
.venv/bin/python quick_test.py BV1vu4y1b7Y9 --top 1
```

预期：正常跑完，末尾 `✅ 分析完成` 后接浏览器打开/降级打印行，进程随即正常退出（web.py 是分离子进程，不挂住终端）。杀端口同上一条命令。

- [ ] Step 4：最终提交（如 Task 1–3 验证后有修正）：

```bash
git add -A web.py src/ quick_test.py config.example.py .gitignore
git commit -m "fix: 离线断言与实跑验证修正" || echo "无修正，跳过"
```

---

## 风险与注意

- **f-string 大括号**：`VIDEO_JS`/`VIDEO_EXTRA_CSS` 是普通字符串常量，JS/CSS 大括号直接写；`video_page` 返回值与 `danmaku_tab` 是 f-string，若执行者临场加入含 `{`/`}` 的字面量必须 `{{`/`}}` 转义——本计划给定的新增 HTML 均不含大括号，照抄即可。
- **DOM id 冲突**：两份卡片 DOM 靠 `full-uid-` 前缀区分；`gotoUser`（弹幕页点发送者跳用户画像页）与"分析完成高亮"（跳完整报告页）各自命中正确前缀，互不影响。
- **SQLite 并发**：job 线程写库只走 `save_sender`/`save_user_data`/`save_global_uid`（各自 `with closing(get_db())` 短事务），Flask 读侧已有 500 JSON 降级；偶发 database locked 本地工具可接受（与上一版计划同口径）。
- **弹幕合并行 vs 勾选**：同一 mid_hash 的多行（不同 content）各自有勾选框但共享勾选状态（Set 按 mid_hash），翻页/重新加载后 `loadDanmaku` 按 `dmSelected` 回显勾选。
- **job 内存态**：服务重启 job 丢失，前端轮询拿到 404 会提示"数据可能已部分落库"并清除 sessionStorage（spec 5 已接受；不做 job 持久化与历史列表——spec 7）。
- **web 端解析率**：无评论映射，只有全局库 + CRC32 彩虹表，解析率低于 run.py 属 spec 7 明确接受的现实；碰撞/低置信度条目照常采集并在画像页显示"可能误识别"徽标。
- **分析完成靠 `location.reload()` 出新卡片**：新数据必须服务端渲染才有 DOM；待高亮 UID 与完成消息经 sessionStorage 跨重载传递。
- **批量模式不自动开浏览器**：`run_batch` 传 `launch_web=False`，spec 未覆盖此场景，属最小合理决策（避免开 N 个标签页）。
- **LLM 深掘 token**：手动勾选即深掘（用户已接受）；llm_cache 命中时零 token。
- **临时文件**：验证日志/快照一律放 `data/` 并在步骤内 `rm` 清理，不写 /tmp。
