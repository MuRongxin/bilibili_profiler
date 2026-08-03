# AGENTS.md

## 项目概述

**B站弹幕发送者用户画像分析系统**：输入视频 BV 号，采集该视频的全部弹幕，破解弹幕发送者的匿名 `mid_hash`（CRC32 反向搜索 + 评论区明文 UID 交叉验证），再对每个发送者做四维度深度画像（主页信息、互动足迹、社交关系、行为模式），可选调用 LLM 逐人生成 AI 分析，最终输出交互式 HTML 报告（Chart.js）。

- 纯 Python 3 项目，无构建系统（无 pyproject.toml / setup.py / package.json），依赖通过 `requirements.txt` 管理。
- 主要依赖：`requests`（HTTP）、`lxml`（弹幕 XML 解析）、`qrcode` + `pillow`（扫码登录）、`openai`（LLM 客户端）、`pycryptodome`。
- 所有代码注释、打印输出、文档均使用**中文**，回复和代码注释请沿用中文。

## 运行与安装

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 主流程（登录→弹幕→评论→UID解析→刷屏检测→用户采集→画像分析→LLM分析→报告）
python run.py BV1vu4y1b7Y9                # 分析视频
python run.py BV1vu4y1b7Y9 --force        # 忽略缓存强制重新分析
python run.py BV1vu4y1b7Y9 --max-users 50 # 限制最大深度分析用户数

# 辅助脚本
python login.py        # 交互式扫码登录（单独登录用）
python login_bg.py     # 非交互式后台轮询扫码登录
python quick_test.py [BV号] [--top N]  # 快速分析：只分析刷屏得分最高的前 N 个发送者
```

首次运行会提示用 B站 APP 扫码登录；Cookie 自动保存到 `data/cookie.json` 并复用。

## 代码结构

入口 `run.py` 将 `src/` 加入 `sys.path` 后调用 `main.main()`。模块间以**扁平的非包方式导入**（`from config import ...`，不带 `src.` 前缀），修改时务必保持这一约定。

```
src/
├── main.py              # 主控流程：登录→弹幕→评论→UID解析→刷屏检测→用户采集→画像分析→LLM分析→报告
├── config.py            # 全部配置常量：API 端点、限速/重试、采集翻页上限、LLM 配置（含 API Key，已被 .gitignore 排除）
├── api_client.py        # BiliAPIClient：HTTP 封装（限速 0.6–1.0s、重试退避、Cookie）
├── auth.py              # 扫码登录、Cookie 保存/加载/校验
├── danmaku.py           # 弹幕 XML 解析，按 mid_hash 聚合发送者
├── comment.py           # 评论区采集，建立 UID→CRC32 映射
├── uid_resolver.py      # mid_hash 破解：评论区交叉验证（最可靠）+ CRC32 反向暴力搜索（仅 UID<5000万 老用户）
├── spam_detector.py     # 刷屏检测：只标记风险等级（高/中/低），绝不删除弹幕数据
├── user_collector.py    # 四维度用户数据采集（主页/动态/关注/收藏等）
├── profile_analyzer.py  # 规则式画像分析与标签生成
├── llm_analyzer.py      # LLMAnalyzer：调 OpenAI 兼容接口逐人生成 AI 画像（未配置 LLM_API_KEY 时自动跳过）
├── up_analyzer.py       # UP 主相关分析
├── report.py            # HTML 报告生成（内嵌 Chart.js，输出到 data/reports/）
└── storage.py           # SQLite 持久化（data/profiler.db），支撑断点续采
```

数据流：`run.py` → `main.run_analysis(bvid, force, max_users)`，各阶段通过 SQLite 缓存中间结果（已解析的 sender、已采集的 user_data、进度），因此 Ctrl+C 中断后重跑可恢复；`--force` 会忽略这些缓存。

## 开发约定

- **限速是硬约束**：B站 API 有风控，`config.py` 中 `REQUEST_DELAY = 0.6` 秒、高风险 API `1.0` 秒，重试最多 3 次指数退避。新增 API 调用必须走 `BiliAPIClient`，不要绕过限速直接发请求。
- **失败要降级而非中断**：例如评论采集失败时回退为仅用 CRC32 破解；LLM 分析失败只打印警告。单个用户采集异常不得中断整体流水线。
- **不删除数据**：刷屏检测只标记 `spam_level`，不删除任何弹幕。
- 采集规模由 `config.py` 中的 `MAX_*` 常量控制（评论 20 页、关注 50 页、默认最多深度分析 100 人等），调优时改这里而不是散落在代码里的数字。
- 输出文件：`data/reports/report_{BV号}_{时间}.html`（报告）、`data/profiler.db`（数据库）、`data/cookie.json`（登录态）。

## 测试说明

项目**没有单元测试框架**（无 pytest/unittest 目录）。验证方式是直接运行：

- `python quick_test.py` —— 最小化端到端冒烟测试（只分析刷屏 top N 用户，速度快）；
- 或完整 `python run.py <BV号>` 跑通全流程并检查生成的 HTML 报告与控制台各阶段统计。

改动后请至少跑一次 `quick_test.py` 验证。注意运行需要有效 Cookie 和真实网络，且会真实请求 B站 API。

## 安全注意事项

- `src/config.py` 含 LLM API Key 默认值、`data/cookie.json` 含 B站登录凭证，两者连同 `data/profiler.db`、`data/reports/`、`data/qrcode.png` 均已在 `.gitignore` 中排除——**不要把它们提交进仓库或打印到日志**。
- LLM 配置走环境变量覆盖：`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `LLM_MAX_TOKENS`，优先用环境变量而非改 `config.py` 里的硬编码 Key。
- Cookie 等于账号登录态，泄露即等于账号被盗，处理 `data/` 目录文件时保持谨慎。
