# AGENTS.md

## 项目概述

**B站弹幕发送者用户画像分析系统**：输入视频 BV 号，采集该视频的全部弹幕（实时弹幕池 + 历史快照），先做本地刷屏检测 + LLM 问题弹幕检测（七类：中二抒情/尬夸捧杀/引战阴阳/人身攻击/恶意剧透/广告引流/键政敏感，结果按 llm_cache 缓存），按兴趣分（中/高刷屏或问题弹幕命中）阈值制动态定员，再破解入选发送者的匿名 `mid_hash`（MITM 中间相遇 CRC32 反查 + 评论/充电名单/互动弹幕明文 UID 交叉验证 + 全局映射库），对每个发送者做四维度深度画像（主页信息、互动足迹、社交关系、行为模式），可选调用 LLM 对兴趣分 top K 重点深掘生成 AI 画像（llm_cache 缓存，全员粗筛已砍），最终输出交互式 Web 报告（根目录 `web.py`，Flask 本地服务 127.0.0.1:8000，四标签页：概览/用户画像/弹幕浏览器/问题弹幕榜；静态单文件 HTML 已完全移除）。

- 纯 Python 3 项目，无构建系统（无 pyproject.toml / setup.py / package.json），依赖通过 `requirements.txt` 管理。
- 主要依赖：`requests`（HTTP）、`lxml`（弹幕 XML 解析）、`qrcode` + `pillow`（扫码登录）、`openai`（LLM 客户端）、`pycryptodome`、`flask`（Web 报告服务）。
- 所有代码注释、打印输出、文档均使用**中文**，回复和代码注释请沿用中文。

## 运行与安装

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 主流程（登录→弹幕→刷屏检测→问题弹幕检测→评论→兴趣分UID解析→用户采集→画像分析→LLM深掘→报告）
python run.py BV1vu4y1b7Y9                # 分析视频
python run.py BV1vu4y1b7Y9 --force        # 忽略缓存强制重新分析
python run.py BV1vu4y1b7Y9 --max-users 50 # 手动覆盖动态定员（默认阈值命中者全进，安全上限300）
python run.py --batch videos.txt          # 批量分析（逐行读取BV号，忽略空行与 # 注释行）

# 辅助脚本
python login.py        # 交互式扫码登录（单独登录用）
python login_bg.py     # 非交互式后台轮询扫码登录
python quick_test.py [BV号] [--top N]  # 快速分析：只分析刷屏得分最高的前 N 个发送者
python web.py       # 交互式 Web 报告（127.0.0.1:8000，PROFILER_PORT 可覆盖端口）
```

首次运行会提示用 B站 APP 扫码登录；Cookie 自动保存到 `data/cookie.json` 并复用。

## 代码结构

入口 `run.py` 将 `src/` 加入 `sys.path` 后调用 `main.main()`。模块间以**扁平的非包方式导入**（`from config import ...`，不带 `src.` 前缀），修改时务必保持这一约定。

```
web.py               # 交互式 Web 报告服务（Flask，首页视频列表 + 四标签页报告 + 弹幕 JSON API）
src/
├── main.py              # 主控流程：登录→弹幕(实时+历史)→刷屏检测→问题弹幕检测→评论→兴趣分UID解析→用户采集→画像分析→LLM深掘→报告
├── config.py            # 全部配置常量：API 端点、限速/重试、采集翻页上限、LLM 配置（含 API Key，已被 .gitignore 排除）
├── api_client.py        # BiliAPIClient：HTTP 封装（线程安全限速 0.6–1.0s、重试退避、-412及重签无效的-352/-403风控全局冷却、Cookie、WBI签名、bili_ticket）
├── auth.py              # 扫码登录、Cookie 保存/加载/校验/自动刷新
├── danmaku.py           # 实时弹幕 XML 解析，按 mid_hash 聚合发送者
├── danmaku_history.py   # 历史弹幕采集（逐日弹幕池快照，protobuf wire 手写解析）
├── comment.py           # 评论区采集（wbi/main 游标 + 子评论补采 + IP属地），建立 UID→CRC32 映射
├── uid_resolver.py      # mid_hash 破解：评论/充电名单/互动弹幕/全局库交叉验证 + MITM 反查碰撞消歧
├── crc_rainbow.py       # MITM 中间相遇 CRC32 反查（10万条内存小表，覆盖全部 ≤10 位 UID，秒级）
├── spam_detector.py     # 刷屏检测：只标记风险等级（高/中/低），绝不删除弹幕数据
├── cringe_detector.py   # LLM 问题弹幕检测（七类判定+发送者聚合+llm_cache缓存，未配置 LLM_API_KEY 自动跳过）
├── user_collector.py    # 四维度用户数据采集（主页/动态/关注/收藏等）
├── profile_analyzer.py  # 规则式画像分析与标签生成
├── llm_analyzer.py      # LLMAnalyzer：重点深掘（兴趣分 top K 单人单调用+llm_cache缓存；全员粗筛已砍，未配置 Key 自动跳过）
├── up_analyzer.py       # UP 主相关分析
├── report.py            # 报告渲染函数库（用户卡片/问题弹幕榜/图表统计/基础CSS，被 web.py 复用）
├── exporter.py          # CSV/JSON 数据导出（report_{BV号}_{时间} 前缀，Web 报告页提供下载链接）
└── storage.py           # SQLite 持久化（data/profiler.db），支撑断点续采与 LLM 结果缓存（llm_cache 表、danmaku 全量弹幕表）
```

数据流：`run.py` → `main.run_analysis(bvid, force, max_users)`，各阶段通过 SQLite 缓存中间结果（已解析的 sender、已采集的 user_data），阶段5采集成功立即落库，因此 Ctrl+C 中断后重跑可恢复；`--force` 会清除该视频的缓存并强制重采全部用户（llm_cache 中仅清该视频的问题弹幕判定缓存 `cringe:{bvid}:*`，深掘缓存 `deep:{uid}:*` 跨视频复用、保留）。全局映射库 `global_uid_map` 跨视频沉淀可靠 mid_hash→UID 映射（多候选碰撞条目不沉淀），解析率随使用次数累积提升。

## 开发约定

- **限速是硬约束**：B站 API 有风控，`config.py` 中 `REQUEST_DELAY = 0.6` 秒、高风险 API `1.0` 秒，重试最多 3 次指数退避。新增 API 调用必须走 `BiliAPIClient`，不要绕过限速直接发请求。
- **失败要降级而非中断**：例如评论采集失败时回退为仅用 CRC32 破解；LLM 分析失败只打印警告。单个用户采集异常不得中断整体流水线。
- **不删除数据**：刷屏检测只标记 `spam_level`，不删除任何弹幕。
- 采集规模由 `config.py` 中的 `MAX_*` 常量控制（评论 100 页、子评论补采 25 页/条、动态定员安全上限 MAX_ANALYZE_USERS_HARD_CAP=300、深掘 LLM_DEEP_TOP_K=20、问题弹幕批大小 CRINGE_BATCH_SIZE=200 等），调优时改这里而不是散落在代码里的数字。
- 输出文件：`data/reports/report_{BV号}_{时间}.csv/.json`（CSV/JSON 导出，web.py 报告页提供下载链接）、`data/profiler.db`（数据库）、`data/cookie.json`（登录态）。

## 测试说明

项目**没有单元测试框架**（无 pytest/unittest 目录）。验证方式是直接运行：

- `python quick_test.py` —— 最小化端到端冒烟测试（只分析刷屏 top N 用户，速度快）；
- 或完整 `python run.py <BV号>` 跑通全流程并用 `python web.py` 检查生成的 Web 报告与控制台各阶段统计。

改动后请至少跑一次 `quick_test.py` 验证。注意运行需要有效 Cookie 和真实网络，且会真实请求 B站 API。

## 安全注意事项

- `src/config.py` 含 LLM API Key 默认值、`data/cookie.json` 含 B站登录凭证，两者连同 `data/profiler.db`、`data/reports/`、`data/qrcode.png` 均已在 `.gitignore` 中排除——**不要把它们提交进仓库或打印到日志**。
- LLM 配置走环境变量覆盖：`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `LLM_MAX_TOKENS`，优先用环境变量而非改 `config.py` 里的硬编码 Key。
- Cookie 等于账号登录态，泄露即等于账号被盗，处理 `data/` 目录文件时保持谨慎。
