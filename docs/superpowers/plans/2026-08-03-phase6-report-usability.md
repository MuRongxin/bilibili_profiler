# 阶段 6：报告、导出与易用性 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 报告展示阶段 5 的新数据（IP 属地、历史弹幕覆盖率）、修复报告遗留缺陷、新增导出与批量分析、统一 quick_test 与主流程口径。

**上游文档：** 路线图阶段 6；审查报告 Minor（CSS 缺空格、filter 全局 event、quick_test 三项不一致）；阶段5终审 Minor #2（属地未闭环到输出）、#4（quick_test 未合并历史弹幕）。

**测试约定：** 无 pytest，python -c / mock 验证；报告改动用 mock 数据生成 HTML 后 grep 断言。每任务一个 commit。

---

### Task 1: report.py 修复与新数据展示

**Files:**
- Modify: `src/report.py`

**Step 1:** CSS 缺空格修复（审查确认无效声明）：`:263 box-shadow:0 10px40px`、`:277 padding:8px20px`、`:294-296 padding:2px8px`、`:301 padding:15px20px`、`:310 padding:2px8px`（行号以当前代码为准，grep `10px40\|8px20\|2px8\|15px20` 定位全部）。
**Step 2:** `filter()` 依赖隐式全局 `event` 修复（`:459` 附近）：改为 `onclick="filter('all', this)"` 传元素，函数签名同步改。
**Step 3:** IP 属地展示：profile 已有 `ip_location`（如 "IP属地：江苏"）——①用户卡片信息区加属地行（esc 转义）；②汇总区加"地域分布"图表（Chart.js bar，Top 10 省份，数据走 js_json）。
**Step 4:** 历史弹幕覆盖率展示：报告头部视频信息区加一行弹幕覆盖说明（实时池 X + 历史 Y = Z 条）。需要 main.py 把合并统计传给 generate_html_report——读 main.py 的 save_report 调用点与 report.py 函数签名，选最小改动（如在 video_info dict 里附 `danmaku_coverage` 字段，main.py 在 _merge_history_danmaku 后填入）。

**验证：** mock 数据生成报告，grep 断言：属地行存在且转义、地域图表数据进 script（无 `</script>` 逃逸）、CSS 无缺空格残留、`filter('all', this)`。

**提交：** `feat: 报告展示IP属地分布与历史弹幕覆盖率并修复CSS与filter缺陷`

---

### Task 2: exporter.py 导出 CSV/JSON

**Files:**
- Create: `src/exporter.py`
- Modify: `src/main.py`（报告阶段后调用）

**接口：**
```python
def export_csv(profiles: list[dict], path: str):
    """发送者画像汇总导出 CSV（uid/昵称/等级/属地/弹幕数/spam等级/置信度/标签等扁平字段，utf-8-sig 便于 Excel）"""

def export_json(video_info: dict, profiles: list[dict], path: str):
    """完整画像数据导出 JSON（ensure_ascii=False, indent=2）"""
```

main.py 在生成 HTML 报告后同步导出到 `data/reports/report_{bvid}_{ts}.csv/.json`（与 HTML 同名前缀；读 main.py 报告路径生成逻辑复用）。

**验证：** mock profiles 导出后读回断言字段完整、中文不乱码。

**提交：** `feat: 画像数据导出CSV与JSON`

---

### Task 3: run.py 批量 BV 号

**Files:**
- Modify: `run.py`、`src/main.py`

**Step 1:** `run.py` 加 `--batch file.txt`：逐行读 BV 号（忽略空行/#注释），逐个调 `main.run_analysis`，单个失败打印警告继续下一个；共享登录态（run_analysis 内部已处理——读代码确认重复登录行为，如需把 client 改为可复用参数则最小改动）。
**Step 2:** 批量结束打印汇总（成功/失败列表）。

**验证：** 构造 2 个 BV 的临时文件 + mock run_analysis 断言逐个调用、失败不中断。

**提交：** `feat: 支持批量分析多个BV号`

---

### Task 4: quick_test.py 对齐主流程

**Files:**
- Modify: `quick_test.py`

**Step 1:** argparse 化（替换手撸 argv 解析）：`quick_test.py [BV号] [--top N]`。
**Step 2:** 评论采集失败打印警告（当前静默吞，`except Exception: comment_uid_map = {}` 处加 print）。
**Step 3:** LLM Key 判断对齐主流程：`from config import LLM_API_KEY`（当前只看 os.environ）。
**Step 4:** 采集错误检查：`collect_user_data` 返回含 "error" 时跳过该用户（对齐 main.py:177 行为）。
**Step 5:** 历史弹幕合并对齐（阶段5终审 Minor #4）：复用 main.py 的 `_merge_history_danmaku`（from main import，或抽公共函数到 danmaku 模块——读代码选最小改动），使 quick_test 的刷屏 top-N 口径与 run.py 一致。

**验证：** `quick_test.py --help` 正常；mock 各分支断言。

**提交：** `fix: quick_test对齐主流程（argparse/评论警告/Key判断/错误检查/历史弹幕）`

---

### Task 5: README 与 AGENTS.md 同步

**Files:**
- Modify: `README.md`、`AGENTS.md`

**Step 1:** README：核心特性加"全量历史弹幕（每日快照上限 1000 条）""IP 属地画像维度""CSV/JSON 导出""批量分析"；用法加 `--batch`；输出节加 csv/json 文件。
**Step 2:** AGENTS.md：代码结构树补 `danmaku_history.py`、`exporter.py`；运行命令补 `--batch`。

**提交：** `docs: README与AGENTS同步阶段5/6新能力`

---

## Self-Review 记录

- 执行顺序：1 → 2 → 3 → 4 → 5（Task 1 的覆盖率数据依赖 main.py 小改，同任务内完成）。
- Spec 覆盖：路线图阶段 6 全部项（进度条项经评估**砍掉**——阶段5后各阶段已有详细打印，tqdm 不在依赖中，手写进度条收益低；在此声明）+ 阶段5终审 Minor #2/#4。
- quick_test 的 LLM 阶段在 Task 4 Step 3 后与主流程行为一致。

## 全部任务完成后（控制器执行）

- [ ] `python quick_test.py --top 3` 冒烟
- [ ] 整体终审（base=main）→ 合并
