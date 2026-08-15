# 交互式 Web 数据报告 实现计划

> **REQUIRED SUB-SKILL: superpowers:subagent-driven-development**
>
> - **Goal**：移除静态单文件 HTML 报告，改为 Flask 本地 Web 服务（`web.py`，127.0.0.1:8000）承载交互式报告：多视频首页、报告页四标签页（概览/用户画像/弹幕浏览器/问题弹幕榜）、全量弹幕 SQL 级搜索/筛选/排序/分页。
> - **Architecture**：`storage.py` 新增 `danmaku` 表（阶段2合并后全量落库）；`report.py` 拆成纯渲染函数库（卡片/榜单/图表统计/CSS 常量）；新增根目录 `web.py`（Flask 服务端渲染骨架与卡片 + `/api/video/<bvid>/danmaku` JSON API，原生 JS + fetch，无前端框架无构建）；`exporter.py` 不动，报告页提供 CSV/JSON 下载链接。
> - **Tech stack**：Python 3 + Flask（新增依赖）+ SQLite（现有 profiler.db）+ 原生 JS + Chart.js/wordcloud2 CDN（沿用现有）。
> - **Spec**：`docs/superpowers/specs/2026-08-15-interactive-web-report-design.md`（全部需求以它为准）。

## 项目约定（所有任务必须遵守）

- 运行一律用 `PYTHONPATH=src .venv/bin/python ...`（`run.py`/`quick_test.py`/`web.py` 自身已处理 sys.path，可直接 `.venv/bin/python xxx.py`）。
- 注释/打印输出全中文；扁平导入 `from config import ...`（不带 `src.` 前缀）。
- `src/config.py` 被 gitignore、含真实 LLM Key，**绝不提交**；`data/cookie.json`、`data/profiler.db`、`data/reports/` 同理。
- 失败降级不中断：弹幕落库失败、API 查询异常只警告/返回 500 JSON，不崩溃。
- 无测试框架：验证靠离线脚本（heredoc）+ 后台起服务 curl + 杀掉 + 实跑。

## 现状关键事实（实现前已核对）

- 弹幕 dict 字段：`content`/`time`(float 秒)/`timestamp`(int)/`mid_hash`（实时与历史一致），另有 mode/fontsize/color/pool/dmid 不入库。
- `senders` 表：`(bvid, mid_hash, uid, confidence, method, danmaku_count, contents_json, spam_level, spam_score)`；`users` 表：`(uid, name, level, data_json, profile_json)`，cringe 在 `profile_json` 的 `cringe.categories`。
- `main.py` 的 `save_video_info` 目前在历史弹幕合并**之前**调用，`danmaku_coverage` 不入库，本计划顺手修正。
- CSV/JSON 导出前缀当前取自 `save_report` 返回值，移除后由 main.py 自行构造。

---

## Task 1: storage.py 新增 danmaku 表 + save_danmaku + clear_video_cache 扩展

**文件**：`src/storage.py`

- [ ] Step 1：在 `init_db()` 中 `llm_cache` 建表语句之后（`conn.commit()` 之前）插入：

```python
        # 全量弹幕表（Web 弹幕浏览器数据源；只存 5 列，mode/fontsize/color/pool/dmid 不入库）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS danmaku (
                bvid TEXT NOT NULL,
                mid_hash TEXT NOT NULL,
                content TEXT NOT NULL,
                time REAL NOT NULL,        -- 视频内出现时间(秒)
                timestamp INTEGER NOT NULL -- 发送时间戳
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_danmaku_bvid ON danmaku(bvid)")
```

- [ ] Step 2：在 `# ========== 缓存清理 ==========` 一节之前新增：

```python
# ========== 全量弹幕（Web 弹幕浏览器数据源） ==========

def save_danmaku(bvid: str, danmaku_list: list[dict]):
    """阶段2弹幕合并后批量落库：先删该 bvid 旧行再插入，幂等可重跑。

    只存 bvid/mid_hash/content/time/timestamp 五列；Web API 直接 SQL 查询，无 load_danmaku。
    """
    rows = [(bvid, dm["mid_hash"], dm["content"], dm["time"], dm["timestamp"])
            for dm in danmaku_list]
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM danmaku WHERE bvid = ?", (bvid,))
        cursor.executemany(
            "INSERT INTO danmaku (bvid, mid_hash, content, time, timestamp) VALUES (?, ?, ?, ?, ?)",
            rows)
        conn.commit()
```

- [ ] Step 3：`clear_video_cache()` 中 `cursor.execute("DELETE FROM videos WHERE bvid = ?", (bvid,))` 行之后加一行，并更新 docstring（加一条"- 删除该 bvid 的全部 danmaku 弹幕行"）：

```python
        cursor.execute("DELETE FROM danmaku WHERE bvid = ?", (bvid,))
```

- [ ] Step 4：验证（对真实 DB 操作，但用假 bvid 且最后自清，预期输出含 `OK`）：

```bash
PYTHONPATH=src .venv/bin/python - <<'EOF'
import storage
storage.init_db()
fake = [{"mid_hash": "abcdef01", "content": "测试弹幕", "time": 1.5, "timestamp": 1700000000}]
storage.save_danmaku("BV_FAKE_T1", fake)
storage.save_danmaku("BV_FAKE_T1", fake * 2)  # 幂等：先删再插，应只有 2 行
conn = storage.get_db()
n = conn.execute("SELECT COUNT(*) FROM danmaku WHERE bvid='BV_FAKE_T1'").fetchone()[0]
assert n == 2, n
storage.clear_video_cache("BV_FAKE_T1")
n = conn.execute("SELECT COUNT(*) FROM danmaku WHERE bvid='BV_FAKE_T1'").fetchone()[0]
assert n == 0, n
print("OK: save_danmaku 幂等 + clear_video_cache 清理 danmaku 均通过")
EOF
```

预期输出：`[Storage] 数据库初始化完成` 与 `OK: save_danmaku 幂等 + clear_video_cache 清理 danmaku 均通过`。

- [ ] Step 5：提交

```bash
git add src/storage.py
git commit -m "feat: 新增 danmaku 全量弹幕表与 save_danmaku，clear_video_cache 同步清理"
```

---

## Task 2: main.py/quick_test.py 弹幕落库 + 移除 save_report + 提示 web.py

**文件**：`src/main.py`、`quick_test.py`

- [ ] Step 1：`src/main.py` 头部导入区改为：

```python
import sys
import os
import argparse
from datetime import datetime

from config import MAX_ANALYZE_USERS_HARD_CAP, LLM_API_KEY, HISTORY_DANMAKU_ENABLED, REPORT_DIR
from storage import init_db, save_video_info, save_sender, save_user_data
from storage import load_user_data, has_user_data, load_senders
from storage import clear_video_cache, update_sender_spam, save_global_uid, load_global_uid_map
from storage import save_danmaku
```

并删除 `from report import save_report` 一行（其余导入不动）。

- [ ] Step 2：`phase_danmaku` 整体替换为（save_video_info 挪到历史合并之后；新增 save_danmaku 落库，失败降级）：

```python
def phase_danmaku(bvid: str, client):
    """阶段2: 采集弹幕（实时弹幕池 + 可选历史弹幕快照合并 + 互动弹幕明文mid）"""
    print("\n[Phase 2/6] 采集弹幕数据...")
    video_info, danmaku_list, sender_groups = collect_danmaku_data(bvid, client)

    # 实时弹幕池只保留最近几千条；开启历史弹幕时拉取每日弹幕池快照补全历史
    if HISTORY_DANMAKU_ENABLED:
        danmaku_list, sender_groups = _merge_history_danmaku(video_info, danmaku_list, client)

    # 落库时机在历史合并之后：danmaku_coverage 由 _merge_history_danmaku 写入 video_info，
    # 提前保存会导致 Web 概览页拿不到覆盖率
    save_video_info(bvid, video_info)

    # 全量弹幕落库（web.py 弹幕浏览器数据源；失败只警告不中断主流程）
    try:
        save_danmaku(bvid, danmaku_list)
        print(f"[Phase 2] 已落库 {len(danmaku_list)} 条弹幕（danmaku 表）")
    except Exception as e:
        print(f"[Phase 2] 警告: 弹幕落库失败（{e}），web.py 弹幕浏览器将无本视频数据")

    # 互动弹幕（含明文mid，需SESSDATA；失败降级不影响主流程）
    command_dms = fetch_command_dms(video_info, client)
    video_info["command_dms"] = command_dms

    return video_info, danmaku_list, sender_groups, command_dms
```

- [ ] Step 3：`run_analysis` 中从 `# 生成报告` 到末尾 `print("=" * 60)` 的整段替换为：

```python
    # 静态单文件 HTML 报告已被交互式 Web 报告（web.py）完全替换，不再生成 .html
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_base = os.path.join(REPORT_DIR, f"report_{bvid}_{ts}")

    # 同步导出 CSV/JSON（web.py 报告页提供下载链接）；导出失败只警告降级
    try:
        csv_path = export_base + ".csv"
        export_csv(profiles, csv_path)
        print(f"[Export] CSV 已导出: {csv_path}")
    except Exception as e:
        print(f"[Export] 警告: CSV 导出失败: {e}")
    try:
        json_path = export_base + ".json"
        export_json(video_info, profiles, json_path)
        print(f"[Export] JSON 已导出: {json_path}")
    except Exception as e:
        print(f"[Export] 警告: JSON 导出失败: {e}")

    print("\n" + "=" * 60)
    print("  分析完成!")
    print(f"  视频: {video_info.get('title', '')}")
    print(f"  分析用户: {len(profiles)} 人")
    print("  运行 python web.py 查看交互式报告")
    print("=" * 60)
```

同时把文件 docstring 里 `--force` 那行的"强制重新采集全部用户"保持原样即可（无变化）。

- [ ] Step 4：`quick_test.py` 改动：
  - 删除 `from report import save_report`，新增：

```python
from storage import init_db, save_video_info, save_danmaku
```

  - 在历史合并 `if HISTORY_DANMAKU_ENABLED:` 块之后、`print(f"   弹幕: ...")` 之前插入：

```python
    # 弹幕落库（供 web.py 弹幕浏览器查询；失败只警告不中断冒烟流程）
    try:
        init_db()
        save_video_info(bvid, video_info)
        save_danmaku(bvid, danmaku_list)
    except Exception as e:
        print(f"   警告: 弹幕落库失败（{e}），web.py 中将无该视频数据")
```

  - 末尾报告段替换为：

```python
    # 静态 HTML 报告已被 web.py 交互式报告完全替换
    print(f"\n✅ 分析完成: {len(profiles)} 人生成画像")
    print("   运行 python web.py 查看交互式报告（本视频弹幕已落库）")
```

- [ ] Step 5：验证（语法编译 + save_report 引用清零，均无输出/无匹配即通过）：

```bash
.venv/bin/python -m py_compile src/main.py quick_test.py && echo "OK: 编译通过"
grep -rn "save_report" src/main.py quick_test.py; test $? -eq 1 && echo "OK: save_report 引用已清零"
```

预期输出：`OK: 编译通过` 与 `OK: save_report 引用已清零`。

- [ ] Step 6：提交

```bash
git add src/main.py quick_test.py
git commit -m "feat: 弹幕全量落库，移除 save_report 调用，完成提示改 web.py"
```

---

## Task 3: report.py 拆分（移除静态骨架，保留复用渲染函数 + 锚点）

**文件**：`src/report.py`

- [ ] Step 1：头部导入区移除 `import os` 与 `from config import REPORT_DIR`（其余 `json/re/datetime/Counter/urlparse/_html` 保留），模块 docstring 改为：

```python
"""
报告渲染函数库

静态单文件 HTML 报告已移除（被 web.py 交互式报告完全替换）。
本模块保留可复用的渲染件：用户卡片/问题弹幕榜/图表统计/基础 CSS，
由 web.py 服务端渲染时组装。
"""
```

- [ ] Step 2：新增模块级常量 `REPORT_CSS`：把当前 `generate_html_report` 内 `<style>` 标签中的全部 CSS（现文件第 400–507 行，`body {` 到 `@media` 块结束）原样复制为普通字符串常量（非 f-string），并把 f-string 转义的 `{{`/`}}` 全部还原为单大括号。形如：

```python
# 报告基础样式（原静态 HTML 骨架的 <style> 内容平移；web.py 页面模板注入）
REPORT_CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:#e8ecf1; color:#333; line-height:1.7; font-size:17px; }
...（第 400–507 行全部内容，大括号还原）...
@media(max-width:768px){
.user-grid { grid-template-columns:1fr; }
.stats-bar { grid-template-columns:repeat(3,1fr); }
.info-grid { grid-template-columns:repeat(2,1fr); }
.charts-grid { grid-template-columns:1fr; }
}
"""
```

- [ ] Step 3：`generate_user_card` 返回模板的最外层 div 加锚点（其余不动）：

```python
    return f'''
    <div class="user-card" id="uid-{esc(uid)}" data-level="{esc(level)}" data-vip="{profile.get('vip_status',0)==1}" data-spam="{esc(spam_level)}" data-official="{profile.get('official_type',-1)>=0}">
```

- [ ] Step 4：在 `generate_summary_stats` 之后新增四个函数（逻辑均从被删的 `generate_html_report` 平移）：

```python
def sort_profiles_by_risk(profiles: list[dict]) -> list[dict]:
    """用户卡片排序：风险等级 高→中→低；同级按兴趣分（刷屏分/问题弹幕严重度/弹幕数）降序"""
    risk_rank = {"高": 0, "中": 1, "低": 2}
    return sorted(profiles, key=lambda p: (
        risk_rank.get(p.get("danmaku", {}).get("spam_level", "低"), 2),
        -p.get("danmaku", {}).get("spam_score", 0.0),
        -p.get("cringe", {}).get("max_severity", 0),
        -p.get("danmaku", {}).get("count", 0),
    ))


def generate_chart_data(profiles: list[dict]) -> dict:
    """概览标签页四个图表的数据（等级/刷屏风险/标签/地域分布 Top10+其他）"""
    stats = generate_summary_stats(profiles)
    data = {
        "level_labels": [f"Lv.{i}" for i in range(7)],
        "level_data": [stats["levels"].get(i, 0) for i in range(7)],
        "spam_labels": ["低", "中", "高"],
        "spam_data": [stats["spam_levels"].get(l, 0) for l in ["低", "中", "高"]],
        "tag_labels": list(stats["top_tags"].keys())[:10],
        "tag_data": list(stats["top_tags"].values())[:10],
    }
    # 地域分布：从 ip_location（格式 "IP属地：江苏"）提取省份，Top10 + 其他；
    # 注意无评论属地的用户该键存在但值为 None，不能用 p.get("ip_location", "")
    region_counts = Counter()
    for p in profiles:
        loc = p.get("ip_location") or ""
        if loc.startswith("IP属地："):
            province = loc[len("IP属地："):].strip()
            if province:
                region_counts[province] += 1
    region_top = region_counts.most_common(10)
    region_labels = [k for k, _ in region_top]
    region_data = [v for _, v in region_top]
    other_count = sum(region_counts.values()) - sum(region_data)
    if other_count > 0:
        region_labels.append("其他")
        region_data.append(other_count)
    data["region_labels"] = region_labels
    data["region_data"] = region_data
    return data


def generate_cringe_board(profiles: list[dict]) -> str:
    """问题弹幕榜：按发送者聚合（最高严重度、条数降序），无命中时返回空串"""
    cringe_entries = [p for p in profiles if p.get("cringe", {}).get("count", 0) >= 1]
    cringe_entries.sort(key=lambda p: (p["cringe"].get("max_severity", 0), p["cringe"]["count"]),
                        reverse=True)
    if not cringe_entries:
        return ""
    rows = []
    for p in cringe_entries:
        cr = p["cringe"]
        example = (cr.get("examples") or [{}])[0]
        rows.append(
            f'<tr><td><a href="https://space.bilibili.com/{esc(p.get("uid", 0))}" target="_blank" rel="noopener">{esc(p.get("name", "未知"))}</a></td>'
            f'<td>{esc(cr["count"])}</td>'
            f'<td>{_category_chips(cr.get("categories", []))}</td>'
            f'<td>{esc(cr.get("max_severity", 0))}</td>'
            f'<td>{esc(example.get("content", ""))}<br>'
            f'<span class="cringe-reason">{esc(example.get("category", ""))}: {esc(example.get("reason", ""))}</span></td></tr>'
        )
    return f'''
    <div class="cringe-board">
        <h3>🚨 问题弹幕榜（{len(cringe_entries)} 人命中）</h3>
        <table>
            <thead><tr><th>用户</th><th>命中条数</th><th>类别</th><th>最高严重度</th><th>代表原文</th></tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
    </div>'''


def up_wordcloud_data(profiles: list[dict]) -> dict:
    """收集所有UP主词云数据：{up_{uid}_{i}: [[词, 权重], ...]}，供 up-chip 悬停词云弹窗"""
    result = {}
    for p in profiles:
        uid = p.get("uid", 0)
        fol = p.get("following_summary", {})
        for i, up in enumerate(fol.get("up_details", [])):
            wf = up.get("word_freq", [])
            if wf:
                result[f"up_{uid}_{i}"] = wf
    return result
```

- [ ] Step 5：删除 `generate_html_report` 与 `save_report` 两个函数整体（现第 286–669 行）。

- [ ] Step 6：验证：

```bash
PYTHONPATH=src .venv/bin/python - <<'EOF'
import report
from report import (REPORT_CSS, generate_user_card, generate_summary_stats,
                    generate_chart_data, generate_cringe_board,
                    sort_profiles_by_risk, up_wordcloud_data)
assert not hasattr(report, "save_report") and not hasattr(report, "generate_html_report")
assert ".user-card" in REPORT_CSS and "{{" not in REPORT_CSS
profile = {"uid": 123, "name": "测试用户", "level": 5,
           "danmaku": {"count": 3, "contents": ["a", "b", "c"], "spam_level": "高"},
           "cringe": {"count": 1, "categories": ["引战阴阳"], "max_severity": 4,
                      "examples": [{"content": "a", "category": "引战阴阳", "reason": "测试"}]}}
card = generate_user_card(profile)
assert 'id="uid-123"' in card, "用户卡片缺少锚点 id"
board = generate_cringe_board([profile])
assert "问题弹幕榜" in board and "引战阴阳" in board
assert generate_cringe_board([]) == ""
chart = generate_chart_data([profile])
assert chart["spam_data"] == [0, 0, 1], chart["spam_data"]
assert chart["region_labels"] == []
assert sort_profiles_by_risk([profile])[0]["uid"] == 123
assert up_wordcloud_data([profile]) == {}
print("OK: report.py 拆分通过（锚点/榜单/图表数据/排序/词云/CSS）")
EOF
```

预期输出：`OK: report.py 拆分通过（锚点/榜单/图表数据/排序/词云/CSS）`。

再确认全仓无残留引用：

```bash
grep -rn "save_report\|generate_html_report" src/ run.py quick_test.py; test $? -eq 1 && echo "OK: 无残留引用"
```

- [ ] Step 7：提交

```bash
git add src/report.py
git commit -m "refactor: report.py 拆为渲染函数库，移除静态单文件 HTML 骨架"
```

---

## Task 4: web.py Flask 骨架 + 首页 + 报告页标签页框架

**文件**：`web.py`（新建，项目根目录）

- [ ] Step 1：安装 flask（写入 requirements.txt 在 Task 7）：

```bash
.venv/bin/pip install "flask>=3.0"
```

- [ ] Step 2：新建 `web.py`，完整内容：

```python
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
```

- [ ] Step 3：验证（后台起服务 + curl + 杀掉；用真实 DB 已有视频，若无视频则只验证 404 分支）：

```bash
.venv/bin/python -m py_compile web.py && echo "OK: 编译通过"
PROFILER_PORT=8123 .venv/bin/python web.py > /tmp/web_t4.log 2>&1 &
sleep 2
curl -s -o /dev/null -w "index: %{http_code}\n" http://127.0.0.1:8123/
curl -s -o /dev/null -w "404: %{http_code}\n" http://127.0.0.1:8123/video/BV_NOTEXIST
BV=$(PYTHONPATH=src .venv/bin/python -c "from storage import get_db; r=get_db().execute('SELECT bvid FROM videos LIMIT 1').fetchone(); print(r[0] if r else '')")
echo "BV=$BV"
if [ -n "$BV" ]; then
  curl -s -o /dev/null -w "video: %{http_code}\n" "http://127.0.0.1:8123/video/$BV"
  curl -s "http://127.0.0.1:8123/video/$BV" | grep -o "概览\|用户画像\|弹幕浏览器\|问题弹幕榜" | sort -u
fi
kill %1
```

预期输出：`index: 200`、`404: 404`、有视频时 `video: 200` 且四个标签名全部列出。

- [ ] Step 4：提交

```bash
git add web.py
git commit -m "feat: web.py Flask 骨架——首页视频列表 + 报告页四标签页框架"
```

---

## Task 5: 弹幕 API（GROUP BY mid_hash,content 合并 + 搜索/筛选/排序/分页 + 发送者联查）

**文件**：`web.py`

- [ ] Step 1：在 `@app.route("/download/<path:filename>")` 路由之前新增：

```python
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
        })
    return jsonify({"rows": rows, "total": total, "page": page})
```

- [ ] Step 2：写入离线假数据脚本 `/tmp/fake_web_data.py`（Task 8 复用；覆盖多 bvid/同人重复/跨人同内容/问题弹幕标记/未解析发送者）：

```python
"""离线假数据：BV_FAKEWEB1(8条/3发送者) + BV_FAKEWEB2(1条/1发送者)"""
import sys
sys.path.insert(0, "/home/lrxin/文档/bilibili_profiler/src")
from storage import init_db, save_video_info, save_danmaku, save_sender, save_user_data

init_db()

V1 = "BV_FAKEWEB1"
save_video_info(V1, {"bvid": V1, "title": "假视频一号", "aid": 990001, "cid": 990001,
                     "duration": 100, "stat": {"view": 100, "danmaku": 8, "reply": 0}})
save_danmaku(V1, [
    # 用户甲(111)：同一内容"前排"发 3 次（应合并 ×3）+ "好耶" 1 条
    {"mid_hash": "aaaa0001", "content": "前排", "time": 1.0, "timestamp": 1700000000},
    {"mid_hash": "aaaa0001", "content": "前排", "time": 2.0, "timestamp": 1700000100},
    {"mid_hash": "aaaa0001", "content": "前排", "time": 3.0, "timestamp": 1700000200},
    {"mid_hash": "aaaa0001", "content": "好耶", "time": 10.0, "timestamp": 1700000300},
    # 用户乙(222)：相同内容"前排" 1 条（跨人不得合并）+ 问题弹幕 1 条
    {"mid_hash": "bbbb0002", "content": "前排", "time": 5.0, "timestamp": 1700000400},
    {"mid_hash": "bbbb0002", "content": "垃圾视频取关了", "time": 20.0, "timestamp": 1700000500},
    # 未解析发送者：重复内容 2 条
    {"mid_hash": "cccc0003", "content": "路过", "time": 30.0, "timestamp": 1700000600},
    {"mid_hash": "cccc0003", "content": "路过", "time": 31.0, "timestamp": 1700000700},
])
save_sender(V1, "aaaa0001", 111, "高", "评论区验证", 4, ["前排", "好耶"], "高", 9.5)
save_sender(V1, "bbbb0002", 222, "高", "评论区验证", 2, ["前排", "垃圾视频取关了"], "低", 1.0)
save_user_data(111, "用户甲", 5, {}, {
    "uid": 111, "name": "用户甲", "level": 5,
    "danmaku": {"count": 4, "contents": ["前排", "好耶"], "spam_level": "高"},
    "cringe": {"count": 0, "categories": [], "max_severity": 0, "examples": []}})
save_user_data(222, "用户乙", 3, {}, {
    "uid": 222, "name": "用户乙", "level": 3,
    "danmaku": {"count": 2, "contents": ["前排", "垃圾视频取关了"], "spam_level": "低"},
    "cringe": {"count": 1, "categories": ["引战阴阳"], "max_severity": 4,
               "examples": [{"content": "垃圾视频取关了", "category": "引战阴阳", "reason": "贬低引战"}]}})

V2 = "BV_FAKEWEB2"
save_video_info(V2, {"bvid": V2, "title": "假视频二号", "aid": 990002, "cid": 990002,
                     "duration": 60, "stat": {"view": 5, "danmaku": 1, "reply": 0}})
save_danmaku(V2, [{"mid_hash": "dddd0004", "content": "打卡", "time": 1.0, "timestamp": 1700000000}])

print("假数据落库完成: BV_FAKEWEB1(8条/3发送者) BV_FAKEWEB2(1条/1发送者)")
```

- [ ] Step 3：验证（起服务 + 断言脚本 + 杀掉；每个断言注释即预期）：

```bash
.venv/bin/python /tmp/fake_web_data.py
PROFILER_PORT=8123 .venv/bin/python web.py > /tmp/web_t5.log 2>&1 &
sleep 2
.venv/bin/python - <<'EOF'
import json, urllib.request
B = "http://127.0.0.1:8123/api/video/BV_FAKEWEB1/danmaku"
def q(qs=""):
    with urllib.request.urlopen(B + qs) as r:
        return json.load(r)

d = q()
assert d["total"] == 5, d["total"]            # 8 条合并为 5 行
by = {(r["mid_hash"], r["content"]): r for r in d["rows"]}
assert by[("aaaa0001", "前排")]["dup_count"] == 3   # 同人同内容合并 ×3
assert by[("cccc0003", "路过")]["dup_count"] == 2

d = q("?search=前排")
assert d["total"] == 2, d["total"]            # 跨人同内容不合并：甲/乙各一行
assert sorted(r["dup_count"] for r in d["rows"]) == [1, 3]

assert q("?sender=用户甲")["total"] == 2      # 昵称精确
assert q("?sender=111")["total"] == 2         # UID 精确
assert q("?sender=aaaa0001")["total"] == 2    # mid_hash 精确
assert q("?sender=不存在的人")["total"] == 0

d = q("?category=引战阴阳")
assert d["total"] == 2 and all(r["mid_hash"] == "bbbb0002" for r in d["rows"])
assert d["rows"][0]["categories"] == ["引战阴阳"]

assert q("?spam=高")["total"] == 2            # 甲（spam_level=高）的 2 行
d = q("?spam=未分析")
assert d["total"] == 1 and d["rows"][0]["spam_level"] == "未分析"  # cccc0003 无 senders 行
assert q("?analyzed=1")["total"] == 4         # 排除未解析发送者的 1 行

d = q("?sort=dup_count&order=desc")
assert d["rows"][0]["dup_count"] == 3         # 排序生效
assert d["rows"][0]["sender_count"] == 4      # 甲总弹幕数联查
d = q("?sort=video_time&order=asc")
assert d["rows"][0]["first_video_time"] == 1.0

d = q("?page=2")
assert d["total"] == 5 and d["rows"] == []    # page_size 固定 100，第 2 页为空

try:
    urllib.request.urlopen("http://127.0.0.1:8123/api/video/BV_UNKNOWN/danmaku")
    raise SystemExit("未知 bvid 应返回 404")
except urllib.error.HTTPError as e:
    assert e.code == 404
print("OK: 弹幕 API 全部断言通过（合并/搜索/发送者/类别/风险/已解析/排序/分页/404）")
EOF
kill %1
```

预期输出最后一行：`OK: 弹幕 API 全部断言通过（合并/搜索/发送者/类别/风险/已解析/排序/分页/404）`。

- [ ] Step 4：提交

```bash
git add web.py
git commit -m "feat: 弹幕 JSON API——同人同内容合并 + 搜索/筛选/排序/分页 + 发送者联查"
```

---

## Task 6: 弹幕浏览器前端（表格 + 交互）

**文件**：`web.py`

- [ ] Step 1：把 `DM_CSS = ""` 一行替换为：

```python
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
```

- [ ] Step 2：把 `video_page` 中 `<div id="dmBrowser"></div>` 一行替换为：

```python
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
        </div>
        <div class="dm-table-wrap">
            <table class="dm-table">
                <thead><tr><th>弹幕内容</th><th>发送者</th><th>视频时间</th><th>发送时间</th><th>类别</th><th>刷屏</th></tr></thead>
                <tbody id="dmTbody"></tbody>
            </table>
            <div class="dm-pager">
                <button id="dmPrev" onclick="dmPage(-1)">上一页</button>
                <span id="dmPageInfo"></span>
                <button id="dmNext" onclick="dmPage(1)">下一页</button>
            </div>
            <div id="dmError" class="dm-error" style="display:none"></div>
        </div>'''
```

注意：这整块在 `danmaku_tab = f'''...` 的 f-string 内，原有缩进与结尾 `'''` 保持。

- [ ] Step 3：把 `VIDEO_JS` 末尾的 `// __DM_BROWSER_JS__` 一行替换为：

```javascript
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
                return '<tr><td>' + escHtml(row.content) + dup + '</td><td>' + sender + '</td><td>' +
                    fmtVideoTime(row.first_video_time) + '</td><td>' +
                    new Date(row.first_send_time * 1000).toLocaleString() + '</td><td>' + cats + '</td><td>' +
                    escHtml(row.spam_level) + '</td></tr>';
            }).join('') || '<tr><td colspan="6" class="empty-note">无匹配弹幕</td></tr>';
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
    loadDanmaku();
}
```

- [ ] Step 4：验证（假数据仍在库中时执行；起服务 + curl 静态检查 + API 冒烟 + 杀掉）：

```bash
.venv/bin/python -m py_compile web.py && echo "OK: 编译通过"
PROFILER_PORT=8123 .venv/bin/python web.py > /tmp/web_t6.log 2>&1 &
sleep 2
curl -s http://127.0.0.1:8123/video/BV_FAKEWEB1 > /tmp/v1.html
for id in dmSearch dmSender dmCategory dmSpam dmAnalyzed dmSort dmOrder dmTbody dmPrev dmNext dmPageInfo dmError dmCatChart; do
  grep -q "id=\"$id\"" /tmp/v1.html && echo "OK: $id" || echo "MISSING: $id"
done
grep -c "loadDanmaku\|filterSender\|gotoUser" /tmp/v1.html
curl -s "http://127.0.0.1:8123/api/video/BV_FAKEWEB1/danmaku?sort=dup_count&order=desc" | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); print('rows:', len(d['rows']), 'total:', d['total'])"
curl -s http://127.0.0.1:8123/video/BV_FAKEWEB2 | grep -c "该视频为旧版本分析" ; true
kill %1
```

预期输出：`OK: 编译通过`；12 个元素 id 全部 `OK:`；`loadDanmaku/filterSender/gotoUser` 计数 ≥ 3；API 输出 `rows: 5 total: 5`。注意 BV_FAKEWEB2 有弹幕（1 条），应走正常分支不显示旧版本提示（grep 计数 0）；旧版本提示用真实库中未重采的老视频验证（Task 8）。

- [ ] Step 5：提交

```bash
git add web.py
git commit -m "feat: 弹幕浏览器前端——统计面板联动 + 表格 + 搜索/筛选/排序/分页交互"
```

---

## Task 7: requirements.txt + AGENTS.md/README 文档同步 + spec/plan 提交

**文件**：`requirements.txt`、`AGENTS.md`、`README.md`、`docs/superpowers/`

- [ ] Step 1：`requirements.txt` 追加一行（对齐现有 `openai>=1.0`/`pycryptodome>=3.0` 的 `>=` 风格）：

```
flask>=3.0
```

验证：`.venv/bin/pip install -r requirements.txt` 无错误；`.venv/bin/python -c "import flask; print(flask.__version__)"` 打印 3.x。

- [ ] Step 2：`AGENTS.md` 更新（最小改动）：
  - 项目概述段末尾"最终输出交互式 HTML 报告（Chart.js，含七类分色的"问题弹幕榜"）"改为"最终输出交互式 Web 报告（根目录 `web.py`，Flask 本地服务 127.0.0.1:8000，四标签页：概览/用户画像/弹幕浏览器/问题弹幕榜；静态单文件 HTML 已完全移除）"。
  - 运行与安装代码块加一行：`python web.py       # 交互式 Web 报告（127.0.0.1:8000，PROFILER_PORT 可覆盖端口）`。
  - 主要依赖行加 `` `flask`（Web 报告服务） ``。
  - 代码结构中 `report.py` 注释改为 `# 报告渲染函数库（用户卡片/问题弹幕榜/图表统计/基础CSS，被 web.py 复用）`，并在树上方加一行根目录文件说明 `web.py               # 交互式 Web 报告服务（Flask，首页视频列表 + 四标签页报告 + 弹幕 JSON API）`；`storage.py` 注释补 `danmaku 全量弹幕表`。
  - 输出文件行改为：`data/reports/report_{BV号}_{时间}.csv/.json`（CSV/JSON 导出，web.py 报告页提供下载链接）、`data/profiler.db`、`data/cookie.json`。

- [ ] Step 3：`README.md` 更新：
  - 第 19 行"- **交互式HTML报告**：Chart.js图表 + 用户卡片 + 筛选功能 + 地域分布"改为"- **交互式Web报告**：`python web.py` 启动本地服务（127.0.0.1:8000），多视频浏览 + 概览图表 + 用户画像 + 全量弹幕浏览器 + 问题弹幕榜"。
  - 第 20 行"- **数据导出**：CSV/JSON 与 HTML 报告同名输出"改为"- **数据导出**：CSV/JSON 导出（report_{BV号}_{时间} 前缀），Web 报告页提供下载链接"。
  - "输出"一节（63-66 行）HTML 报告行删除，改为"- **Web 报告**：`python web.py` 后访问 http://127.0.0.1:8000"；CSV/JSON 行保留并把"（与 HTML 同名）"改为"（与分析运行同时间戳）"。
  - 第 83 行 `report.py # HTML报告生成器` 改为 `report.py # 报告渲染函数库（被 web.py 复用）`，并在代码结构树加 `web.py` 行。

- [ ] Step 4：提交（spec/plan 一并入库）：

```bash
git add requirements.txt AGENTS.md README.md \
  docs/superpowers/specs/2026-08-15-interactive-web-report-design.md \
  docs/superpowers/plans/2026-08-15-interactive-web-report.md
git commit -m "docs: Web 报告依赖与文档同步（flask + AGENTS.md/README），提交 spec 与实现计划"
```

---

## Task 8: 离线假数据 curl 验证 + 实跑验证

- [ ] Step 1：端到端离线断言（假数据如已清理先重跑 `/tmp/fake_web_data.py`）：

```bash
.venv/bin/python /tmp/fake_web_data.py   # 幂等，可重复执行
PROFILER_PORT=8123 .venv/bin/python web.py > /tmp/web_t8.log 2>&1 &
sleep 2
curl -s http://127.0.0.1:8123/ | grep -c "假视频一号\|假视频二号"   # 期望 2（两个假视频都在列表）
curl -s -o /dev/null -w "video1: %{http_code}\n" http://127.0.0.1:8123/video/BV_FAKEWEB1   # 200
curl -s "http://127.0.0.1:8123/api/video/BV_FAKEWEB1/danmaku?search=前排" | .venv/bin/python -m json.tool
# 人工核对上面 JSON：total=2，dup_count 分别为 3 和 1（同人合并、跨人不合并）
kill %1
```

- [ ] Step 2：清理假数据（避免污染真实库）：

```bash
PYTHONPATH=src .venv/bin/python -c "
from storage import clear_video_cache
clear_video_cache('BV_FAKEWEB1'); clear_video_cache('BV_FAKEWEB2')
print('假数据已清理')"
```

- [ ] Step 3：实跑验证（全缓存视频，弹幕落库走新代码路径，快）：

```bash
.venv/bin/python run.py BV1wZMy6DE31
```

预期：控制台出现 `[Phase 2] 已落库 N 条弹幕（danmaku 表）`、不再出现 `[Report] 生成HTML报告`、末尾打印 `运行 python web.py 查看交互式报告`。

- [ ] Step 4：起服务检查真实数据：

```bash
.venv/bin/python web.py > /tmp/web_real.log 2>&1 &
sleep 2
curl -s -o /dev/null -w "index: %{http_code}\n" http://127.0.0.1:8000/
curl -s http://127.0.0.1:8000/video/BV1wZMy6DE31 | grep -o "概览\|用户画像\|弹幕浏览器\|问题弹幕榜" | sort -u
curl -s "http://127.0.0.1:8000/api/video/BV1wZMy6DE31/danmaku" | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); print('total:', d['total'], 'rows:', len(d['rows']))"
# 找一个未重采的老视频验证旧数据兼容提示
OLD_BV=$(PYTHONPATH=src .venv/bin/python -c "
from storage import get_db
c = get_db()
r = c.execute('SELECT v.bvid FROM videos v WHERE NOT EXISTS (SELECT 1 FROM danmaku d WHERE d.bvid=v.bvid) LIMIT 1').fetchone()
print(r[0] if r else '')")
if [ -n "$OLD_BV" ]; then curl -s "http://127.0.0.1:8000/video/$OLD_BV" | grep -c "该视频为旧版本分析"; fi
kill %1
```

预期：index 200；四个标签名全列出；API 输出 total>0、rows=100（若 total≥100）；老视频 grep 计数为 1。随后浏览器人工打开 http://127.0.0.1:8000 检查四个标签页（spec 8 人工项）。

- [ ] Step 5：冒烟（需真实网络与有效 Cookie）：

```bash
.venv/bin/python quick_test.py BV1vu4y1b7Y9 --top 1
```

预期：正常跑完，末尾打印 `运行 python web.py 查看交互式报告（本视频弹幕已落库）`，不再生成 .html。

- [ ] Step 6：最终提交（如 Task 2–6 有验证后修正）：

```bash
git add -A src/ web.py quick_test.py
git commit -m "fix: 离线断言与实跑验证修正" || echo "无修正，跳过"
```

---

## 风险与注意

- `web.py` 与 `run.py` 同时写库可能偶发 database locked：API 已按 spec 7 返回 500 JSON 不崩溃；本地工具场景可接受。
- 旧视频的 `users.profile_json` 若缺 `cringe` 字段，`_sender_meta` 的 `.get("cringe", {})` 链已兜底为空列表。
- 弹幕内容含 `%`/`_` 时 LIKE 会当通配符——本地工具可接受，不做转义（YAGNI）。
- `sender_count` 排序走子查询预聚合，万级弹幕下 SQLite 无性能问题；不在本计划加索引（`idx_danmaku_bvid` 已够）。
