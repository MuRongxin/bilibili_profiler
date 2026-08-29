# AGENTS.md

## 项目概述

**B站弹幕发送者用户画像分析系统**：输入视频 BV 号，采集该视频的全部弹幕（实时弹幕池 + 历史快照），先做本地刷屏检测 + LLM 问题弹幕检测（八类：中二抒情/尬夸捧杀/引战阴阳/人身攻击/恶意剧透/广告引流/键政敏感/批评吐槽，二游社区语境口径，结果按 llm_cache 缓存）+ LLM 问题评论检测（同八类口径，回写 comments.problem 列，`cmt:{bvid}:*` 缓存），按兴趣分（中/高刷屏或问题弹幕命中）阈值制动态定员，再破解入选发送者的匿名 `mid_hash`（MITM 中间相遇 CRC32 反查 + 评论/充电名单/互动弹幕/视频元信息（UP主/联合投稿staff/简介@提及 desc_v2）明文 UID 交叉验证 + 全局映射库）；问题评论达阈值（严重度≥COMMENT_AUTHOR_MIN_SEVERITY 或 命中≥COMMENT_AUTHOR_MIN_HITS 条）的作者凭明文 UID 以合成键 `cmt:{uid}` 直引画像名单（senders 表 method="问题评论"、danmaku_count=0），对每个发送者做四维度深度画像（主页信息、互动足迹、社交关系、行为模式），可选调用 LLM 对兴趣分 top K 重点深掘生成 AI 画像（llm_cache 缓存，全员粗筛已砍），最终输出交互式 Web 报告（根目录 `web.py`，Flask 本地服务 127.0.0.1:8000，七标签页：概览/用户画像/弹幕浏览器/问题弹幕榜/争执焦点（问题回复按 parent_rpid 还原 A→B 攻击边、挑事者/被围攻者双榜附代表原文（被围攻者成对展示受害原评+攻击者攻击原文；名额随攻击边数动态浮动：保底 ATTACK_FOCUS_TOP_N=5、每10边+1、封顶 ATTACK_FOCUS_MAX_N=20；顶部关系图画布（多圆簇布局：每个连通分量一个独立小圆——受害者居中、其攻击者环绕，1:1对成小簇，无边节点独立环簇，簇按半径网格排布；SVG 箭头线随攻击者分色、线粗映射攻击次数，头像节点大小映射攻击/被攻击次数（face_cache 后台补采），环上标签沿切线旋转、中心受害者标签上下交错，同一 uid 只画一个节点（既攻击又被攻击的链条节点标签显示 攻×n 被×n、同时有进出边），悬停节点高亮其全部关系边，点击节点定位到下方明细条目）））/问题评论榜（全部问题评论按 点赞+回复数×权重 热度降序、楼中楼附父评原文）/高回复评论（潜在争执热点：回复数达阈值的评论单独成页，完整回复树按 parent_rpid 嵌套缩进、默认折叠可展开，评论者直接显示用户名（comments.uname），楼主/UP主身份徽标，问题评论分色标注；悬停评论组静止600ms弹该组讨论主题词云（data-wc 服务端注入词频，复用 wc-popup）），原"完整报告"标签页已移除、由 @media print 打印样式替代；概览页含操作条（返回首页/重新生成/删除/导出下载）、弹幕密度时间轴（按视频内时间分桶直方图，点击柱条跳转视频对应时段核验）、解析质量区块（解析方式/置信度分布 + 碰撞风险人数）；问题弹幕/问题评论条目带「误报」人工纠偏按钮（false_positive 表持久化，弹幕按内容、评论按 rpid，标记后不计入聚合与用户疑似分、可撤销，跨重跑保留，删除报告时清除）；首页含跨视频重叠用户面板（≥2 个已分析视频都出现过的发送者，视频条目可展开查看该用户在其中的弹幕/评论明细样本）；用户卡片含「其他视频足迹」区块（该用户在其他已分析视频中的弹幕与评论样本）与毕业院校徽标（school），并可经高回复评论作者进入「用户互动时间线」页（/user/<uid>，该用户在全部已分析视频中的弹幕/评论按最近互动倒序）；标签页/弹幕筛选状态写入 URL 可分享；弹幕浏览器支持勾选 mid_hash 手动触发强制分析（后台 job：UID解析+采集+画像+LLM深掘，失败明细透出可重试）；报告页操作条支持删除报告/重新生成（后台完整重跑流水线）；报告页整页 HTML 按 bvid 内存缓存（_PAGE_CACHE 存数据指纹+HTML，job 完成/删除/误报标记时主动失效，且每请求比对六表聚合指纹（含 face_cache 头像缓存）、外部进程 run.py 落库变化也能检出自动重渲染）；Chart.js 与 wordcloud2 已本地化到 static/（离线可用）；run.py/quick_test.py 分析完毕自动启动 web.py 并打开报告页（WEB_AUTOSTART 可关）；静态单文件 HTML 已完全移除）。

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
python run.py BV1vu4y1b7Y9 --max-users 50 # 手动覆盖动态定员（默认阈值命中者全进，上限随发送者规模浮动：保底300/发送者数×5%/封顶1000）
python run.py --batch videos.txt          # 批量分析（逐行读取BV号，忽略空行与 # 注释行）

# 辅助脚本
python login.py        # 交互式扫码登录（主号，单独登录用）
python login.py alt1   # 扫码登录小号 alt1（存 data/cookies/alt1.json，run.py 阶段5自动发现轮转分摊采集）
python login_bg.py     # 非交互式后台轮询扫码登录（同样可选跟账号名）
python quick_test.py [BV号] [--top N]  # 快速分析：只分析刷屏得分最高的前 N 个发送者
python web.py       # 交互式 Web 报告（127.0.0.1:8000，PROFILER_PORT 可覆盖端口）
python web.py --stop  # 停止后台运行的 web 服务并释放端口（pidfile: data/web_{端口}.pid，防 PID 复用误杀）
```

注意：`run.py`/`quick_test.py` 分析完毕会自动启动 web.py 并打开报告页（`config.py` 中 `WEB_AUTOSTART=False` 关闭；批量模式不自动启动）。

首次运行会提示用 B站 APP 扫码登录；Cookie 自动保存到 `data/cookie.json` 并复用。

## 代码结构

入口 `run.py` 将 `src/` 加入 `sys.path` 后调用 `main.main()`。模块间以**扁平的非包方式导入**（`from config import ...`，不带 `src.` 前缀），修改时务必保持这一约定。

```
web.py               # 交互式 Web 报告服务（Flask，首页视频列表 + 五标签页报告 + 弹幕 JSON API + 手动分析/删除/重新生成 job API（job 原子注册互斥防并发重入）；非 GET 请求强制校验 Origin/Host 为本机回环否则 403（防 CSRF/DNS rebinding）；Cookie 失效后可经 /api/reload_client 热复位；CSS/JS 在 static/，只留路由与数据装配）
static/              # Web 报告静态资源（report.css/report.js/index.css/index.js + 本地化的 chart.umd.min.js/wordcloud2.min.js，Flask 默认伺服 /static/；数据经内联 window.__DATA__ 注入）
src/
├── main.py              # 主控流程：登录→弹幕(实时+历史)→刷屏检测→问题弹幕检测→评论→兴趣分UID解析（+问题评论作者直引 select_problem_comment_authors）→用户采集→画像分析→LLM深掘→报告
├── web_autostart.py     # 分析完毕自动启动 web.py 并打开报告页（maybe_launch_web，WEB_AUTOSTART 开关）
├── config.py            # 全部配置常量：API 端点、限速/重试、采集翻页上限、LLM 配置（含 API Key，已被 .gitignore 排除）
├── api_client.py        # BiliAPIClient：HTTP 封装（线程安全限速（区间内随机：基础0.8–1.6s/高风险2–4s）、自适应降速（触发风控×1.5、成功缓慢回落）、重试退避、-412及重签无效的-352/-403风控全局冷却（仅 WBI 端点走重签）、Cookie、WBI签名（密钥获取失败 60s 负缓存）、bili_ticket/buvid3（失败 300s 后可重试）；post() 与 get() 同风控语义）
├── clash_ctl.py         # Clash/mihomo 控制器封装（列节点/切节点换出口 IP，失败静默降级）
├── proxy_core.py        # 内置 mihomo 核心生命周期（SUB_URLS 多订阅→127.0.0.1 随机端口代理+控制器，零安装；自动下载锁定版本且经官方 SHA256 校验不匹配拒绝执行、github.com 直连优先；PDEATHSIG 防孤儿驻留，stop 时清理含凭证 config.yaml）
├── combo_pool.py        # 账号×IP 组合池（鸭子类型模拟 BiliAPIClient；风控换"新号+新IP"重试，冷却按截止时刻锁外等待（单账号池冷却缩至 SINGLE_ACCOUNT_RISK_COOLDOWN=120s），IP 池故障摘代理降级直连、每 PROXY_RETRY_AFTER=600s 重探恢复；注意内置核心为单 mixed-port 单 select 组，IP 维度全局单点：所有账号共享同一出口 IP）
├── auth.py              # 扫码登录、Cookie 保存/加载/校验/自动刷新、小号池发现（data/cookies/*.json → load_extra_clients，失效自动尝试刷新）
├── danmaku.py           # 实时弹幕 XML 解析，按 mid_hash 聚合发送者
├── danmaku_history.py   # 历史弹幕采集（逐日弹幕池快照，protobuf wire 手写解析+0x0A 特征校验防错误页误判；失败日记账 failed_dates 优先补采、截断写 truncated、done=1 后重跑滚动补采最近 3 天）
├── comment.py           # 评论区采集（wbi/main 游标 + 子评论补采 + IP属地），建立 UID→CRC32 映射
├── uid_resolver.py      # mid_hash 破解：评论/充电名单/互动弹幕/视频元信息（main.build_video_meta_uid_map）/全局库交叉验证 + MITM 反查碰撞消歧
├── crc_rainbow.py       # MITM 中间相遇 CRC32 反查（10万条内存小表，覆盖全部 ≤10 位 UID；惰性建表加双检锁线程安全，预计算 adv5 表，约 49ms/hash）
├── spam_detector.py     # 刷屏检测：只标记风险等级（高/中/低），绝不删除弹幕数据
├── cringe_detector.py   # LLM 问题弹幕检测（八类判定+发送者聚合+llm_cache缓存，key 带口径版本号 v4，批次响应先解析校验成功才落缓存）+ 问题评论检测（同口径，按内容缓存判定、回映 rpid 回写 comments.problem，未配置 LLM_API_KEY 自动跳过；致命 4xx 直接上抛由 phase 层降级，瞬态错误同厂商短退避，整轮重试 LLM_RETRY_BUDGET_SECONDS=1800s 熔断）
├── user_collector.py    # 四维度用户数据采集（主页/动态/关注/收藏等；采集全程走账号×IP 组合池（combo_pool，鸭子类型透明接管：多号并行分片（每账号一子池、限速按号独立、线程↔分片绑定、吞吐≈账号数倍）、风控换号+切节点重试，冷却锁外等待，IP 池故障自动降级直连并定时重探恢复））
├── profile_analyzer.py  # 规则式画像分析与标签生成
├── llm_analyzer.py      # LLMAnalyzer：重点深掘（兴趣分 top K 单人单调用+llm_cache缓存，OpenAI client 初始化时复用、单次调用超时 LLM_DEEP_TIMEOUT=120s；全员粗筛已砍，未配置 Key 自动跳过）
├── up_analyzer.py       # UP 主相关分析
├── report.py            # 报告渲染函数库（用户卡片/问题弹幕榜/图表统计/基础CSS，被 web.py 复用）
├── exporter.py          # CSV/JSON 数据导出（report_{BV号}_{时间} 前缀，Web 报告页提供下载链接）
└── storage.py           # SQLite 持久化（data/profiler.db，get_db 统一 WAL + busy_timeout=10000 + synchronous=NORMAL），支撑断点续采与 LLM 结果缓存（llm_cache 表、danmaku 全量弹幕表（含 mode/color/pool/dmid/page 属性列）、comments 评论表（含 uname 昵称、parent_rpid 回复树、problem 问题标注、location IP属地）、false_positive 误报标记表（kind: dm=弹幕内容/cmt=评论rpid，展示层扣除聚合）、phase_state 阶段检查点表（弹幕历史 last_date/fetched_dates/failed_dates/truncated、评论游标，中断续采））
```

数据流：`run.py` → `main.run_analysis(bvid, force, max_users)`，全阶段断点续采（任意位置 Ctrl+C/崩溃后重跑可续）：弹幕逐日增量落库+phase_state 检查点（danmaku 表 last_date/fetched_dates/failed_dates/truncated/done，历史快照中断后逐日续采、失败日记账优先补采；月份/日期降序遍历、上限耗尽保留最新日期；done=1 后重跑仍滚动补采最近 HISTORY_RECENT_REFRESH_DAYS=3 天，dmid 幂等去重；仅时间窗完整无失败才写 done）、评论逐页落库+游标检查点（comments 表 mode/page/offset/done/truncated，wbi/legacy 双路径续页，模式切换页码归零、截断不写 done）、LLM 判定批次级缓存（llm_cache 批次 key=稳定前缀`cringe:{bvid}:版本`/`cmt:{bvid}:版本`@batch:模型:内容指纹，先解析校验成功才落缓存、坏响应进重试链，已完成批次重跑零调用）、senders 逐条落库、users 每人落库；非 --force 重跑时各阶段按库内数据存在性独立跳过（完整判据：done=1 或本功能前的旧版完整落库），`--force` 清除该视频全部缓存与检查点强制重采（llm_cache 中仅清该视频的问题弹幕/问题评论判定缓存 `cringe:{bvid}:*` 与 `cmt:{bvid}:*`，深掘缓存 `deep:{uid}:*` 跨视频复用、保留）。全局映射库 `global_uid_map` 跨视频沉淀可靠 mid_hash→UID 映射（仅评论/充电/互动/元信息等明文来源可沉淀，纯 CRC32 破解结果不沉淀防误归因跨视频放大；冲突覆盖按来源优先级，明文>破解），解析率随使用次数累积提升。

## 开发约定

- **限速是硬约束**：B站 API 有风控，`config.py` 中 `REQUEST_DELAY` / `REQUEST_DELAY_LONG` 为区间值（基础 0.8–1.6s、高风险 2–4s，区间内随机取时长以消除固定节奏特征），触发风控（-412/HTTP412/重签无效的-352/-403）时自适应倍率 ×1.5 上调（上限 5.0，由 `ADAPTIVE_THROTTLE_*` 控制）、业务成功后缓慢回落，重试最多 3 次指数退避。新增 API 调用必须走 `BiliAPIClient`，不要绕过限速直接发请求。
- **失败要降级而非中断**：例如评论采集失败时回退为仅用 CRC32 破解；LLM 分析失败只打印警告。单个用户采集异常不得中断整体流水线。
- **不删除数据**：刷屏检测只标记 `spam_level`，不删除任何弹幕。
- 采集规模由 `config.py` 中的 `MAX_*` 常量控制（评论 100 页、子评论补采 25 页/条、动态定员上限 ANALYZE_USERS_FLOOR=300 / ANALYZE_USERS_RATIO=0.05 / MAX_ANALYZE_USERS_HARD_CAP=1000（保底/按比例上浮/绝对封顶）、深掘 LLM_DEEP_TOP_K=20、问题弹幕/问题评论判定并发上限 LLM_CONCURRENCY=16（实际路数=min(批次数,上限)，429 限速自动退避重试）、问题弹幕批大小 CRINGE_BATCH_SIZE=200、问题评论批次/上限 COMMENT_CRINGE_BATCH_SIZE=100 / COMMENT_CRINGE_MAX_ITEMS=2000、问题评论作者直引阈值 COMMENT_AUTHOR_MIN_SEVERITY=2 / COMMENT_AUTHOR_MIN_HITS=2、问题评论榜 COMMENT_HEAT_REPLY_WEIGHT=10 / PROBLEM_COMMENT_TOP_N=30、争执焦点 ATTACK_FOCUS_TOP_N=5 / ATTACK_FOCUS_MAX_N=20（保底/封顶，按攻击边数浮动）、密度时间轴桶数 DENSITY_BUCKETS=60、跨视频面板 CROSS_VIDEO_MIN_VIDEOS=2 / CROSS_VIDEO_MAX_USERS=50、LLM 深掘超时 LLM_DEEP_TIMEOUT=120、LLM 判定整轮重试熔断 LLM_RETRY_BUDGET_SECONDS=1800 / 瞬态重试 LLM_TRANSIENT_RETRIES=2、历史弹幕滚动补采 HISTORY_RECENT_REFRESH_DAYS=3、代理恢复重探 PROXY_RETRY_AFTER=600、单账号池冷却 SINGLE_ACCOUNT_RISK_COOLDOWN=120、WBI 密钥负缓存 WBI_KEY_FAIL_TTL=60、buvid3/bili_ticket 重试 CRED_FAIL_TTL=300、回复树深度 REPLY_TREE_MAX_DEPTH=50、web job 淘汰 WEB_JOB_MAX_KEPT=100、手动分析上限 ANALYZE_MAX_TARGETS=200、刷屏检测 SPAM_BURST_*/SPAM_VARIANT_* 窗口阈值等），调优时改这里而不是散落在代码里的数字。
- 输出文件：`data/reports/report_{BV号}_{时间}.csv/.json`（CSV/JSON 导出，web.py 报告页提供下载链接）、`data/profiler.db`（数据库）、`data/cookie.json`（登录态）。

## 测试说明

项目**没有单元测试框架**（无 pytest/unittest 目录）。验证方式是直接运行：

- `python quick_test.py` —— 最小化端到端冒烟测试（只分析刷屏 top N 用户，速度快）；
- 或完整 `python run.py <BV号>` 跑通全流程并用 `python web.py` 检查生成的 Web 报告与控制台各阶段统计。

改动后请至少跑一次 `quick_test.py` 验证。注意运行需要有效 Cookie 和真实网络，且会真实请求 B站 API。

## 安全注意事项

- `src/config.py` 含 LLM API Key 默认值、`data/cookie.json` 与 `data/cookies/`（小号池）含 B站登录凭证，以上连同 `data/profiler.db`、`data/reports/`、`data/qrcode.png` 均已在 `.gitignore` 中排除——**不要把它们提交进仓库或打印到日志**。
- LLM 配置走环境变量覆盖：`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `LLM_MAX_TOKENS`，优先用环境变量而非改 `config.py` 里的硬编码 Key。双厂商：`LLM_PROVIDER=mimo` 切换到小米 MiMo（`MIMO_API_KEY` / `MIMO_BASE_URL` / `MIMO_MODEL`，默认 mimo-v2.5），llm_cache key 含模型名，两厂商缓存互不污染可 A/B 对比；判定批次失败会自动换备用厂商（`LLM_FALLBACK`，另一套）兜底重试，双 key 都配置才启用；仍失败则整轮等待重试（60s×轮次递增、封顶 300s，总耗时超 LLM_RETRY_BUDGET_SECONDS=1800s 熔断放弃剩余批次并告警；认证失败/模型不存在等致命 4xx 直接报错降级，不死循环）。
- Cookie 等于账号登录态，泄露即等于账号被盗，处理 `data/` 目录文件时保持谨慎。
- 机场订阅链接（config.py SUB_URLS / 环境变量）与 data/mihomo_runtime/config.yaml 含凭证，均不入库、不打印（mihomo stop 时自动删除该文件）；内置核心二进制在 data/ 或 vendor/（gitignore），自动下载强制官方 SHA256 校验。注意 config.py 中既有明文 Key 建议尽快轮换并改用环境变量（见 CODE_REVIEW.md 中-23）。
