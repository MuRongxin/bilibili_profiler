# 报告层改造（阶段一）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 改进 Web 报告的用户体验：删除重复标签页、卡片分页排序防抖、弹幕浏览器增强、job 现场恢复与失败重试、URL 状态、首页搜索排序、已有数据展示增强、CSS/JS 拆分为静态文件

**Architecture:** 只动报告展示层（web.py / src/report.py + 新增 static/ 静态资源），不改数据库结构与采集流水线

**Tech Stack:** Python 3 + Flask + vanilla JS + Chart.js/wordcloud2（CDN 维持现状）+ SQLite（只读）

---

## 设计规格

`docs/superpowers/specs/2026-08-16-report-ux-phase1-design.md`（8 章：信息架构 / 用户画像页 / 弹幕浏览器 / 手动分析 job / URL 状态 / 首页与下载 / 数据展示增强 / 工程结构）。

**明确不做（YAGNI）：** 图表库本地化、移动端响应式补强、弹幕 mode/fontsize/color/dmid 入库与相应筛选（阶段二）、引入前端框架或模板引擎、`src/` 采集流水线逻辑改动。

## 项目验证约定（无 pytest/unittest）

本项目没有单元测试框架，本计划的验证方式：

- `src/report.py` 纯函数改动 → 用 `.venv/bin/python -c "..."` 或 heredoc 小脚本断言验证（注意先 `sys.path.insert(0, 'src')`，模块间为扁平导入）；
- `web.py` 路由/前端改动 → 启动本地服务 + `curl` 断言返回内容；
- 数据库已有真实数据（`data/profiler.db`，6 个视频），取一个真实 BV 号用：

```bash
cd /home/lrxin/文档/bilibili_profiler
BVID=$(.venv/bin/python -c "import sqlite3;print(sqlite3.connect('data/profiler.db').execute('SELECT bvid FROM videos LIMIT 1').fetchone()[0])")
```

- 启动/停止 Web 服务的标准片段（各验证步骤按需复制）：

```bash
cd /home/lrxin/文档/bilibili_profiler
.venv/bin/python web.py > /tmp/phase1_web.log 2>&1 & WEB_PID=$!
sleep 2
# ... curl 断言 ...
kill $WEB_PID
```

- **不做任何 git 提交**，任务以验证通过为结束。
- 所有代码注释、页面文案用中文。

## File Structure

**新增：**

- `static/report.css` — 报告页样式：原 `VIDEO_EXTRA_CSS` + `DM_CSS` 全文，追加打印样式（Task 2）、用户画像分页/排序/计数样式（Task 3）、弹幕 spinner/错误条样式（Task 4）、job 失败明细条样式（Task 5）、下载折叠与 404 卡片样式（Task 7）、覆盖率说明条样式（Task 8）
- `static/report.js` — 报告页全部前端逻辑：原 `VIDEO_JS` 全文，顶部占位符改为读取 `window.__DATA__`；后续 Task 2/3/4/5/6 在此文件内改写对应函数
- `static/index.css` — 首页样式：原 `INDEX_EXTRA_CSS` 全文，追加搜索框/可排序列头/分页条样式（Task 7）
- `static/index.js` — 首页搜索 + 列头排序 + 分页逻辑（Task 7 新建）

Flask 默认以 `/static/<path>` 伺服项目根目录 `static/`，无需新增路由。

**修改：**

- `web.py` — 删除 4 个 CSS/JS 字符串常量，数据注入改 `window.__DATA__`；删除"完整报告"标签页；首页表格加列与 data 属性；弹幕 API 加 `page_size` 参数；job errors 结构化；下载区分层；404 页样式
- `src/report.py` — `generate_user_card` 卡片根节点补 `data-is-up`/`data-spam-score`/`data-danmaku-count`/`data-fans`；刷屏行加精确分值与解析徽标；基础信息加采集时间/投稿数/动态获赞；新增直播小节；`REPORT_CSS` 追加 `.method-badge`/`.live-badge`（`REPORT_CSS` 保留在 report.py 内联注入，不拆分）
- `src/exporter.py` — 仅更新模块 docstring 中"文件名与 HTML 报告同前缀"的过时表述（静态 HTML 报告已移除）
- `AGENTS.md` — 代码结构一节补 `static/` 目录说明（Task 9）
- `data/reports/report_*.html` — 删除残留的废弃静态 HTML 报告（Task 7）

**不改：** `src/storage.py`（表结构不变）、`src/main.py` 及全部采集流水线模块、`requirements.txt`。

## 关键命名约定（后续任务必须一致）

- `window.__DATA__` 键：`chart` / `categories` / `categoryColors` / `upWordcloud` / `bvid`；JS 侧统一从 `PAGE_DATA` 读取
- 卡片数据属性：`data-level` `data-vip` `data-spam` `data-official`（已有）+ `data-is-up` `data-spam-score` `data-danmaku-count` `data-fans`（Task 3 新增）；筛选按钮带 `data-filter`
- 用户画像 JS：`userState = {filter, kw, sort, page}`、`USER_PAGE_SIZE = 24`、`userCards[]`、`initUserCards()`、`userFilter(type, btn)`、`renderUserCards()`、`renderUserPager(pages)`、`userPage(p)`、`userMatchFilter(d)`、`userSortCmp(a, b)`、`userCardData(card)`
- 弹幕浏览器 JS：保留 `loadDanmaku/dmPage/dmReload/dmParams/filterSender/gotoUser/dmUpdateAnalyzeStatus/dmBindChecks/startAnalysis/pollJob/escHtml/fmtVideoTime/dmSelected/DM_JOB_KEY`；新增 `dmGotoPage()`、`dmPageSizeChange()`、`dmSyncUrl()`、`dmRestoreFromUrl()`、`renderJobFailures(errors)`、`saveViewState()`、`restoreViewState()`、`restoreTabFromHash()`、`jobPollFails`
- HTML id：`userSort` `userResultCount` `userEmpty` `userPager`（画像页）；`dmGoto` `dmPageSize` `dmSpinner` `dmErrorText` `dmFailBar` `dmFailList` `dmRetryBtn` `dmReloadBtn`（弹幕页）；`idxSearch` `videoTbody` `idxPrev` `idxNext` `idxPageInfo`（首页）
- CSS 类：`result-count` `empty-filter` `user-pager` `pager-btn` `sort-select` `dm-spinner` `dm-error-bar` `dm-retry-btn` `fail-detail` `coverage-note` `dl-history` `nf-card` `method-badge` `live-badge` `idx-pager`
- profile 注入键（仅渲染期存在，不落库）：`resolve_method` / `resolve_confidence` / `collected_at`

---

## Task 1: 工程结构——CSS/JS 拆分为 static/，数据注入改 window.__DATA__

设计规格第 8 章。这是后续所有前端任务的地基：之后任务直接编辑 `static/report.js` / `static/report.css`，不再碰 Python 字符串常量。

**Files:**
- Create: `static/report.css`、`static/report.js`、`static/index.css`
- Modify: `web.py`（删除常量、改注入方式；`REPORT_CSS` 继续从 report.py 导入内联，不动）

- [ ] **Step 1: 创建 `static/index.css`，内容为原 INDEX_EXTRA_CSS 全文**

创建文件（注意首行注释说明来源）：

```css
/* 首页样式（原 web.py INDEX_EXTRA_CSS 平移，Task 7 追加搜索/排序/分页样式） */
.video-table { width:100%; border-collapse:collapse; background:white; border-radius:12px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.04); }
.video-table th, .video-table td { text-align:left; padding:12px 16px; border-bottom:1px solid #f0f0f0; font-size:15px; }
.video-table th { color:#999; font-weight:500; }
.video-table a { color:#00a1d6; text-decoration:none; }
```

- [ ] **Step 2: 创建 `static/report.css`，内容为原 VIDEO_EXTRA_CSS + DM_CSS 全文**

```css
/* 报告页样式（原 web.py VIDEO_EXTRA_CSS + DM_CSS 平移；基础样式 REPORT_CSS 仍由 report.py 内联） */
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

/* 弹幕浏览器样式（原 web.py DM_CSS 平移） */
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
```

- [ ] **Step 3: 创建 `static/report.js`，内容为原 VIDEO_JS 全文，仅替换 4 处占位符注入**

将 `web.py` 中 `VIDEO_JS` 常量（web.py:355-670，首尾三引号不含）全文拷贝到 `static/report.js`，文件最顶部加来源注释，并做且仅做以下 4 处替换：

替换 1，原：

```js
// 概览图表
const chartData = __CHART_JSON__;
```

新：

```js
// 页面数据由服务端内联 <script>window.__DATA__</script> 注入（工程结构改造，替代占位符 .replace）
const PAGE_DATA = window.__DATA__ || {};

// 概览图表
const chartData = PAGE_DATA.chart;
```

替换 2，原：

```js
const dmCatData = __CAT_JSON__;
const dmCatColors = __CAT_COLORS__;
```

新：

```js
const dmCatData = PAGE_DATA.categories;
const dmCatColors = PAGE_DATA.categoryColors;
```

替换 3，原：

```js
const upWcData = __UPWC_JSON__;
```

新：

```js
const upWcData = PAGE_DATA.upWordcloud;
```

替换 4，原：

```js
const BVID = "__BVID__";
```

新：

```js
const BVID = PAGE_DATA.bvid;
```

文件首行加注释：

```js
// 报告页前端逻辑（原 web.py VIDEO_JS 平移；数据经 window.__DATA__ 注入，本文件须在其后加载）
```

- [ ] **Step 4: web.py 删除 4 个常量与 script 装配，改 link/inline-data 注入**

删除 `web.py` 中从 `# ========== 页面模板 CSS/JS（字面量全放常量，避免 f-string 大括号转义） ==========` 注释行起，到 `VIDEO_JS` 结束三引号为止的全部内容（web.py:316-670，即 `INDEX_EXTRA_CSS`、`VIDEO_EXTRA_CSS`、`DM_CSS`、`VIDEO_JS` 四个常量）。

`index()` 的 head，原：

```python
<title>B站弹幕用户画像分析 - 视频列表</title>
<style>{REPORT_CSS}
{INDEX_EXTRA_CSS}</style>
```

新：

```python
<title>B站弹幕用户画像分析 - 视频列表</title>
<style>{REPORT_CSS}</style>
<link rel="stylesheet" href="/static/index.css">
```

`video_page()` 中删除 script 装配块，原：

```python
    script = (VIDEO_JS
              .replace("__CHART_JSON__", js_json(chart))
              .replace("__CAT_JSON__", js_json(panel.get("categories", {})))
              .replace("__CAT_COLORS__", js_json(PROBLEM_CATEGORY_COLORS))
              .replace("__UPWC_JSON__", js_json(up_wordcloud_data(profiles)))
              .replace("__BVID__", bvid))
```

新：

```python
    # 数据经内联 <script>window.__DATA__</script> 注入，静态 /static/report.js 读取（spec 8）
    page_data = {
        "chart": chart,
        "categories": panel.get("categories", {}),
        "categoryColors": PROBLEM_CATEGORY_COLORS,
        "upWordcloud": up_wordcloud_data(profiles),
        "bvid": bvid,
    }
```

`video_page()` 的 head，原：

```python
<style>{REPORT_CSS}
{VIDEO_EXTRA_CSS}
{DM_CSS}</style>
```

新：

```python
<style>{REPORT_CSS}</style>
<link rel="stylesheet" href="/static/report.css">
```

`video_page()` 页面尾部，原：

```python
<script>{script}</script>
```

新：

```python
<script>window.__DATA__ = {js_json(page_data)};</script>
<script src="/static/report.js"></script>
```

- [ ] **Step 5: 验证**

```bash
cd /home/lrxin/文档/bilibili_profiler
.venv/bin/python -c "import ast; ast.parse(open('web.py').read()); print('web.py 语法 OK')"
grep -c "INDEX_EXTRA_CSS\|VIDEO_EXTRA_CSS\|DM_CSS\|VIDEO_JS" web.py   # 预期 0
node --version 2>/dev/null && node --check static/report.js && echo "report.js 语法 OK" || echo "无 node，跳过 JS 语法检查（Task 9 浏览器验证兜底）"
BVID=$(.venv/bin/python -c "import sqlite3;print(sqlite3.connect('data/profiler.db').execute('SELECT bvid FROM videos LIMIT 1').fetchone()[0])")
.venv/bin/python web.py > /tmp/phase1_web.log 2>&1 & WEB_PID=$!
sleep 2
curl -s "http://127.0.0.1:8000/video/$BVID" | grep -c '/static/report.js'    # 预期 1
curl -s "http://127.0.0.1:8000/video/$BVID" | grep -c 'window.__DATA__'      # 预期 1
curl -s "http://127.0.0.1:8000/video/$BVID" | grep -c '__CHART_JSON__'       # 预期 0
curl -s "http://127.0.0.1:8000/static/report.js" | grep -c 'PAGE_DATA'       # 预期 >=4
curl -s "http://127.0.0.1:8000/static/report.css" | grep -c '.tab-bar'       # 预期 >=1
curl -s "http://127.0.0.1:8000/" | grep -c '/static/index.css'               # 预期 1
kill $WEB_PID
```

预期：web.py 语法 OK；常量引用为 0；报告页含 `window.__DATA__` 与 `/static/report.js` 引用；静态文件可正常伺服。若有 node，`node --check` 通过。

---

## Task 2: 信息架构——删除"完整报告"标签页，补 @media print 样式

设计规格第 1 章。依赖 Task 1（以下 JS 改动都在 `static/report.js`）。

**Files:**
- Modify: `web.py`（删 full 标签按钮、pane、克隆 DOM 装配）
- Modify: `static/report.js`（删 `initFullCharts` 与 `switchTab` 中的 full 分支；完成重载的恢复跳转改指"用户画像"页）
- Modify: `static/report.css`（追加打印样式）

- [ ] **Step 1: web.py 删除完整报告的 HTML 与装配代码**

删除标签按钮（web.py:849 原样一行）：

```python
        <button class="tab-btn" data-tab="full" onclick="switchTab('full')">完整报告</button>
```

删除克隆 DOM 装配（web.py:738-741 原样）：

```python
    # 完整报告标签页（spec A）：卡片第二份 DOM 的锚点 id 改写为 full-uid- 防 DOM id 冲突
    full_cards_html = cards_html.replace('id="uid-', 'id="full-uid-')
    region_canvas2 = ('<div class="chart-card"><h3>地域分布 Top10</h3><canvas id="regionChart2"></canvas></div>'
                      if chart["region_labels"] else "")
```

删除整个 `tab-full` pane（web.py:886-912 原样整块，从 `<div id="tab-full" class="tab-pane">` 到 `{board_html}\n    </div>` 结束）：

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

- [ ] **Step 2: static/report.js 删除完整报告的 JS 逻辑**

`switchTab` 中删除一行，原：

```js
function switchTab(name) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
    if (name === 'full') initFullCharts();
}
```

新：

```js
function switchTab(name) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
}
```

删除完整报告克隆图表懒初始化块（原样整段删除）：

```js
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

旧筛选代码（`userFilterState`/`USER_SCOPE`/`filter`/`searchUsers`/`applyUserFilter`）本任务保留不动——full 分支已随 DOM 删除而成为死代码，Task 3 整体重写时一并删除。

- [ ] **Step 3: static/report.js 修复"分析完成重载"恢复跳转（原指向已删除的 full 页）**

页面加载恢复 IIFE 中原：

```js
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
```

新：

```js
        switchTab('users');
        dmUpdateAnalyzeStatus(flashMsg);
        uids.forEach(uid => {
            const el = document.getElementById('uid-' + uid);
            if (el) {
                el.classList.add('flash-highlight');
                setTimeout(() => el.classList.remove('flash-highlight'), 3000);
            }
        });
        const first = document.getElementById('uid-' + uids[0]);
        if (first) first.scrollIntoView({behavior: 'smooth', block: 'center'});
        return;
```

- [ ] **Step 4: static/report.css 追加打印样式**

文件末尾追加：

```css
/* 打印样式（spec 1）：展开全部标签页与用户卡片、隐藏交互控件，替代原"完整报告"汇总查看场景 */
@media print {
    .tab-bar, .filter-bar, .user-pager, .dm-controls, .dm-pager, .dm-error-bar, .fail-detail, .wc-popup { display:none !important; }
    .tab-pane { display:block !important; page-break-before:always; }
    .tab-pane:first-of-type { page-break-before:avoid; }
    .user-card { display:block !important; break-inside:avoid; box-shadow:none; transform:none; }
    body { background:white; }
}
```

（`.user-pager`/`.dm-error-bar`/`.fail-detail` 类在 Task 3/4/5 才出现在 HTML 中，此处提前列入无副作用。）

- [ ] **Step 5: 验证**

```bash
cd /home/lrxin/文档/bilibili_profiler
.venv/bin/python -c "import ast; ast.parse(open('web.py').read()); print('web.py 语法 OK')"
BVID=$(.venv/bin/python -c "import sqlite3;print(sqlite3.connect('data/profiler.db').execute('SELECT bvid FROM videos LIMIT 1').fetchone()[0])")
.venv/bin/python web.py > /tmp/phase1_web.log 2>&1 & WEB_PID=$!
sleep 2
PAGE=$(curl -s "http://127.0.0.1:8000/video/$BVID")
echo "$PAGE" | grep -c 'data-tab="full"'      # 预期 0
echo "$PAGE" | grep -c 'id="tab-full"'        # 预期 0
echo "$PAGE" | grep -c 'full-uid-'            # 预期 0
echo "$PAGE" | grep -c 'levelChart2'          # 预期 0
echo "$PAGE" | grep -c 'data-tab="users"'     # 预期 1
JS=$(curl -s "http://127.0.0.1:8000/static/report.js")
echo "$JS" | grep -c 'initFullCharts'         # 预期 0
echo "$JS" | grep -c 'full-uid-'              # 预期 0
curl -s "http://127.0.0.1:8000/static/report.css" | grep -c '@media print'   # 预期 1
kill $WEB_PID
```

预期：页面只剩四个标签页（概览/用户画像/弹幕浏览器/问题弹幕榜），无任何 full 残留；report.css 含打印样式。

---

## Task 3: 用户画像页——卡片 data 属性、筛选重写、搜索防抖、分页、排序

设计规格第 2 章。依赖 Task 1/2。

**Files:**
- Modify: `src/report.py`（`generate_user_card` 根节点 data 属性）
- Modify: `web.py`（`tab-users` pane HTML：按钮加 `data-filter`、新增排序下拉/计数/空态/分页容器）
- Modify: `static/report.js`（删除旧筛选代码，新增 `userState` 体系，重写 `gotoUser`）
- Modify: `static/report.css`（追加分页/计数/空态/排序样式）

- [ ] **Step 1: src/report.py 的 generate_user_card 根节点补 4 个 data 属性**

原（report.py:317）：

```python
    return f'''
    <div class="user-card" id="uid-{esc(uid)}" data-level="{esc(level)}" data-vip="{profile.get('vip_status',0)==1}" data-spam="{esc(spam_level)}" data-official="{profile.get('official_type',-1)>=0}">
```

新：

```python
    return f'''
    <div class="user-card" id="uid-{esc(uid)}" data-level="{esc(level)}" data-vip="{profile.get('vip_status',0)==1}" data-spam="{esc(spam_level)}" data-official="{profile.get('official_type',-1)>=0}" data-is-up="{profile.get('archive_count',0)>0}" data-spam-score="{dm.get('spam_score',0.0):.2f}" data-danmaku-count="{esc(dm_count)}" data-fans="{esc(follower)}">
```

说明：`data-is-up` 用 `archive_count`（真实投稿总数）判定 UP 主，替代原 `.stats-bar .stat:nth-child(4)` 的 DOM 位置解析；`data-spam-score`/`data-danmaku-count`/`data-fans` 供前端排序。

- [ ] **Step 2: 验证 report.py 改动（纯函数断言）**

```bash
cd /home/lrxin/文档/bilibili_profiler
.venv/bin/python - <<'EOF'
import sys
sys.path.insert(0, 'src')
from report import generate_user_card
html = generate_user_card({
    "uid": 123, "name": "测试用户", "level": 5, "follower": 42,
    "archive_count": 3, "danmaku": {"count": 7, "spam_level": "中", "spam_score": 0.66, "contents": [], "video_times": []},
})
for attr in ('data-is-up="True"', 'data-spam-score="0.66"', 'data-danmaku-count="7"', 'data-fans="42"', 'id="uid-123"'):
    assert attr in html, f"缺少 {attr}"
print("OK: 卡片 data 属性齐全")
EOF
```

预期输出：`OK: 卡片 data 属性齐全`。

- [ ] **Step 3: web.py 重写 tab-users pane 的 HTML**

原（web.py 报告页 f-string 内）：

```python
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
```

新：

```python
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
```

（搜索框去掉 `oninput` 内联绑定，改由 `initUserCards` 中 300ms 防抖监听。）

- [ ] **Step 4: static/report.js 删除旧筛选代码并写入新体系**

删除整段旧代码（原样，从注释行到 `applyUserFilter` 结束）：

```js
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
```

在原位置写入新代码：

```js
// ===== 用户画像：筛选 + 搜索防抖 + 排序 + 前端分页（全部读卡片 data-* 属性，不再做 DOM 位置解析） =====
const USER_PAGE_SIZE = 24;
const userState = {filter: 'all', kw: '', sort: 'risk', page: 1};
let userSearchTimer = null;
let userCards = [];   // 缓存卡片元素与解析后的数据，避免每次 querySelectorAll 重读 DOM

function userCardData(card) {
    return {
        el: card,
        level: parseInt(card.dataset.level) || 0,
        vip: card.dataset.vip === 'true',
        spam: card.dataset.spam || '低',
        official: card.dataset.official === 'true',
        isUp: card.dataset.isUp === 'true',
        spamScore: parseFloat(card.dataset.spamScore) || 0,
        dmCount: parseInt(card.dataset.danmakuCount) || 0,
        fans: parseInt(card.dataset.fans) || 0,
        name: (card.querySelector('.username')?.textContent || '').toLowerCase(),
        uid: (card.querySelector('.uid')?.textContent || '').toLowerCase(),
        riskIdx: 0,   // 服务端 sort_profiles_by_risk 顺序下标，initUserCards 中赋值
    };
}

function initUserCards() {
    const grid = document.getElementById('userGrid');
    if (!grid) return;
    userCards = Array.from(grid.querySelectorAll('.user-card')).map((el, i) => {
        const d = userCardData(el);
        d.riskIdx = i;
        return d;
    });
    // 昵称/UID 搜索：300ms 防抖
    document.getElementById('userSearch').addEventListener('input', () => {
        clearTimeout(userSearchTimer);
        userSearchTimer = setTimeout(() => {
            userState.kw = document.getElementById('userSearch').value.trim().toLowerCase();
            userState.page = 1;
            renderUserCards();
        }, 300);
    });
    document.getElementById('userSort').addEventListener('change', function() {
        userState.sort = this.value;
        userState.page = 1;
        renderUserCards();
    });
    renderUserCards();
}

function userFilter(type, btn) {
    userState.filter = type;
    userState.page = 1;
    document.querySelectorAll('#tab-users .filter-bar .filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderUserCards();
}

function userMatchFilter(d) {
    switch (userState.filter) {
        case 'all': return true;
        case 'high-level': return d.level >= 5;
        case 'vip': return d.vip;
        case 'official': return d.official;
        case 'spam': return d.spam !== '低';
        case 'creator': return d.isUp;
    }
    return true;
}

function userSortCmp(a, b) {
    switch (userState.sort) {
        case 'spam-score': return b.spamScore - a.spamScore || a.riskIdx - b.riskIdx;
        case 'danmaku': return b.dmCount - a.dmCount || a.riskIdx - b.riskIdx;
        case 'fans': return b.fans - a.fans || a.riskIdx - b.riskIdx;
        default: return a.riskIdx - b.riskIdx;   // risk：保持服务端风险序
    }
}

function renderUserCards() {
    const grid = document.getElementById('userGrid');
    if (!grid) return;
    const hit = userCards.filter(d => userMatchFilter(d) &&
        (!userState.kw || d.name.includes(userState.kw) || d.uid.includes(userState.kw)));
    hit.sort(userSortCmp);
    hit.forEach(d => grid.appendChild(d.el));   // appendChild 移动已挂载节点完成重排
    const pages = Math.max(1, Math.ceil(hit.length / USER_PAGE_SIZE));
    userState.page = Math.min(userState.page, pages);
    const start = (userState.page - 1) * USER_PAGE_SIZE;
    const showSet = new Set(hit.slice(start, start + USER_PAGE_SIZE).map(d => d.el));
    userCards.forEach(d => { d.el.style.display = showSet.has(d.el) ? '' : 'none'; });
    document.getElementById('userResultCount').textContent =
        '共 ' + userCards.length + ' 人 · 命中 ' + hit.length + ' 人';
    document.getElementById('userEmpty').style.display = hit.length ? 'none' : 'block';
    renderUserPager(pages);
}

function renderUserPager(pages) {
    const pager = document.getElementById('userPager');
    if (pages <= 1) { pager.innerHTML = ''; return; }
    let html = '<button class="pager-btn" ' + (userState.page <= 1 ? 'disabled' : '') +
        ' onclick="userPage(' + (userState.page - 1) + ')">上一页</button>';
    for (let p = 1; p <= pages; p++) {
        html += '<button class="pager-btn' + (p === userState.page ? ' active' : '') +
            '" onclick="userPage(' + p + ')">' + p + '</button>';
    }
    html += '<button class="pager-btn" ' + (userState.page >= pages ? 'disabled' : '') +
        ' onclick="userPage(' + (userState.page + 1) + ')">下一页</button>';
    pager.innerHTML = html;
}

function userPage(p) { userState.page = p; renderUserCards(); }
```

- [ ] **Step 5: static/report.js 重写 gotoUser（分页感知定位）**

原：

```js
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
```

新：

```js
// 弹幕浏览器点击发送者跳转到用户画像卡片（锚点 id="uid-{uid}"，spec 4）
// 分页感知：重置筛选/搜索后翻到目标卡片所在页，再滚动高亮
function gotoUser(uid) {
    switchTab('users');
    const el = document.getElementById('uid-' + uid);
    if (!el) return;
    userState.filter = 'all';
    userState.kw = '';
    const searchEl = document.getElementById('userSearch');
    if (searchEl) searchEl.value = '';
    document.querySelectorAll('#tab-users .filter-bar .filter-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.filter === 'all'));
    const d = userCards.find(c => c.el === el);
    if (d) {
        const hit = userCards.filter(x => userMatchFilter(x));
        hit.sort(userSortCmp);
        userState.page = Math.max(1, Math.floor(hit.indexOf(d) / USER_PAGE_SIZE) + 1);
        renderUserCards();
    }
    el.scrollIntoView({behavior: 'smooth', block: 'center'});
    el.style.boxShadow = '0 0 0 3px #00a1d6';
    setTimeout(() => { el.style.boxShadow = ''; }, 2000);
}
```

- [ ] **Step 6: static/report.js 挂载 initUserCards**

在弹幕浏览器事件绑定块（`if (document.getElementById('dmTbody')) { ... }`）之前插入一行调用（画像页 DOM 始终存在，无条件初始化）：

```js
// 用户画像初始化（筛选/排序/分页/搜索防抖）
initUserCards();
```

- [ ] **Step 7: static/report.css 追加画像页样式**

文件末尾追加：

```css
/* 用户画像页：排序/计数/空态/分页（spec 2） */
.sort-select { padding:7px 10px; border:2px solid #e0e0e0; border-radius:8px; font-size:14px; background:white; }
.result-count { font-size:13px; color:#999; align-self:center; margin-left:auto; }
.empty-filter { color:#999; text-align:center; padding:40px; background:white; border-radius:12px; margin-top:10px; }
.user-pager { display:flex; gap:8px; align-items:center; justify-content:center; padding:20px; flex-wrap:wrap; }
.pager-btn { padding:6px 14px; border:2px solid #e0e0e0; border-radius:18px; background:white; cursor:pointer; font-size:14px; }
.pager-btn:hover:not(:disabled) { border-color:#00a1d6; color:#00a1d6; }
.pager-btn.active { background:#00a1d6; color:white; border-color:#00a1d6; }
.pager-btn:disabled { opacity:0.4; cursor:default; }
```

- [ ] **Step 8: 验证**

```bash
cd /home/lrxin/文档/bilibili_profiler
BVID=$(.venv/bin/python -c "import sqlite3;print(sqlite3.connect('data/profiler.db').execute('SELECT bvid FROM videos LIMIT 1').fetchone()[0])")
.venv/bin/python web.py > /tmp/phase1_web.log 2>&1 & WEB_PID=$!
sleep 2
PAGE=$(curl -s "http://127.0.0.1:8000/video/$BVID")
echo "$PAGE" | grep -c 'id="userSort"'          # 预期 1
echo "$PAGE" | grep -c 'id="userResultCount"'   # 预期 1
echo "$PAGE" | grep -c 'id="userPager"'         # 预期 1
echo "$PAGE" | grep -c 'data-is-up='            # 预期 = 画像卡片数（>=1）
echo "$PAGE" | grep -c 'data-filter="all"'      # 预期 1
JS=$(curl -s "http://127.0.0.1:8000/static/report.js")
echo "$JS" | grep -c 'USER_PAGE_SIZE = 24'      # 预期 1
echo "$JS" | grep -c 'nth-child(4)'             # 预期 0（DOM 位置解析已删除）
echo "$JS" | grep -c 'function filter('         # 预期 0（旧函数已删除）
echo "$JS" | grep -c 'initUserCards()'          # 预期 2（函数定义行 + 挂载调用行）
kill $WEB_PID
```

预期：全部计数符合。`data-is-up=` 计数可用 `echo "$PAGE" | grep -o 'class="user-card"' | wc -l` 对照应相等。

---

## Task 4: 弹幕浏览器——spinner、页码跳转、每页条数、错误条+重试

设计规格第 3 章。

**Files:**
- Modify: `web.py`（`api_danmaku` 支持 `page_size` 白名单参数；弹幕 pane HTML 加控件）
- Modify: `static/report.js`（`dmParams`/`loadDanmaku` 改写，新增 `dmGotoPage`/`dmPageSizeChange`）
- Modify: `static/report.css`（spinner/错误条样式）

- [ ] **Step 1: web.py 的 api_danmaku 支持 page_size 参数（白名单 50/100/200）**

在 `page` 解析之后（`except ValueError: page = 1` 一行后）插入：

```python
    # 每页条数：白名单 50/100/200，非法值回退默认 PAGE_SIZE（spec 3 每页条数选择）
    try:
        page_size = int(args.get("page_size", str(PAGE_SIZE)))
    except ValueError:
        page_size = PAGE_SIZE
    if page_size not in (50, 100, 200):
        page_size = PAGE_SIZE
```

`rows_sql` 的 LIMIT/OFFSET，原：

```python
        ORDER BY {sort_col} {order}, d.mid_hash, d.content
        LIMIT {PAGE_SIZE} OFFSET {(page - 1) * PAGE_SIZE}
```

新：

```python
        ORDER BY {sort_col} {order}, d.mid_hash, d.content
        LIMIT {page_size} OFFSET {(page - 1) * page_size}
```

返回值，原：

```python
    return jsonify({"rows": rows, "total": total, "page": page})
```

新：

```python
    return jsonify({"rows": rows, "total": total, "page": page, "page_size": page_size})
```

同时把 `api_danmaku` docstring 中 `page（page_size 固定 100）` 改为 `page、page_size（50/100/200，默认100）`。

- [ ] **Step 2: web.py 弹幕 pane HTML 加分页控件与 spinner/错误条**

`dm-pager` 与 `dmError` 块，原：

```python
            <div class="dm-pager">
                <button id="dmPrev" onclick="dmPage(-1)">上一页</button>
                <span id="dmPageInfo"></span>
                <button id="dmNext" onclick="dmPage(1)">下一页</button>
            </div>
            <div id="dmError" class="dm-error" style="display:none"></div>
```

新：

```python
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
```

- [ ] **Step 3: static/report.js 改写 dmParams 与 loadDanmaku，新增跳转/条数函数**

`dmParams` 原：

```js
    p.set('sort', document.getElementById('dmSort').value);
    p.set('order', document.getElementById('dmOrder').value);
    p.set('page', dmState.page);
    return p.toString();
```

新：

```js
    p.set('sort', document.getElementById('dmSort').value);
    p.set('order', document.getElementById('dmOrder').value);
    p.set('page_size', document.getElementById('dmPageSize').value);
    p.set('page', dmState.page);
    return p.toString();
```

`loadDanmaku` 整函数替换，原：

```js
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
```

新：

```js
function loadDanmaku() {
    const err = document.getElementById('dmError');
    const spinner = document.getElementById('dmSpinner');
    err.style.display = 'none';
    spinner.style.display = 'block';
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
            const pageSize = data.page_size || 100;
            const pages = Math.max(1, Math.ceil(data.total / pageSize));
            document.getElementById('dmPageInfo').textContent =
                '第 ' + data.page + ' / ' + pages + ' 页（共 ' + data.total + ' 行）';
            document.getElementById('dmPrev').disabled = data.page <= 1;
            document.getElementById('dmNext').disabled = data.page >= pages;
        })
        .catch(e => {
            document.getElementById('dmErrorText').textContent = '弹幕加载失败: ' + e.message;
            err.style.display = 'flex';
        })
        .finally(() => { spinner.style.display = 'none'; });
}

// 页码输入跳转（spec 3）
function dmGotoPage() {
    const v = parseInt(document.getElementById('dmGoto').value);
    if (!isNaN(v) && v >= 1) {
        dmState.page = v;
        loadDanmaku();
    }
}

// 每页条数切换（50/100/200）：回到第 1 页
function dmPageSizeChange() {
    dmState.page = 1;
    loadDanmaku();
}
```

- [ ] **Step 4: static/report.js 事件绑定补充**

弹幕浏览器事件绑定块中，原：

```js
    ['dmCategory', 'dmSpam', 'dmSort', 'dmOrder', 'dmAnalyzed'].forEach(id =>
        document.getElementById(id).addEventListener('change', dmReload));
```

新：

```js
    ['dmCategory', 'dmSpam', 'dmSort', 'dmOrder', 'dmAnalyzed'].forEach(id =>
        document.getElementById(id).addEventListener('change', dmReload));
    document.getElementById('dmPageSize').addEventListener('change', dmPageSizeChange);
    document.getElementById('dmGoto').addEventListener('keydown', e => { if (e.key === 'Enter') dmGotoPage(); });
```

- [ ] **Step 5: static/report.css 追加 spinner 与错误条样式（同时删除不再使用的 .dm-error）**

删除一行（已无人使用，`dmError` 改用 `.dm-error-bar`）：

```css
.dm-error { color:#d32f2f; padding:15px; text-align:center; }
```

文件末尾追加：

```css
/* 弹幕浏览器：加载 spinner 与错误条（spec 3） */
.dm-spinner { text-align:center; color:#999; padding:24px; font-size:14px; }
.dm-spinner::before { content:''; display:inline-block; width:14px; height:14px; margin-right:8px; vertical-align:-2px;
    border:2px solid #e0e0e0; border-top-color:#00a1d6; border-radius:50%; animation:dm-spin .8s linear infinite; }
@keyframes dm-spin { to { transform:rotate(360deg); } }
.dm-pager input[type="number"] { width:70px; padding:6px 10px; border:2px solid #e0e0e0; border-radius:20px; font-size:14px; }
.dm-error-bar { display:flex; gap:12px; align-items:center; justify-content:center; color:#d32f2f; padding:15px;
    background:#ffebee; border-radius:8px; margin-top:10px; }
.dm-retry-btn { padding:4px 16px; border:2px solid #d32f2f; border-radius:16px; background:white; color:#d32f2f; cursor:pointer; font-size:13px; }
.dm-retry-btn:hover { background:#d32f2f; color:white; }
```

- [ ] **Step 6: 验证**

```bash
cd /home/lrxin/文档/bilibili_profiler
# 找一个有弹幕数据的视频（danmaku 表非空）
BVID=$(.venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('data/profiler.db')
row = conn.execute('SELECT bvid, COUNT(*) c FROM danmaku GROUP BY bvid ORDER BY c DESC LIMIT 1').fetchone()
print(row[0])")
.venv/bin/python web.py > /tmp/phase1_web.log 2>&1 & WEB_PID=$!
sleep 2
# API：page_size=50 应返回 50 行且带 page_size 字段
.venv/bin/python -c "
import json, urllib.request
j = json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/video/$BVID/danmaku?page_size=50'))
assert j['page_size'] == 50, j.get('page_size')
assert len(j['rows']) == min(50, j['total'])
print('OK: page_size=50 ->', len(j['rows']), 'rows /', j['total'], 'total')"
# 非法 page_size 回退 100
.venv/bin/python -c "
import json, urllib.request
j = json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/video/$BVID/danmaku?page_size=999'))
assert j['page_size'] == 100
print('OK: 非法 page_size 回退 100')"
PAGE=$(curl -s "http://127.0.0.1:8000/video/$BVID")
echo "$PAGE" | grep -c 'id="dmGoto"'      # 预期 1
echo "$PAGE" | grep -c 'id="dmPageSize"'  # 预期 1
echo "$PAGE" | grep -c 'id="dmSpinner"'   # 预期 1
echo "$PAGE" | grep -c 'dm-error-bar'     # 预期 >=1
echo "$PAGE" | grep -c 'dm-retry-btn'     # 预期 1
kill $WEB_PID
```

预期：API 断言全过；页面含全部新控件。

---

## Task 5: 手动分析 job——失败明细透出、重试按钮、重载现场恢复

设计规格第 4 章 + 错误处理章（job 状态接口不可用提示"进度查询失败"）。

**Files:**
- Modify: `web.py`（`_run_analysis_job` 的 errors 结构化）
- Modify: `static/report.js`（`pollJob` 改写、新增 `renderJobFailures`/`saveViewState`/`restoreViewState`、加载恢复 IIFE 重写）
- Modify: `web.py` 弹幕 pane HTML（失败明细条容器）
- Modify: `static/report.css`（`.fail-detail` 样式）

- [ ] **Step 1: web.py 的 _run_analysis_job errors 结构化**

`add_error` 原：

```python
    def add_error(msg):
        with JOBS_LOCK:
            JOBS[job_id]["errors"].append(msg)
        print(f"[Job {job_id}] 失败: {msg}")
```

新：

```python
    def add_error(msg, mid_hash=None):
        # 失败明细结构化：mid_hash 供前端"重试失败项"按钮重新提交，msg 为人类可读摘要
        with JOBS_LOCK:
            JOBS[job_id]["errors"].append({"mid_hash": mid_hash, "error": msg})
        print(f"[Job {job_id}] 失败: {mid_hash or '-'} {msg}")
```

5 处调用点替换：

1. 原 `add_error(f"{e}（job 已终止）")` → 新 `add_error(f"{e}（job 已终止）")`（不变，mid_hash 默认 None）
2. 原 `add_error(f"创建 API 客户端失败: {e}（job 已终止）")` → 不变
3. 原 `add_error(f"{mid_hash}: UID 解析失败（{method}）")` → 新 `add_error(f"UID 解析失败（{method}）", mid_hash)`
4. 原 `add_error(f"{mid_hash} (UID:{uid}): 采集失败 {user_data['error']}")` → 新 `add_error(f"UID:{uid} 采集失败 {user_data['error']}", mid_hash)`
5. 原 `add_error(f"{mid_hash}: {e}")` → 新 `add_error(str(e), mid_hash)`

`_run_analysis_job` docstring 中"单发送者失败记 errors 继续"改为"单发送者失败记 errors（{mid_hash, error} 结构，供前端透出与重试）继续"。

- [ ] **Step 2: 验证 errors 结构化（Flask test_client，无需启动服务）**

```bash
cd /home/lrxin/文档/bilibili_profiler
.venv/bin/python - <<'EOF'
import sys
sys.path.insert(0, 'src')
import web
web.JOBS['test123'] = {"total": 2, "done": 2, "current": "", "finished": True,
                       "results": [123],
                       "errors": [{"mid_hash": "abcd1234", "error": "UID 解析失败（未知）"}]}
c = web.app.test_client()
j = c.get('/api/job/test123').get_json()
assert j['errors'][0]['mid_hash'] == 'abcd1234'
assert j['errors'][0]['error'] == 'UID 解析失败（未知）'
print('OK: /api/job 失败明细结构化透出')
EOF
```

预期输出：`OK: /api/job 失败明细结构化透出`。

- [ ] **Step 3: web.py 弹幕 pane 加失败明细条容器**

弹幕 `dm-controls` 中 `<span id="dmAnalyzeStatus"></span>` 之后、`</div>`（dm-controls 结束）之后插入新块。即原：

```python
            <button id="dmAnalyzeBtn" class="filter-btn" onclick="startAnalysis()">分析选中发送者</button>
            <span id="dmAnalyzeStatus"></span>
        </div>
```

新：

```python
            <button id="dmAnalyzeBtn" class="filter-btn" onclick="startAnalysis()">分析选中发送者</button>
            <span id="dmAnalyzeStatus"></span>
        </div>
        <div id="dmFailBar" class="fail-detail" style="display:none">
            <div>部分发送者分析失败：</div>
            <ul id="dmFailList"></ul>
            <button id="dmRetryBtn" class="filter-btn">重试失败项</button>
            <button id="dmReloadBtn" class="filter-btn">刷新查看结果</button>
        </div>
```

- [ ] **Step 4: static/report.js 改写 pollJob + 新增 renderJobFailures**

`pollJob` 整函数替换，原：

```js
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
```

新：

```js
let jobPollFails = 0;   // 连续轮询失败计数：>=5 判定状态接口不可用，停止轮询并提示（spec 错误处理）
function pollJob(jobId) {
    fetch('/api/job/' + jobId)
        .then(r => r.json())
        .then(j => {
            jobPollFails = 0;
            if (j.error) {  // 服务重启 job 丢失
                dmUpdateAnalyzeStatus('任务状态查询失败: ' + j.error + '（数据可能已部分落库）');
                sessionStorage.removeItem(DM_JOB_KEY);
                return;
            }
            dmUpdateAnalyzeStatus('分析中 ' + j.done + '/' + j.total + (j.current ? '　' + j.current : ''));
            if (j.finished) {
                sessionStorage.removeItem(DM_JOB_KEY);
                const errs = j.errors || [];
                // 新卡片需服务端渲染才有 DOM：记录待高亮 UID 与结果文案，重载后由加载恢复逻辑展示
                sessionStorage.setItem('dmFlash_' + BVID, JSON.stringify(j.results || []));
                sessionStorage.setItem('dmFlashMsg_' + BVID,
                    '手动分析完成: 成功 ' + (j.results || []).length + '/' + j.total +
                    (errs.length ? '（' + errs.length + ' 条失败）' : ''));
                if (errs.length) {
                    // 有失败项：不自动重载，展示失败明细条，由用户选「重试失败项」或「刷新查看结果」
                    renderJobFailures(errs);
                } else {
                    saveViewState();
                    location.reload();
                }
            } else {
                setTimeout(() => pollJob(jobId), 2000);
            }
        })
        .catch(() => {
            jobPollFails++;
            if (jobPollFails >= 5) {
                dmUpdateAnalyzeStatus('进度查询失败，请稍后刷新页面（job 可能仍在后台运行）');
                return;   // job_id 保留在 sessionStorage，刷新后恢复轮询
            }
            setTimeout(() => pollJob(jobId), 2000);
        });
}

// 失败明细条：列出失败 mid_hash 与错误摘要；「重试失败项」把失败集合塞回 dmSelected 复用 startAnalysis
function renderJobFailures(errors) {
    const bar = document.getElementById('dmFailBar');
    document.getElementById('dmFailList').innerHTML = errors.map(e =>
        '<li><code>' + escHtml(e.mid_hash || '-') + '</code>：' + escHtml(e.error) + '</li>').join('');
    bar.style.display = 'block';
    dmUpdateAnalyzeStatus('分析完成，' + errors.length + ' 项失败');
    document.getElementById('dmRetryBtn').onclick = function() {
        const mids = errors.map(e => e.mid_hash).filter(Boolean);
        if (!mids.length) return;
        bar.style.display = 'none';
        dmSelected.clear();
        mids.forEach(m => dmSelected.add(m));
        startAnalysis();
    };
    document.getElementById('dmReloadBtn').onclick = function() {
        saveViewState();
        location.reload();
    };
}
```

- [ ] **Step 5: static/report.js 新增 saveViewState / restoreViewState**

在 `renderJobFailures` 之后插入：

```js
// 现场保存/恢复（sessionStorage，仅本次会话）：job 完成重载页面后回到原标签页/筛选/排序/分页/滚动位置
function saveViewState() {
    const state = {
        tab: document.querySelector('.tab-btn.active')?.dataset.tab || 'overview',
        userFilter: userState.filter,
        userKw: document.getElementById('userSearch')?.value || '',
        userSort: userState.sort,
        userPage: userState.page,
        scrollY: window.scrollY,
    };
    sessionStorage.setItem('viewState_' + BVID, JSON.stringify(state));
}

function restoreViewState() {
    const raw = sessionStorage.getItem('viewState_' + BVID);
    if (!raw) return false;
    sessionStorage.removeItem('viewState_' + BVID);   // 一次性消费
    try {
        const s = JSON.parse(raw);
        userState.filter = s.userFilter || 'all';
        userState.kw = (s.userKw || '').trim().toLowerCase();
        userState.sort = s.userSort || 'risk';
        userState.page = s.userPage || 1;
        const searchEl = document.getElementById('userSearch');
        if (searchEl) searchEl.value = s.userKw || '';
        const sortEl = document.getElementById('userSort');
        if (sortEl) sortEl.value = userState.sort;
        document.querySelectorAll('#tab-users .filter-bar .filter-btn').forEach(b =>
            b.classList.toggle('active', b.dataset.filter === userState.filter));
        renderUserCards();
        switchTab(s.tab || 'overview');
        window.scrollTo(0, s.scrollY || 0);
        return true;
    } catch (e) { return false; }
}
```

- [ ] **Step 6: static/report.js 重写加载恢复 IIFE**

原（Task 2 改后的版本）：

```js
// 页面加载恢复：优先处理"分析完成重载"的跳转+高亮；否则有未完成 job 则继续轮询
(function() {
    const flashMsg = sessionStorage.getItem('dmFlashMsg_' + BVID);
    if (flashMsg) {
        sessionStorage.removeItem('dmFlashMsg_' + BVID);
        const uids = JSON.parse(sessionStorage.getItem('dmFlash_' + BVID) || '[]');
        sessionStorage.removeItem('dmFlash_' + BVID);
        switchTab('users');
        dmUpdateAnalyzeStatus(flashMsg);
        uids.forEach(uid => {
            const el = document.getElementById('uid-' + uid);
            if (el) {
                el.classList.add('flash-highlight');
                setTimeout(() => el.classList.remove('flash-highlight'), 3000);
            }
        });
        const first = document.getElementById('uid-' + uids[0]);
        if (first) first.scrollIntoView({behavior: 'smooth', block: 'center'});
        return;
    }
    const jobId = sessionStorage.getItem(DM_JOB_KEY);
    if (jobId) pollJob(jobId);
})();
```

新：

```js
// 页面加载恢复：优先处理"分析完成重载"的现场恢复+结果提示+高亮；否则恢复手动刷新的现场；
// 再否则有未完成 job 则继续轮询
(function() {
    const flashMsg = sessionStorage.getItem('dmFlashMsg_' + BVID);
    if (flashMsg) {
        sessionStorage.removeItem('dmFlashMsg_' + BVID);
        const uids = JSON.parse(sessionStorage.getItem('dmFlash_' + BVID) || '[]');
        sessionStorage.removeItem('dmFlash_' + BVID);
        restoreViewState();            // 恢复重载前的标签页/筛选/排序/分页/滚动
        switchTab('users');
        dmUpdateAnalyzeStatus(flashMsg);
        uids.forEach(uid => {
            const el = document.getElementById('uid-' + uid);
            if (el) {
                el.classList.add('flash-highlight');
                setTimeout(() => el.classList.remove('flash-highlight'), 3000);
            }
        });
        // gotoUser 内部会把筛选重置为"全部"并翻到目标卡片所在页——新卡片可见优先于现场筛选
        if (uids.length) gotoUser(uids[0]);
        return;
    }
    if (restoreViewState()) return;    // 「刷新查看结果」按钮的手动重载现场
    const jobId = sessionStorage.getItem(DM_JOB_KEY);
    if (jobId) pollJob(jobId);
})();
```

- [ ] **Step 7: static/report.css 追加失败明细条样式**

文件末尾追加：

```css
/* job 失败明细条（spec 4） */
.fail-detail { background:#fff8e1; border:1px solid #ffe082; border-radius:10px; padding:14px 18px; margin-bottom:15px; font-size:14px; }
.fail-detail ul { margin:8px 0 12px 20px; color:#666; }
.fail-detail code { background:#f0f0f0; padding:1px 6px; border-radius:4px; font-size:12px; }
.fail-detail .filter-btn { margin-right:10px; }
```

- [ ] **Step 8: 验证**

```bash
cd /home/lrxin/文档/bilibili_profiler
BVID=$(.venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('data/profiler.db')
print(conn.execute('SELECT bvid FROM danmaku GROUP BY bvid LIMIT 1').fetchone()[0])")
.venv/bin/python web.py > /tmp/phase1_web.log 2>&1 & WEB_PID=$!
sleep 2
PAGE=$(curl -s "http://127.0.0.1:8000/video/$BVID")
echo "$PAGE" | grep -c 'id="dmFailBar"'    # 预期 1
echo "$PAGE" | grep -c 'id="dmRetryBtn"'   # 预期 1
echo "$PAGE" | grep -c 'id="dmReloadBtn"'  # 预期 1
JS=$(curl -s "http://127.0.0.1:8000/static/report.js")
echo "$JS" | grep -c 'function renderJobFailures'   # 预期 1
echo "$JS" | grep -c 'function saveViewState'       # 预期 1
echo "$JS" | grep -c 'function restoreViewState'    # 预期 1
echo "$JS" | grep -c 'jobPollFails >= 5'            # 预期 1
echo "$JS" | grep -c '详见服务端日志'                # 预期 0（旧文案已移除）
grep -c 'add_error(f"{mid_hash}' web.py             # 预期 0（旧拼接格式已移除）
kill $WEB_PID
```

预期：全部计数符合；服务端 errors 结构已在 Step 2 断言过。

---

## Task 6: URL 状态——标签页 hash + 弹幕浏览器 query 参数还原

设计规格第 5 章。依赖 Task 3/4 的 `userState`/`dmParams`/`dmPageSize` 控件。

**Files:**
- Modify: `static/report.js`（`switchTab` 写 hash、新增 `restoreTabFromHash`/`dmSyncUrl`/`dmRestoreFromUrl`、加载恢复 IIFE 加 hash 兜底）

- [ ] **Step 1: static/report.js 的 switchTab 写入 URL hash**

原（Task 2 改后的版本）：

```js
function switchTab(name) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
}
```

新：

```js
function switchTab(name) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
    // 标签页写入 URL hash（spec 5）：刷新/分享链接可回到同一标签页
    if (location.hash !== '#tab=' + name) history.replaceState(null, '', '#tab=' + name);
}

// 从 URL hash 还原标签页（仅接受存在的标签名）
function restoreTabFromHash() {
    const m = location.hash.match(/tab=(\w+)/);
    if (m && document.querySelector('.tab-btn[data-tab="' + m[1] + '"]')) switchTab(m[1]);
}
```

- [ ] **Step 2: static/report.js 新增 dmSyncUrl / dmRestoreFromUrl**

在 `dmParams` 函数之后插入：

```js
// 弹幕浏览器状态写入 query 参数（spec 5）：搜索词/发送者/筛选/排序/页码/每页条数
function dmSyncUrl() {
    const u = new URL(location.href);
    ['search', 'sender', 'category', 'spam', 'sort', 'order', 'page', 'page_size', 'analyzed']
        .forEach(k => u.searchParams.delete(k));
    new URLSearchParams(dmParams()).forEach((v, k) => u.searchParams.set(k, v));
    history.replaceState(null, '', u.toString());
}

// 页面加载时从 query 参数还原弹幕浏览器控件与页码
function dmRestoreFromUrl() {
    const q = new URLSearchParams(location.search);
    const setVal = (id, key) => { if (q.has(key)) document.getElementById(id).value = q.get(key); };
    setVal('dmSearch', 'search');
    setVal('dmSender', 'sender');
    setVal('dmCategory', 'category');
    setVal('dmSpam', 'spam');
    setVal('dmSort', 'sort');
    setVal('dmOrder', 'order');
    setVal('dmPageSize', 'page_size');
    if (q.get('analyzed') === '1') document.getElementById('dmAnalyzed').checked = true;
    const pg = parseInt(q.get('page'));
    if (!isNaN(pg) && pg >= 1) dmState.page = pg;
}
```

- [ ] **Step 3: loadDanmaku 成功后同步 URL**

`loadDanmaku` 的 `.then(data => { ... })` 分支末尾（`document.getElementById('dmNext').disabled = data.page >= pages;` 一行之后）插入一行：

```js
            dmSyncUrl();
```

- [ ] **Step 4: 弹幕初始化时先还原再加载**

事件绑定块中，原：

```js
    loadDanmaku();
}
```

（`if (document.getElementById('dmTbody')) { ... }` 块的最后一行）

新：

```js
    dmRestoreFromUrl();
    loadDanmaku();
}
```

- [ ] **Step 5: 加载恢复 IIFE 加 hash 兜底**

IIFE 尾部，原：

```js
    if (restoreViewState()) return;    // 「刷新查看结果」按钮的手动重载现场
    const jobId = sessionStorage.getItem(DM_JOB_KEY);
    if (jobId) pollJob(jobId);
})();
```

新：

```js
    if (restoreViewState()) return;    // 「刷新查看结果」按钮的手动重载现场
    const jobId = sessionStorage.getItem(DM_JOB_KEY);
    if (jobId) pollJob(jobId);
    restoreTabFromHash();              // 无 job 现场时按 URL hash 还原标签页
})();
```

- [ ] **Step 6: 验证**

URL 状态是浏览器行为，curl 无法执行 JS，本任务做静态断言 + 留待 Task 9 人工检查：

```bash
cd /home/lrxin/文档/bilibili_profiler
JS=static/report.js
grep -c "history.replaceState(null, '', '#tab=' + name)" $JS   # 预期 1
grep -c 'function restoreTabFromHash' $JS                      # 预期 1
grep -c 'function dmSyncUrl' $JS                               # 预期 1
grep -c 'function dmRestoreFromUrl' $JS                        # 预期 1
grep -c 'dmSyncUrl();' $JS                                     # 预期 1（loadDanmaku 成功后调用）
grep -c 'dmRestoreFromUrl();' $JS                              # 预期 1
grep -c 'restoreTabFromHash();' $JS                            # 预期 1
node --version 2>/dev/null && node --check $JS && echo "report.js 语法 OK" || echo "无 node，Task 9 浏览器验证兜底"
```

预期：全部计数符合，JS 语法检查通过（若有 node）。

---

## Task 7: 首页搜索/排序/分页 + 时长播放量列 + 下载区折叠 + 404 页 + 清理残留 HTML

设计规格第 6 章。

**Files:**
- Modify: `web.py`（`index()` 重写、`_export_links` 分层、下载区渲染、404 页）
- Create: `static/index.js`
- Modify: `static/index.css`、`static/report.css`（`.dl-history`/`.nf-card`）
- Delete: `data/reports/report_*.html`
- Modify: `src/exporter.py`（仅 docstring 过时表述）

- [ ] **Step 1: web.py 新增时长格式化辅助函数**

在 `_export_links` 之前插入：

```python
def _fmt_duration(sec) -> str:
    """秒 → mm:ss 或 h:mm:ss 时长文本（首页视频列表时长列）"""
    sec = int(sec or 0)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
```

- [ ] **Step 2: web.py 重写 index()**

整个 `index()` 函数替换，原：

```python
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
<style>{REPORT_CSS}</style>
<link rel="stylesheet" href="/static/index.css">
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
```

新：

```python
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
        <td>{r["view_count"]:,}</td>
        <td>{esc(r["created_at"])}</td>
        <td>{r["dm_count"]:,}</td>
        <td>{r["profile_count"]}</td>
        <td>{r["spam_high"]} / {r["spam_mid"]}</td>
    </tr>''' for r in rows)
    body = items or '<tr><td colspan="8" class="empty-note">暂无已分析视频，先运行 python run.py &lt;BV号&gt;</td></tr>'
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
</div>
<script src="/static/index.js"></script>
</body>
</html>'''
```

（首页分页复用 report.css 的 `.pager-btn` 样式——但首页不加载 report.css，故 `.pager-btn` 样式需在 Step 5 复制进 index.css。）

- [ ] **Step 3: web.py 的 _export_links 分层为最新/历史两组**

原：

```python
def _export_links(bvid: str) -> list[tuple[str, str]]:
    """data/reports/ 下 report_{bvid}_*.csv/.json 下载链接（spec 2：存在才显示，按时间倒序）"""
    links = []
    for ext in ("csv", "json"):
        files = sorted(glob.glob(os.path.join(REPORT_DIR, f"report_{bvid}_*.{ext}")), reverse=True)
        links.extend((os.path.basename(f), ext.upper()) for f in files)
    return links
```

新：

```python
def _export_links(bvid: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """data/reports/ 下 report_{bvid}_*.csv/.json 下载链接，分（最新一组, 历史组）。

    文件名含时间戳，按文件名倒序即时间倒序；每种格式第一个为最新，其余收进历史折叠块。"""
    latest, history = [], []
    for ext in ("csv", "json"):
        files = sorted(glob.glob(os.path.join(REPORT_DIR, f"report_{bvid}_*.{ext}")), reverse=True)
        for i, f in enumerate(files):
            (latest if i == 0 else history).append((os.path.basename(f), ext.upper()))
    return latest, history
```

- [ ] **Step 4: web.py 下载区渲染改折叠**

video_page 中原：

```python
    # CSV/JSON 导出下载链接（spec 2：指向 data/reports/ 同名前缀文件，存在才显示）
    links = " ".join(f'<a class="filter-btn" href="/download/{esc(fname)}">{esc(ext)} 下载</a>'
                     for fname, ext in _export_links(bvid))
```

新：

```python
    # CSV/JSON 导出下载链接：默认只显示最新一组，历史导出收进 <details> 折叠块（spec 6）
    latest, history = _export_links(bvid)

    def _dl_link(fname: str, ext: str) -> str:
        return f'<a class="filter-btn" href="/download/{esc(fname)}">{esc(ext)} 下载</a>'

    links = " ".join(_dl_link(f, e) for f, e in latest)
    if history:
        links += (f' <details class="dl-history"><summary>历史导出（{len(history)} 个）</summary>'
                  f'{" ".join(_dl_link(f, e) for f, e in history)}</details>')
```

- [ ] **Step 5: static/index.css 追加首页控件样式 + static/report.css 追加下载折叠/404 样式**

`static/index.css` 末尾追加：

```css
/* 首页：搜索框 / 可排序列头 / 分页条（spec 6） */
.idx-controls { margin-bottom:16px; }
.video-table th[data-sort] { cursor:pointer; user-select:none; }
.video-table th[data-sort]:hover { color:#00a1d6; }
.video-table th[data-sort]::after { content:' ⇅'; font-size:12px; }
.idx-pager { display:flex; gap:12px; align-items:center; justify-content:center; padding:18px; }
.idx-pager span { font-size:14px; color:#999; }
/* 与 report.css 同款分页按钮（首页不加载 report.css，此处复制） */
.pager-btn { padding:6px 14px; border:2px solid #e0e0e0; border-radius:18px; background:white; cursor:pointer; font-size:14px; }
.pager-btn:hover:not(:disabled) { border-color:#00a1d6; color:#00a1d6; }
.pager-btn:disabled { opacity:0.4; cursor:default; }
```

`static/report.css` 末尾追加：

```css
/* 下载区历史折叠 + 404 卡片（spec 6） */
.dl-history { display:inline-block; margin-left:10px; }
.dl-history summary { cursor:pointer; color:#fff; opacity:0.85; font-size:14px; display:inline; }
.dl-history[open] { display:block; margin:10px 0 0; background:rgba(255,255,255,0.15); border-radius:10px; padding:10px 14px; }
.dl-history[open] summary { margin-bottom:8px; }
.dl-history .filter-btn { margin:4px 6px 0 0; }
.nf-card { background:white; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,0.04); padding:40px; text-align:center; }
.nf-card code { background:#f0f0f0; padding:2px 8px; border-radius:6px; }
.nf-card .filter-btn { display:inline-block; margin-top:16px; text-decoration:none; color:#00a1d6; }
```

- [ ] **Step 6: 创建 static/index.js**

```js
// 首页视频列表：搜索（标题/BV号）+ 列头排序（分析时间/弹幕数/画像人数）+ 分页（每页 20 条）
const IDX_PAGE_SIZE = 20;
const idxState = {kw: '', sortKey: 'time', asc: false, page: 1};
let idxRows = [];

function idxRender() {
    const tbody = document.getElementById('videoTbody');
    const hit = idxRows.filter(tr => !idxState.kw || tr.dataset.title.includes(idxState.kw));
    hit.sort((a, b) => {
        const k = idxState.sortKey;
        const va = a.dataset[k], vb = b.dataset[k];
        const cmp = (k === 'time') ? va.localeCompare(vb) : (parseFloat(va) - parseFloat(vb));
        return idxState.asc ? cmp : -cmp;
    });
    hit.forEach(tr => tbody.appendChild(tr));
    const pages = Math.max(1, Math.ceil(hit.length / IDX_PAGE_SIZE));
    idxState.page = Math.min(idxState.page, pages);
    const start = (idxState.page - 1) * IDX_PAGE_SIZE;
    const show = new Set(hit.slice(start, start + IDX_PAGE_SIZE));
    idxRows.forEach(tr => { tr.style.display = show.has(tr) ? '' : 'none'; });
    document.getElementById('idxPageInfo').textContent =
        hit.length > IDX_PAGE_SIZE ? '第 ' + idxState.page + ' / ' + pages + ' 页（共 ' + hit.length + ' 个视频）' : '';
    document.getElementById('idxPrev').disabled = idxState.page <= 1;
    document.getElementById('idxNext').disabled = idxState.page >= pages;
}

(function idxInit() {
    const tbody = document.getElementById('videoTbody');
    if (!tbody) return;
    idxRows = Array.from(tbody.querySelectorAll('tr[data-title]'));
    document.getElementById('idxSearch').addEventListener('input', function() {
        idxState.kw = this.value.trim().toLowerCase();
        idxState.page = 1;
        idxRender();
    });
    document.querySelectorAll('.video-table th[data-sort]').forEach(th =>
        th.addEventListener('click', () => {
            const k = th.dataset.sort;
            if (idxState.sortKey === k) idxState.asc = !idxState.asc;   // 同列再点翻转升降序
            else { idxState.sortKey = k; idxState.asc = false; }        // 换列默认降序
            idxState.page = 1;
            idxRender();
        }));
    document.getElementById('idxPrev').addEventListener('click', () => { idxState.page--; idxRender(); });
    document.getElementById('idxNext').addEventListener('click', () => { idxState.page++; idxRender(); });
    idxRender();
})();
```

- [ ] **Step 7: web.py 重写 404 错误页**

原：

```python
@app.errorhandler(404)
def not_found(e):
    """未知 bvid / 未知路径 → 中文 404 页面（spec 7）"""
    return ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='UTF-8'><title>404</title></head>"
            "<body style='font-family:sans-serif;text-align:center;padding:80px'>"
            "<h1>404</h1><p>视频不存在或尚未分析，请先运行 python run.py &lt;BV号&gt;</p>"
            "<p><a href='/'>返回首页</a></p></body></html>"), 404
```

新：

```python
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
```

- [ ] **Step 8: 删除 data/reports/ 残留静态 HTML + 更新 exporter.py docstring**

```bash
cd /home/lrxin/文档/bilibili_profiler
ls data/reports/report_*.html   # 先确认只有 report_ 前缀的废弃 HTML（预期: report_BV1BtoYBaELd_20260810_120315.html）
rm -v data/reports/report_*.html
```

`src/exporter.py` 模块 docstring，原：

```python
"""
画像数据导出模块

在 HTML 报告之外，将发送者画像同步导出为 CSV（Excel 汇总查看）和
JSON（完整数据留档/二次分析）。文件名与 HTML 报告同前缀。
"""
```

新：

```python
"""
画像数据导出模块

将发送者画像同步导出为 CSV（Excel 汇总查看）和 JSON（完整数据留档/二次分析）。
文件名前缀 report_{BV号}_{时间}；静态单文件 HTML 报告已移除，下载链接由 web.py 报告页提供。
"""
```

- [ ] **Step 9: 验证**

```bash
cd /home/lrxin/文档/bilibili_profiler
.venv/bin/python -c "import ast; ast.parse(open('web.py').read()); print('web.py 语法 OK')"
.venv/bin/python web.py > /tmp/phase1_web.log 2>&1 & WEB_PID=$!
sleep 2
HOME_PAGE=$(curl -s "http://127.0.0.1:8000/")
echo "$HOME_PAGE" | grep -c 'id="idxSearch"'        # 预期 1
echo "$HOME_PAGE" | grep -c 'id="videoTbody"'       # 预期 1
echo "$HOME_PAGE" | grep -c 'data-sort="time"'      # 预期 1
echo "$HOME_PAGE" | grep -c '<th>时长</th>'         # 预期 1
echo "$HOME_PAGE" | grep -c '<th>播放量</th>'       # 预期 1
echo "$HOME_PAGE" | grep -c 'data-title='           # 预期 = 视频数（当前 6）
# 下载区折叠：找一个有多组导出的视频（report_ 前缀文件多于 2 个）
BVID_DL=$(ls data/reports/ | sed -n 's/^report_\(BV[A-Za-z0-9]*\)_.*/\1/p' | sort | uniq -c | awk '$1>2 {print $2; exit}')
if [ -n "$BVID_DL" ]; then
  curl -s "http://127.0.0.1:8000/video/$BVID_DL" | grep -c 'dl-history'   # 预期 >=1
else
  echo "无多组导出视频，跳过折叠断言"
fi
# 404 页
code=$(curl -s -o /tmp/phase1_404.html -w '%{http_code}' "http://127.0.0.1:8000/video/BV1notexist00")
echo "$code"                                        # 预期 404
grep -c '返回首页' /tmp/phase1_404.html             # 预期 1
grep -c 'nf-card' /tmp/phase1_404.html              # 预期 >=1
kill $WEB_PID
ls data/reports/*.html 2>&1                         # 预期: No such file or directory
```

预期：全部计数符合；404 状态码正确且含返回首页按钮；残留 HTML 已删除。

---

## Task 8: 数据展示增强——spam_score 精确分值、解析徽标 tooltip、采集时间/投稿/直播/动态获赞、覆盖率说明条

设计规格第 7 章 + 错误处理章（字段缺失不渲染对应区块）。

**Files:**
- Modify: `web.py`（`_load_profiles` 注入 `resolve_method`/`resolve_confidence`/`collected_at`；概览页加覆盖率说明条；config 导入补 `HISTORY_MAX_MONTHS`/`HISTORY_MAX_DAYS`）
- Modify: `src/report.py`（`generate_user_card` 刷屏行/基础信息/直播小节；`REPORT_CSS` 追加徽标样式）
- Modify: `static/report.css`（`.coverage-note`）

- [ ] **Step 1: web.py 的 _load_profiles 联查注入解析元数据**

原：

```python
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
```

新：

```python
def _load_profiles(bvid: str) -> list[dict]:
    """该视频已解析发送者的画像（senders JOIN users；同 uid 多 mid_hash 按 uid 去重）。

    附带注入渲染期键（不落库）：resolve_method/resolve_confidence 来自 senders 表
    （卡片解析徽标 tooltip），collected_at 来自 users 表（基础信息采集时间）。
    GROUP BY u.uid 下 method/confidence 取该 uid 任一行（同 uid 多 mid_hash 极少见，可接受）。"""
    conn = get_db()
    rows = conn.execute('''
        SELECT u.profile_json, s.method, s.confidence, u.collected_at
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
        profiles.append(p)
    return profiles
```

- [ ] **Step 2: web.py config 导入补充 + 概览页覆盖率说明条**

导入行，原：

```python
from config import REPORT_DIR, LLM_API_KEY
```

新：

```python
from config import REPORT_DIR, LLM_API_KEY, HISTORY_MAX_MONTHS, HISTORY_MAX_DAYS
```

video_page 中 `coverage` 块之后（`coverage_line` 赋值块结束后）插入：

```python
    # 弹幕覆盖率说明条（spec 7）：明示数据边界——实时池容量有限、历史快照回溯窗口有限、
    # 已解析发送者为按兴趣分阈值入选的子集
    coverage_note = (
        f'<div class="coverage-note">📊 数据边界：实时弹幕池仅保留最近若干条（容量由B站限定，'
        f'超出部分被顶出）；历史弹幕按日快照回溯，最多 {HISTORY_MAX_MONTHS} 个月 / {HISTORY_MAX_DAYS} 天，'
        f'更早的弹幕不在统计范围内。已解析发送者是按兴趣分阈值入选的子集，非全部弹幕发送者。</div>'
    )
```

`tab-overview` pane 开头，原：

```python
    <div id="tab-overview" class="tab-pane active">
        <div class="stats-grid">
```

新：

```python
    <div id="tab-overview" class="tab-pane active">
        {coverage_note}
        <div class="stats-grid">
```

`static/report.css` 末尾追加：

```css
/* 概览页弹幕覆盖率说明条（spec 7） */
.coverage-note { background:#e3f2fd; border-left:4px solid #00a1d6; border-radius:8px;
    padding:12px 16px; font-size:13px; color:#555; margin-bottom:20px; line-height:1.8; }
```

- [ ] **Step 3: src/report.py 的 generate_user_card 刷屏行加精确分值与解析徽标**

在 `spam_class = f"spam-{esc(spam_level)}"` 一行之后插入：

```python
    # 精确刷屏分值 + UID 解析方式/置信度徽标（tooltip 呈现，spec 7）；
    # resolve_method/resolve_confidence 由 web.py _load_profiles 渲染期注入，缺失时不渲染徽标
    spam_score = dm.get("spam_score", 0.0)
    resolve_method = profile.get("resolve_method", "")
    resolve_confidence = profile.get("resolve_confidence", "")
    method_badge = (f'<span class="method-badge" title="UID 解析方式：{esc(resolve_method)}">解析:{esc(resolve_method)}</span>'
                    if resolve_method else "")
    conf_badge = (f'<span class="method-badge" title="解析置信度：{esc(resolve_confidence)}（低置信度可能误识别）">置信度:{esc(resolve_confidence)}</span>'
                  if resolve_confidence else "")
```

弹幕行为小节标题行，原：

```python
            <div class="section">
                <h4>🎤 弹幕行为 <span class="spam-badge {spam_class}">{esc(spam_level)}风险</span></h4>
```

新：

```python
            <div class="section">
                <h4>🎤 弹幕行为 <span class="spam-badge {spam_class}">{esc(spam_level)}风险 {spam_score:.2f}分</span>{method_badge}{conf_badge}</h4>
```

- [ ] **Step 4: src/report.py 基础信息区补采集时间/投稿数/动态获赞 + 新增直播小节**

动态分析取值处，原：

```python
    # 动态
    dyn = profile.get("dynamic", {})
    dyn_count = dyn.get("count", 0)
```

新：

```python
    # 动态
    dyn = profile.get("dynamic", {})
    dyn_count = dyn.get("count", 0)
    dyn_total_likes = dyn.get("total_likes", 0)

    # 采集时间（users.collected_at 渲染期注入，ISO 串取日期部分）；缺失不渲染
    collected_at = (profile.get("collected_at") or "")[:10]
```

基础信息 info-grid，原：

```python
            <div class="section">
                <h4>👤 基础信息</h4>
                <div class="info-grid">
                    <span>性别: {esc(sex) or "未知"}</span>
                    {f'<span>{esc(ip_location)}</span>' if ip_location else ''}
                    <span>活跃模式: {esc(act_type)}</span>
                    {f'<span>高峰时段: {esc(peak_hour)}:00</span>' if peak_hour is not None else ''}
                    {f'<span>活跃星期: {esc(peak_day)}</span>' if peak_day else ''}
                </div>
            </div>
```

新：

```python
            <div class="section">
                <h4>👤 基础信息</h4>
                <div class="info-grid">
                    <span>性别: {esc(sex) or "未知"}</span>
                    {f'<span>{esc(ip_location)}</span>' if ip_location else ''}
                    <span>活跃模式: {esc(act_type)}</span>
                    {f'<span>高峰时段: {esc(peak_hour)}:00</span>' if peak_hour is not None else ''}
                    {f'<span>活跃星期: {esc(peak_day)}</span>' if peak_day else ''}
                    <span>投稿数: {esc(profile.get("archive_count", 0))}</span>
                    {f'<span>动态获赞: {dyn_total_likes:,}</span>' if dyn_total_likes else ''}
                    {f'<span>采集时间: {esc(collected_at)}</span>' if collected_at else ''}
                </div>
            </div>
            {live_section}
```

直播小节构造，在 `# 追番` 注释块之前插入：

```python
    # 直播信息（spec 7）：有直播间才渲染小节；直播中加徽标
    live = profile.get("live", {})
    live_section = ""
    if live.get("has_room"):
        live_status_badge = '<span class="live-badge">直播中</span>' if live.get("is_live") else ""
        live_title = f'：{esc(live.get("room_title", ""))}' if live.get("room_title") else ""
        live_section = f'''
            <div class="section">
                <h4>📡 直播 {live_status_badge}</h4>
                <div class="detail">有直播间{live_title}</div>
            </div>'''
```

- [ ] **Step 5: src/report.py 的 REPORT_CSS 追加徽标样式**

`REPORT_CSS` 中 `@media(max-width:768px){` 一行之前插入：

```css
/* UID 解析徽标与直播徽标（spec 7） */
.method-badge { font-size:11px; background:#f0f0f0; color:#888; padding:2px 8px; border-radius:10px; margin-left:6px; cursor:help; }
.live-badge { font-size:12px; background:#f44336; color:white; padding:2px 8px; border-radius:10px; }
```

- [ ] **Step 6: 验证（纯函数断言 + 页面断言）**

```bash
cd /home/lrxin/文档/bilibili_profiler
.venv/bin/python - <<'EOF'
import sys
sys.path.insert(0, 'src')
from report import generate_user_card
html = generate_user_card({
    "uid": 123, "name": "测试用户", "level": 5, "follower": 42, "archive_count": 3,
    "resolve_method": "评论区验证", "resolve_confidence": "高", "collected_at": "2026-08-16T09:21:44",
    "live": {"has_room": True, "is_live": True, "room_title": "测试直播间"},
    "dynamic": {"count": 5, "total_likes": 88},
    "danmaku": {"count": 7, "spam_level": "中", "spam_score": 0.66, "contents": [], "video_times": []},
})
for s in ('中风险 0.66分', '解析:评论区验证', '置信度:高', 'title="UID 解析方式：评论区验证"',
          '投稿数: 3', '动态获赞: 88', '采集时间: 2026-08-16', '直播中', '测试直播间', 'method-badge', 'live-badge'):
    assert s in html, f"缺少: {s}"
# 缺失字段不渲染（无 live / 无 method）
html2 = generate_user_card({"uid": 1, "name": "x", "danmaku": {"count": 1, "spam_level": "低", "spam_score": 0.1}})
assert 'method-badge' not in html2 and '📡 直播' not in html2 and '采集时间' not in html2
print('OK: 数据展示增强渲染正确，缺失字段不渲染')
EOF
BVID=$(.venv/bin/python -c "import sqlite3;print(sqlite3.connect('data/profiler.db').execute('SELECT bvid FROM videos LIMIT 1').fetchone()[0])")
.venv/bin/python web.py > /tmp/phase1_web.log 2>&1 & WEB_PID=$!
sleep 2
PAGE=$(curl -s "http://127.0.0.1:8000/video/$BVID")
echo "$PAGE" | grep -c 'coverage-note'    # 预期 1（概览页说明条元素；样式定义在 static/report.css）
echo "$PAGE" | grep -c '数据边界'          # 预期 1
echo "$PAGE" | grep -c 'method-badge'     # 预期 >=1（有已解析用户）
kill $WEB_PID
```

预期：断言脚本输出 `OK: 数据展示增强渲染正确，缺失字段不渲染`；页面含覆盖率说明条与解析徽标。

---

## Task 9: 端到端验证 + 文档收尾

设计规格"验证方式"章。

**Files:**
- Modify: `AGENTS.md`（代码结构补 `static/`）

- [ ] **Step 1: AGENTS.md 代码结构补 static/ 说明**

`AGENTS.md` 代码结构代码块中 `web.py` 一行之后插入一行：

```
static/              # Web 报告静态资源（report.css/report.js/index.css/index.js，Flask 默认伺服 /static/；数据经内联 window.__DATA__ 注入）
```

同时把 `web.py` 一行的说明更新为：

```
web.py               # 交互式 Web 报告服务（Flask，首页视频列表 + 四标签页报告 + 弹幕 JSON API；CSS/JS 在 static/，只留路由与数据装配）
```

- [ ] **Step 2: quick_test.py 冒烟（需有效 Cookie 与真实网络）**

```bash
cd /home/lrxin/文档/bilibili_profiler
.venv/bin/python quick_test.py
```

预期：各阶段正常推进（`[3/6] 刷屏检测 + 问题弹幕检测...` 等），末行输出 `✅ 分析完成: N 人生成画像`。本阶段改造未触碰采集流水线，冒烟应无回归；若失败，先对照 `git diff` 确认是否与本次改动无关（网络/风控问题重试即可）。

- [ ] **Step 3: web.py 人工检查清单（浏览器打开 http://127.0.0.1:8000）**

```bash
cd /home/lrxin/文档/bilibili_profiler
.venv/bin/python web.py
```

逐项人工核对（对应设计规格各章）：

1. 报告页只有四个标签页：概览 / 用户画像 / 弹幕浏览器 / 问题弹幕榜；无"完整报告"（spec 1）
2. 切换标签页，地址栏 hash 变为 `#tab=users` 等；带 hash 复制到新标签页打开直接落在对应标签页（spec 5）
3. 用户画像页：6 个筛选按钮（含 UP 主）均生效；搜索输入 300ms 防抖生效；筛选条右侧显示"共 N 人 · 命中 M 人"；筛选无结果显示空态；超过 24 张卡片出现分页条；排序下拉四种排序（默认风险序/刷屏分/弹幕数/粉丝数）均生效；切换排序后分页回到第 1 页（spec 2）
4. 弹幕浏览器：加载时表格区显示 spinner；页码输入 + 跳转生效；每页条数 50/100/200 切换生效；地址栏 query 参数随筛选/翻页更新，复制 URL 新标签页打开还原现场（spec 3/5）
5. 弹幕 API 断网模拟（DevTools offline）点击重试按钮，错误条带"重试"（spec 3）
6. 手动勾选发送者分析：job 完成后页面自动重载并回到原标签页/筛选/滚动位置，新卡片高亮；若制造失败（如断网期间启动），失败明细条列出 mid_hash + 错误摘要，"重试失败项"按钮重新提交（spec 4）
7. 首页：搜索标题/BV号即时过滤；点击"分析时间/弹幕数/画像人数"列头排序（同列再点翻转）；超过 20 条出现分页；时长列格式 mm:ss、播放量千分位（spec 6）
8. 报告页头部下载区：只有最新一组 CSV/JSON 平铺，历史导出收进"历史导出（N 个）"折叠块（spec 6）
9. 访问不存在的 BV 号：404 页有样式与"返回首页"按钮（spec 6）
10. 用户卡片：刷屏行显示"X风险 x.xx分"与"解析:XXX/置信度:X"徽标（悬停有 tooltip）；基础信息有投稿数/采集时间；有直播间的用户显示 📡 直播小节（spec 7）
11. 概览页顶部有蓝色"数据边界"说明条（spec 7）
12. Ctrl+P 打印预览：全部标签页展开、全部卡片可见（无视当前分页）、筛选控件与分页条隐藏（spec 1）

- [ ] **Step 4: 收尾核对**

```bash
cd /home/lrxin/文档/bilibili_profiler
git status --short    # 核对改动范围：web.py / src/report.py / src/exporter.py / AGENTS.md / static/* 新增 / data/reports 删除（data/ 本就在 .gitignore，HTML 删除可能不显示）
git diff --stat
```

确认无规格外改动（不动 storage.py 表结构、不动采集流水线、不引入新依赖）。**不做 git 提交**，由用户验收后自行处理。
## Task 10: 删除报告与重新生成（2026-08-19 追加，设计规格第 9 章）

**Files:**
- Modify: `src/storage.py`（新增 `delete_video_data`，放在 `clear_video_cache` 之后）
- Modify: `web.py`（storage 导入加 `delete_video_data`、顶部加 `from main import run_analysis`；analyze job dict 加 `kind`/`bvid` 字段；新增 `_has_running_job`/`api_delete_video`/`_run_regen_job`/`api_regenerate`；首页表格加操作列；报告页 header 加按钮）
- Modify: `static/index.js`（`regenVideo`/`pollRegen`/`deleteVideo`）
- Modify: `static/report.js`（`reportRegen`/`reportPollRegen`/`reportDelete`）
- Modify: `static/index.css`、`static/report.css`（`.btn-danger`）

- [ ] **Step 1: src/storage.py 新增 delete_video_data**

在 `clear_video_cache` 函数之后插入：

```python
def delete_video_data(bvid: str) -> dict:
    """彻底删除指定视频的全部分析数据（Web 报告页「删除」按钮，spec 9）。

    与 clear_video_cache 的区别——共享缓存一并清除（用户明确选择）：
    - users 画像行无条件删除（即使仍被其他视频的 senders 引用）
    - llm_cache 深掘缓存 deep:{uid}:* 删除
    - global_uid_map 中该视频涉及的 mid_hash 条目删除
    返回各项删除行数 dict。"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT uid, mid_hash FROM senders WHERE bvid = ?", (bvid,))
        rows = cursor.fetchall()
        uids = {r["uid"] for r in rows if r["uid"] is not None}
        mid_hashes = {r["mid_hash"] for r in rows}

        counts = {}
        counts["senders"] = cursor.execute("DELETE FROM senders WHERE bvid = ?", (bvid,)).rowcount
        counts["danmaku"] = cursor.execute("DELETE FROM danmaku WHERE bvid = ?", (bvid,)).rowcount
        counts["videos"] = cursor.execute("DELETE FROM videos WHERE bvid = ?", (bvid,)).rowcount
        counts["cringe_cache"] = cursor.execute(
            "DELETE FROM llm_cache WHERE cache_key LIKE ?", (f"cringe:{bvid}:%",)).rowcount
        counts["deep_cache"] = 0
        for uid in uids:
            counts["deep_cache"] += cursor.execute(
                "DELETE FROM llm_cache WHERE cache_key LIKE ?", (f"deep:{uid}:%",)).rowcount
        counts["global_uid_map"] = 0
        for mh in mid_hashes:
            counts["global_uid_map"] += cursor.execute(
                "DELETE FROM global_uid_map WHERE mid_hash = ?", (mh,)).rowcount
        counts["users"] = 0
        for uid in uids:
            counts["users"] += cursor.execute("DELETE FROM users WHERE uid = ?", (uid,)).rowcount
        conn.commit()
    return counts
```

- [ ] **Step 2: 验证 delete_video_data（用数据库副本，绝不动真实 data/profiler.db）**

```bash
cd /home/lrxin/文档/bilibili_profiler
cp data/profiler.db /tmp/phase1_test.db
.venv/bin/python - <<'EOF'
import sys
sys.path.insert(0, 'src')
import config
config.DB_PATH = '/tmp/phase1_test.db'   # 重定向到副本（以 config 中实际常量名为准，先 grep 确认）
import storage, sqlite3
# 若 storage 模块级缓存了路径，改为 monkeypatch storage 侧引用
bvid = sqlite3.connect('/tmp/phase1_test.db').execute('SELECT bvid FROM videos LIMIT 1').fetchone()[0]
counts = storage.delete_video_data(bvid)
conn = sqlite3.connect('/tmp/phase1_test.db')
for t in ('videos', 'senders', 'danmaku'):
    n = conn.execute(f'SELECT COUNT(*) FROM {t} WHERE bvid = ?', (bvid,)).fetchone()[0]
    assert n == 0, f"{t} 未删净: {n}"
n = conn.execute('SELECT COUNT(*) FROM llm_cache WHERE cache_key LIKE ?', (f'cringe:{bvid}:%',)).fetchone()[0]
assert n == 0, f"cringe 缓存未删净: {n}"
print('OK: delete_video_data 删净', counts)
EOF
rm /tmp/phase1_test.db
```

预期输出：`OK: delete_video_data 删净 {...}`，各计数 >= 0。（注意：必须先 grep config.py/storage.py 确认数据库路径常量的名字与引用方式，按实际 monkeypatch；验证用副本，结束后删除副本。）

- [ ] **Step 3: web.py 导入与 analyze job dict 补字段**

storage 导入块加 `delete_video_data`；顶部导入区加 `from main import run_analysis`（已确认 main.py 不 import web，无循环导入）。

`api_analyze` 的 job dict，原：

```python
        JOBS[job_id] = {"total": len(mid_hashes), "done": 0, "current": "",
                        "errors": [], "finished": False, "results": []}
```

新：

```python
        JOBS[job_id] = {"kind": "analyze", "bvid": bvid, "total": len(mid_hashes), "done": 0, "current": "",
                        "errors": [], "finished": False, "results": []}
```

- [ ] **Step 4: web.py 新增删除/重新生成路由**

在 `api_job` 函数之后插入：

```python
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
```

- [ ] **Step 5: web.py 首页表格加操作列**

表头 `高/中风险</th>` 后加 `<th>操作</th>`；行模板 `spam_high / spam_mid` 单元格后加：

```python
        <td><button class="filter-btn" onclick="regenVideo('{esc(r["bvid"])}', this)">重新生成</button>
            <button class="filter-btn btn-danger" onclick="deleteVideo('{esc(r["bvid"])}')">删除</button></td>
```

空态行 `colspan="8"` 改 `colspan="9"`。

- [ ] **Step 6: web.py 报告页 header 加按钮**

`<div style="margin-top:10px">{links}</div>` 一行之后插入：

```python
        <div style="margin-top:10px">
            <button class="filter-btn" onclick="reportRegen()">🔄 重新生成报告</button>
            <button class="filter-btn btn-danger" onclick="reportDelete()">🗑 删除报告</button>
            <span id="reportJobStatus" style="margin-left:10px"></span>
        </div>
```

- [ ] **Step 7: static/index.js 追加操作列逻辑**

文件末尾追加：

```js
// ===== 操作列：重新生成 / 删除（spec 9） =====
function regenVideo(bvid, btn) {
    if (!confirm('重新生成 ' + bvid + ' 的报告？将清空该视频缓存并后台重跑完整分析流水线。')) return;
    btn.disabled = true;
    btn.textContent = '重跑中…';
    fetch('/api/video/' + encodeURIComponent(bvid) + '/regenerate', {method: 'POST'})
        .then(r => r.json().then(j => ({ok: r.ok, j})))
        .then(({ok, j}) => {
            if (!ok) { alert(j.error || '发起失败'); btn.disabled = false; btn.textContent = '重新生成'; return; }
            pollRegen(j.job_id, bvid, btn);
        })
        .catch(() => { alert('网络错误'); btn.disabled = false; btn.textContent = '重新生成'; });
}

function pollRegen(jobId, bvid, btn) {
    fetch('/api/job/' + jobId)
        .then(r => r.json())
        .then(j => {
            if (j.error) { alert('任务状态查询失败: ' + j.error); return; }
            if (j.finished) {
                if (j.errors && j.errors.length) alert('重新生成失败: ' + j.errors[0].error);
                location.reload();
                return;
            }
            setTimeout(() => pollRegen(jobId, bvid, btn), 2000);
        })
        .catch(() => setTimeout(() => pollRegen(jobId, bvid, btn), 2000));
}

function deleteVideo(bvid) {
    if (!confirm('删除 ' + bvid + ' 的全部分析数据？\n包括：弹幕、发送者、用户画像、LLM 缓存（含共享深掘缓存）、全局映射、导出文件。\n注意：若其他视频涉及相同用户，其报告将缺数据。此操作不可恢复。')) return;
    fetch('/api/video/' + encodeURIComponent(bvid) + '/delete', {method: 'POST'})
        .then(r => r.json().then(j => ({ok: r.ok, j})))
        .then(({ok, j}) => {
            if (!ok) { alert(j.error || '删除失败'); return; }
            location.reload();
        })
        .catch(() => alert('网络错误'));
}
```

- [ ] **Step 8: static/report.js 追加报告页按钮逻辑**

文件末尾追加：

```js
// ===== 报告页：重新生成 / 删除（spec 9） =====
function reportRegen() {
    if (!confirm('重新生成 ' + BVID + ' 的报告？将清空该视频缓存并后台重跑完整分析流水线。')) return;
    const status = document.getElementById('reportJobStatus');
    status.textContent = '重新生成发起中…';
    fetch('/api/video/' + encodeURIComponent(BVID) + '/regenerate', {method: 'POST'})
        .then(r => r.json().then(j => ({ok: r.ok, j})))
        .then(({ok, j}) => {
            if (!ok) { status.textContent = j.error || '发起失败'; return; }
            status.textContent = '重新生成中（完整流水线，可能需要几分钟）…';
            reportPollRegen(j.job_id);
        })
        .catch(() => { status.textContent = '网络错误'; });
}

function reportPollRegen(jobId) {
    fetch('/api/job/' + jobId)
        .then(r => r.json())
        .then(j => {
            const status = document.getElementById('reportJobStatus');
            if (j.error) { status.textContent = '任务状态查询失败: ' + j.error; return; }
            if (j.finished) {
                if (j.errors && j.errors.length) {
                    status.textContent = '重新生成失败: ' + j.errors[0].error;
                    return;
                }
                location.reload();
                return;
            }
            setTimeout(() => reportPollRegen(jobId), 3000);
        })
        .catch(() => setTimeout(() => reportPollRegen(jobId), 3000));
}

function reportDelete() {
    if (!confirm('删除 ' + BVID + ' 的全部分析数据？\n包括：弹幕、发送者、用户画像、LLM 缓存（含共享深掘缓存）、全局映射、导出文件。\n注意：若其他视频涉及相同用户，其报告将缺数据。此操作不可恢复。')) return;
    fetch('/api/video/' + encodeURIComponent(BVID) + '/delete', {method: 'POST'})
        .then(r => r.json().then(j => ({ok: r.ok, j})))
        .then(({ok, j}) => {
            if (!ok) { alert(j.error || '删除失败'); return; }
            location.href = '/';
        })
        .catch(() => alert('网络错误'));
}
```

- [ ] **Step 9: static/index.css 与 static/report.css 各追加 .btn-danger**

两个文件末尾各追加（首页与报告页各加载其一，需双份）：

```css
/* 删除按钮警示色（spec 9） */
.btn-danger { border-color:#f44336 !important; color:#f44336 !important; }
.btn-danger:hover:not(:disabled) { background:#f44336 !important; color:white !important; }
```

- [ ] **Step 10: 验证（不触发真实删除/重跑）**

```bash
cd /home/lrxin/文档/bilibili_profiler
.venv/bin/python -c "import ast; ast.parse(open('web.py').read()); ast.parse(open('src/storage.py').read()); print('语法 OK')"
node --check static/index.js && node --check static/report.js && echo "JS 语法 OK"
# 路由 404/409 行为（test_client，不实际删除/重跑真实数据）
.venv/bin/python - <<'EOF'
import sys
sys.path.insert(0, 'src')
import web
c = web.app.test_client()
assert c.post('/api/video/BV1notexist00/delete').status_code == 404
assert c.post('/api/video/BV1notexist00/regenerate').status_code == 404
bvid = None
import sqlite3
bvid = sqlite3.connect('data/profiler.db').execute('SELECT bvid FROM videos LIMIT 1').fetchone()[0]
web.JOBS['fake_running'] = {"kind": "regen", "bvid": bvid, "total": 1, "done": 0,
                            "current": "", "errors": [], "finished": False, "results": []}
r = c.post(f'/api/video/{bvid}/delete')
assert r.status_code == 409, r.status_code
r = c.post(f'/api/video/{bvid}/regenerate')
assert r.status_code == 409, r.status_code
print('OK: 404/409 行为正确（未实际删除任何数据）')
EOF
# 页面控件
BVID=$(.venv/bin/python -c "import sqlite3;print(sqlite3.connect('data/profiler.db').execute('SELECT bvid FROM videos LIMIT 1').fetchone()[0])")
PROFILER_PORT=8001 .venv/bin/python web.py > /tmp/phase1_web.log 2>&1 & WEB_PID=$!
sleep 2
curl -s "http://127.0.0.1:8001/" | grep -c 'deleteVideo('            # 预期 = 视频数（>=1）
curl -s "http://127.0.0.1:8001/" | grep -c '<th>操作</th>'           # 预期 1
PAGE=$(curl -s "http://127.0.0.1:8001/video/$BVID")
echo "$PAGE" | grep -c 'reportRegen()'      # 预期 1
echo "$PAGE" | grep -c 'reportDelete()'     # 预期 1
echo "$PAGE" | grep -c 'reportJobStatus'    # 预期 1
kill $WEB_PID; sleep 1
PID=$(ss -tlnp 2>/dev/null | grep ':8001' | grep -oP 'pid=\K[0-9]+' | head -1); [ -n "$PID" ] && kill $PID
ss -tln | grep ':8001' || echo "8001 已清理"
```

预期：全部断言通过；**不得对真实数据库执行删除或真实重跑**。

---
