# 手动弹幕分析 + 完整报告页 + 自动启动 Web 设计文档

日期：2026-08-15
状态：已获用户批准

## 1. 背景与决策

三个需求（用户已确认的决策）：
1. **弹幕浏览器手动勾选问题弹幕并分析其发送者**：手动选中即**强制采集**（无视置信度，低置信度/CRC32 碰撞带"可能误识别"徽标），含 **LLM 深掘**（用户接受 token 消耗）。
2. **保留原 HTML 卡片式报告样式**：不恢复静态文件，在 web.py 报告页新增"完整报告"标签页，按原静态报告结构纵向呈现（统计卡片→图表→筛选+搜索→全部用户卡片→问题弹幕榜）。
3. **分析完毕自动启动 web.py**：run.py/quick_test.py 结束后自动起服务并打开浏览器。

## 2. 完整报告标签页（web.py + report.py）

- 报告页加第五个标签"完整报告"（放首位还是末位：放**末位**，前四个标签不变）。
- 内容：按原静态 HTML 报告结构纵向排布——统计卡片（stats-grid）→ 图表区（等级/风险/标签/地域）→ 筛选栏 + 昵称/UID 搜索 → 全部用户卡片（user-grid）→ 问题弹幕榜。
- 全部复用现有渲染函数（generate_user_card/generate_summary_stats/generate_chart_data/generate_cringe_board/sort_profiles_by_risk/up_wordcloud_data）与 REPORT_CSS，无新渲染逻辑。
- 筛选按钮与搜索在该标签页内可用（JS 过滤逻辑与"用户画像"标签页共用 applyUserFilter；两页各一份卡片 DOM，筛选状态独立——为避免 DOM id 冲突，完整报告页的筛选/搜索输入用独立 id（如 fullFilter/fullSearch），卡片容器 id 不同，applyUserFilter 参数化容器与输入来源）。
- 图表：完整报告页克隆概览页的 charts-grid HTML，canvas 用独立 id（`levelChart2/spamChart2/tagChart2/regionChart2`），JS 侧对两组 id 分别 new Chart（数据相同，直接复用 chartData）。

## 3. 弹幕手动勾选分析（web.py）

### 3.1 前端

- 弹幕表格每行行首加勾选框，表头加全选（当前页）框；控件区加"分析选中发送者"按钮 + 进度文本区。
- 点击按钮：收集选中行的 mid_hash 列表（去重）→ POST 启动分析 → 每 2 秒轮询 job 进度 → 完成后提示并跳转「完整报告」标签页，新卡片闪烁高亮（复用 gotoUser 的高亮样式思路）。

### 3.2 后端 API

- `POST /api/video/<bvid>/analyze`，body JSON `{"mid_hashes": [...]}`：
  - 未知 bvid → 404 JSON；空列表 → 400 JSON；无有效 Cookie → 503 JSON（"Cookie 失效，请先运行 python login.py"）。
  - 起后台线程跑 job，立即返回 `{"job_id": "..."}`。job 状态存内存 dict（服务重启即失效，可接受）。
- `GET /api/job/<job_id>` → `{"total": n, "done": n, "current": "处理对象描述", "errors": [...], "finished": bool, "results": [uid,...]}`。

### 3.3 job 线程流程（每个 mid_hash 串行，天然限流）

1. **UID 解析**：senders 表该 bvid 缓存已有 uid → 直接用；否则全局映射库命中 → 用；否则 `uid_resolver.resolve_sender`（评论映射为空 dict，全局库优先，CRC32 彩虹表破解兜底）。解析失败记入 errors 继续。
2. **强制采集**：`user_collector.collect_user_data(uid, client)` 无视置信度（含"低"/碰撞）；`save_sender` 落库（confidence 保留真实值，web 画像页可见徽标）。
3. **规则画像**：`profile_analyzer.analyze_profile` + `save_user_data`；`collision_risk` 按解析结果设置。
4. **LLM 深掘**：`LLMAnalyzer().analyze_deep([profile], video_info, top_k=1)`，走 llm_cache（证据未变零 token）；LLM 失败只影响深掘。
- 已分析过的 mid_hash（senders 有 uid 且 users 有数据）→ 直接跳过计入 results。
- web.py 懒加载创建 `BiliAPIClient`（auth.py 加载 data/cookie.json；校验失败按 503 处理）。
- SQLite 并发：job 线程写库均为短事务（现有 save_* 函数），Flask 读侧已有 500 JSON 降级。

## 4. 分析完毕自动启动 Web（main.py / quick_test.py）

- 新增 `src/web_autostart.py`，单一函数 `maybe_launch_web(bvid)`（run.py 与 quick_test.py 共用）：分析结束后：
  1. 探测 `http://127.0.0.1:{PORT}/`（1 秒超时）是否已有服务；
  2. 没有则 `subprocess.Popen([sys.executable, "web.py"], start_new_session=True, stdout/stderr 重定向到 data/web.log)` 分离启动；
  3. `webbrowser.open(f"http://127.0.0.1:{PORT}/video/{bvid}")`；
  4. 全部失败只打印 URL 提示手动访问。
- `config.py` / `config.example.py` 加 `WEB_AUTOSTART = True`（False 关闭）。
- 打印文案从"运行 python web.py 查看交互式报告"改为"报告页已在浏览器打开: URL"（或降级提示）。

## 5. 错误处理

- 单发送者失败记 errors 继续；Cookie 失效 job 整体终止并在 job 状态里标记。
- 分析途中刷新页面：job 继续跑，job_id 存前端 sessionStorage，重开页面可继续轮询（简化：job 列表 API 可省，刷新后丢失进度显示但数据仍会落库——可接受，文档注明）。

## 6. 验证

- 离线：假数据落库 → 起服务 → 勾选分析 API 全流程（mock 掉 BiliAPIClient 采集与 LLM 调用）→ 断言 senders/users 落库与 job 状态机；完整报告标签页元素存在性检查。
- 实跑：`run.py BV1ebg16jEhp`（此前 0 画像的视频）→ 自动开浏览器 → 弹幕浏览器勾选 2-3 个发送者手动分析 → 卡片出现在报告中。人工浏览器检查。
- quick_test.py 冒烟（自动启动不阻塞冒烟结束）。

## 7. 明确不做（YAGNI）

- 不恢复静态 .html 文件生成；不做 job 持久化与历史列表；不做批量全选所有页；不做评论采集加入 web 端解析（web 端无评论映射，接受解析率低于 run.py 的现实）。
