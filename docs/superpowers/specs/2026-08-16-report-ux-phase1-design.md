# 报告层改造（阶段一）设计文档

日期：2026-08-16
状态：已获用户批准（2026-08-16）
范围：报告展示层 UX 改进，**不改动数据库表结构**。弹幕明细入库（mode/color/dmid）列为阶段二，另行设计。

## 背景

当前 Web 报告（`web.py`，Flask，127.0.0.1:8000）存在以下已证实的问题：

- 用户卡片被完整渲染两份（"用户画像"页 + "完整报告"页克隆，`web.py:739`），页面体积翻倍；
- "概览"与"完整报告"内容严重重复（`web.py:857-866` vs `887-899`）；
- 用户卡片无分页、无排序，筛选依赖脆弱的 DOM 位置选择器（`web.py:438`），搜索无防抖；
- 弹幕浏览器加载无提示、分页只有上/下页；
- 手动分析 job 完成后强制 `location.reload()` 丢失现场，失败明细不透出前端；
- 标签页/筛选状态不入 URL，不可分享；
- 首页视频列表无搜索/排序/分页；下载链接全部平铺；
- `senders.spam_score/confidence/method`、`users.collected_at/archive_count`、直播信息等已有数据未展示；
- 弹幕覆盖率上限未明示（`docs/code_review_2026-08-03.md:97` 已建议）。

## 总体方案

分两个阶段推进，本文档只覆盖阶段一。阶段一所有改动集中在报告层：`web.py`、`src/report.py`，以及新增的 `static/` 静态资源；不触碰 `storage.py` 表结构与采集流水线。

## 1. 信息架构

- 删除"完整报告"标签页及其克隆 DOM 与懒初始化图表逻辑。报告页保留四个标签页：概览 / 用户画像 / 弹幕浏览器 / 问题弹幕榜。
- 补一套 `@media print` 样式：打印时展开全部用户卡片、隐藏筛选控件与分页条，替代原"完整报告"的汇总查看场景。
- 页面体积因去除双份 DOM 直接减半。

## 2. 用户画像页

- `generate_user_card()` 输出的卡片根节点写入数据属性：`data-level`、`data-vip`、`data-spam`、`data-official`、`data-is-up`、`data-spam-score`、`data-danmaku-count`、`data-fans`。所有筛选、排序只读数据属性，删除 `web.py:438` 的 DOM 位置解析逻辑。
- 前端分页：每页 24 张卡片，页码条（上一页/页码/下一页）。
- 排序控件：默认风险序（`sort_profiles_by_risk` 服务端顺序）/ 刷屏分 / 弹幕数 / 粉丝数，前端对卡片重排。
- 昵称/UID 搜索加 300ms 防抖。
- 筛选条旁显示"共 N 人 · 命中 M 人"；筛选无结果时显示空态提示。

## 3. 弹幕浏览器

- `loadDanmaku` 期间在表格区域显示 spinner。
- 分页控件增加：页码输入跳转、每页条数选择（50/100/200）。
- API 错误显示为带"重试"按钮的错误条，替代单行红字。

## 4. 手动分析 job

- `/api/job/<job_id>` 的返回增加失败明细：失败 mid_hash 列表与错误摘要（服务端日志仍保留完整 traceback）。
- 前端展示失败明细，并提供"重试失败项"按钮（对失败集合重新 POST `/api/video/<bvid>/analyze`）。
- job 完成后仍重载页面取新数据，但重载前把当前标签页、筛选、排序、分页、滚动位置写入 sessionStorage，加载后自动恢复现场，不再裸 `location.reload()`。

## 5. URL 状态

- 标签页写入 URL hash：`#tab=overview|users|danmaku|cringe`。
- 弹幕浏览器的搜索词、发送者、类别/风险/已解析筛选、排序、页码写入 query 参数。
- 页面加载时从 URL 还原上述状态，使刷新与链接分享均可回到同一现场。

## 6. 首页与下载

- 首页视频列表：加搜索框（匹配标题/BV号）、列头排序（分析时间/弹幕数/画像人数），补充展示 `videos` 表已有的时长与播放量列；超过 20 条时分页。
- 报告页下载区：默认只显示最新一组 CSV/JSON，历史导出收进 `<details>` 折叠块。
- 404 页补基本样式与"返回首页"按钮。
- 删除 `data/reports/` 中残留的废弃静态 HTML 报告文件（不会被列出，仅清理）。

## 7. 数据展示增强

数据均已存在于 `senders`/`users` 表或 profile 字典中，只改渲染：

- 卡片刷屏风险行显示精确 `spam_score` 分值；解析方式（`method`）与置信度（`confidence`）以徽标 tooltip 呈现。
- 卡片基础信息区补充：采集时间（`users.collected_at`）、投稿数（`archive_count`）、直播信息（直播间/标题）、动态获赞数（`dynamic.total_likes`）。
- 概览页顶部加弹幕覆盖率说明条：明示历史弹幕池回溯窗口有限、实时弹幕池有上限，讲清数据边界。

## 8. 工程结构

- 将 `web.py` 中的大段 CSS/JS 字符串常量（`INDEX_EXTRA_CSS`/`VIDEO_EXTRA_CSS`/`DM_CSS`/`VIDEO_JS`）拆分为 `static/` 下的静态文件（如 `static/report.css`、`static/index.css`、`static/report.js`），Flask 路由伺服。
- 数据注入方式从 `.replace('__CHART_JSON__')` 占位符替换，改为页面内联 `<script>` 注入 JSON + 静态 JS 读取 `window.__DATA__`。
- 不引入模板引擎，不改变 `src/` 扁平导入约定，`web.py` 只保留路由与数据装配。

## 错误处理

- 弹幕 API 失败：错误条 + 重试按钮，不静默、不中断页面其余功能。
- 分析 job 失败：明细透出 + 失败项重试；job 状态接口本身不可用时前端提示"进度查询失败"。
- 数据字段缺失（如未采集到直播信息）：卡片对应区块不渲染，不显示"None/未知"占位。

## 验证方式

项目无单元测试框架，按项目惯例：

1. `python quick_test.py` 跑最小化端到端冒烟，确认采集与分析流水线不受影响；
2. `python web.py` 启动后人工检查：四个标签页切换与 hash 还原、用户卡片筛选/排序/分页/搜索防抖、弹幕浏览器加载提示/页码跳转/每页条数、job 重试与现场恢复、首页搜索排序、下载折叠、打印样式、覆盖率说明条。

## 明确不做（YAGNI）

- 图表库本地化、移动端响应式补强（属已排除的"离线与移动端"组）；
- 弹幕 mode/fontsize/color/dmid 入库与相应筛选（阶段二）；
- 引入前端框架或模板引擎；
- `src/` 采集流水线逻辑改动。

---

## 9. 删除报告与重新生成（2026-08-19 追加，用户确认）

**入口**：首页视频表格每行加"操作"列（重新生成/删除按钮）；报告页 header 放同样两个按钮。

**删除**（`POST /api/video/<bvid>/delete`，前端 confirm 二次确认；该视频有任务在跑则 409 拒绝）：
- `videos`/`senders`/`danmaku` 该视频全部记录；
- `llm_cache`：`cringe:{bvid}:*` + 涉及 uid 的 `deep:{uid}:*`（用户选择"连共享缓存也清"）；
- `global_uid_map` 该视频涉及的 mid_hash 条目；
- `users` 表涉及 uid 的画像行**无条件删除**（用户明确选择，已告知会破坏引用相同用户的其他视频报告）；
- `data/reports/` 下 `report_{bvid}_*.csv/.json` 导出文件。

**重新生成**（`POST /api/video/<bvid>/regenerate`）：后台线程跑 `main.run_analysis(bvid, force=True, launch_web=False)`（等价 `run.py --force`），复用 JOBS + `/api/job/<job_id>` 轮询机制（job 加 `kind`/`bvid` 字段）；前端轮询显示"重新生成中"，完成后重载页面。该视频已有任务在跑则 409 拒绝。
