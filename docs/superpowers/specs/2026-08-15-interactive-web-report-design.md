# 交互式 Web 数据报告 设计文档

日期：2026-08-15
状态：已获用户批准（用户决策：全视频所有弹幕 / 搜索+筛选+排序+统计面板 / 本地 Web 服务 / 多视频浏览 / 完全替换静态 HTML）

## 1. 背景

静态单文件 HTML 报告无法承载"全量弹幕浏览"（上万条），用户要求改为本地 Web 服务的交互式数据报告，支持多视频浏览，静态 HTML 完全移除。

## 2. 架构

- 新增 `web.py`：Flask 本地服务，监听 `127.0.0.1:8000`（端口可用环境变量 `PROFILER_PORT` 覆盖）。新增依赖 flask，写入 `requirements.txt`。不引入前端框架，原生 JS + fetch，无构建步骤。
- 渲染策略：**服务端渲染 + JSON API 混合**——页面骨架与用户卡片由 Flask 服务端渲染，复用 `report.py` 现有卡片/榜单生成函数；弹幕浏览器走 JSON API（搜索/筛选/排序/分页在 SQL 层完成，前端只渲染当前页）。
- `run.py` 分析完成后不再生成 .html，打印 `运行 python web.py 查看交互式报告`。
- CSV/JSON 导出保留（`exporter.py` 不动），Web 报告页提供下载链接（指向 data/reports/ 下同名前缀文件，存在才显示）。

## 3. 数据层改动（storage.py）

新增 `danmaku` 表：

```sql
CREATE TABLE IF NOT EXISTS danmaku (
    bvid TEXT NOT NULL,
    mid_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    time REAL NOT NULL,        -- 视频内出现时间(秒)
    timestamp INTEGER NOT NULL -- 发送时间戳
);
CREATE INDEX IF NOT EXISTS idx_danmaku_bvid ON danmaku(bvid);
```

- `save_danmaku(bvid, danmaku_list)`：阶段2 弹幕合并后批量写入（先 DELETE 该 bvid 旧行再 INSERT，幂等）。
- `load_danmaku` 不需要——Web API 直接 SQL 查询（搜索/筛选/分页都在 SQL 层）。
- `clear_video_cache(bvid)` 增加删除该 bvid 的 danmaku 行。
- 弹幕字段裁剪：只存上表 5 列（mode/fontsize/color/pool/dmid 不入库，浏览器不展示）。
- 旧数据兼容：历史视频 danmaku 表无数据 → 弹幕板块显示"该视频为旧版本分析，无全量弹幕数据，--force 重采后可浏览"。

## 4. 页面与路由

- `GET /` 首页：视频列表（标题、BV号、分析时间、弹幕数、画像人数、高/中风险人数），点击进入报告页。
- `GET /video/<bvid>` 报告页，顶部标签页：
  - **概览**：现有 Chart.js 图表（等级/风险/标签/地域分布）+ 统计卡片（服务端渲染数据）。
  - **用户画像**：现有用户卡片 + 筛选按钮（全部/Lv.5+/大会员/认证/刷屏/UP主），新增昵称/UID 搜索框（前端过滤）。卡片锚点 `id="uid-{uid}"` 供弹幕浏览器跳转。
  - **弹幕浏览器**：统计面板（总弹幕数/合并后行数/独立发送者数/已解析发送者数 + 问题弹幕类别分布 Chart.js 小图 + 发送者弹幕数 Top10 排行，点击名字筛选该发送者）+ 表格。
  - **问题弹幕榜**：现有榜单平移。
- `GET /api/video/<bvid>/danmaku` 弹幕 API，参数：
  - `search`（内容 LIKE）、`sender`（mid_hash 或昵称/UID 精确）、`category`（7 类之一，命中该发送者的问题弹幕类别）、`spam`（高/中/低/未分析）、`analyzed=1`（只看已解析用户）
  - `sort`（video_time/send_time/dup_count/sender_count）、`order`（asc/desc）、`page`、`page_size`（固定 100）
  - 返回 `{rows: [...], total: int, page: int}`；每行：content、dup_count（同人同内容合并 ×N）、mid_hash、uid、name、first_video_time、first_send_time、categories、spam_level
- 合并规则（核心需求）：**同一 mid_hash 相同 content 合并为一行带 dup_count；不同 mid_hash 的相同内容不合并**。SQL 层 `GROUP BY mid_hash, content`。

## 5. 发送者信息联查

弹幕 API 的 uid/name/categories/spam_level 来自：senders 表（bvid 维度，mid_hash→uid/confidence/spam_level）LEFT JOIN users 表（uid→name）；categories 来自 users.profile_json 的 cringe 字段（Python 侧解析，非 SQL）。未解析发送者 uid/name 为 null，spam_level 显示"未分析"。

## 6. 删减

- `save_report()` 与单文件 HTML 骨架从 `report.py` 移除；保留 `generate_user_card()`、问题弹幕榜生成、图表统计函数，改造为被 web.py 复用。
- `main.py`/`quick_test.py` 移除 save_report 调用；quick_test 末尾提示改打印 web.py 启动方式。
- `exporter.py`（CSV/JSON）保留不动。

## 7. 错误处理

- 数据库锁定/查询异常 → API 返回 500 JSON 错误，前端显示错误提示，不崩溃。
- 未知 bvid → 404 页面。
- Flask 仅作为本地工具，debug=False。

## 8. 验证

- 离线：构造假数据（多 bvid、同人重复弹幕、跨人同内容弹幕、问题弹幕标记）落库 → 起服务 → curl 断言：`/` 列表正确；`/api/.../danmaku` 的合并规则（同人合并、跨人不合并）、搜索/筛选/排序/分页正确；`/video/<bvid>` 200。
- 实跑：`run.py BV1wZMy6DE31`（全缓存，快）→ `python web.py` → 浏览器人工检查四个标签页。
- `quick_test.py` 冒烟通过。

## 9. 明确不做（YAGNI）

- 前端框架（Vue/React）、用户登录、远程访问、弹幕发送、实时刷新。
- 评论浏览板块（评论数据不落库，只在 profile 里）。
- 静态 HTML 报告（明确完全替换）。
