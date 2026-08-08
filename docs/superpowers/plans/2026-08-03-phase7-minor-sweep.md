# 阶段 7：Minor 清扫 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 清理前六个阶段审查累计的 Minor/backlog 项，消除已知小 bug 与死代码，补 fresh clone 可运行的配置模板。

**测试约定：** 无 pytest，python -c / mock 验证；每任务一个 commit。

**Backlog 来源：** 各阶段审查报告的 Minor 项与实现者自报项（已去重合并）。

---

### Task 1: 小 bug 修复包

**Files:** 多个（逐项见下）

1. **report.py 变量遮蔽**（约 :105/:117，阶段1发现）：`generate_user_card` 中 `for name in all_names` / `sign = raw.get(...)` 覆盖外层用户名/签名变量，导致卡片头部显示被最后一个关注对象覆盖。改循环变量名（如 `up_name`/`raw_sign`）。
2. **api_client.py -352/-403 末次 attempt 空转**（约 :166-172）：`attempt < MAX_RETRY - 1` 时才走清缓存+重签恢复分支，末次直接退出循环。
3. **_ensure_buvid3 失败抑制**（api_client.py）：加 `_buvid3_failed` 标志，失败后本进程不再每次 get 重试 spi（对齐 _bili_ticket_ok 模式）。
4. **auth.py poll_qrcode 防御**：`result.get("data", {})` 改 `(result.get("data") or {})`。
5. **load_cookie 容错放宽**：`except (json.JSONDecodeError, UnicodeDecodeError)` 且校验加载结果是 dict，否则按损坏处理返回 None。
6. **login.py/login_bg.py update_cookies 前 pop `_refresh_token`**（伪 cookie 不应注入 session，对齐 auth.py 主流程）。
7. **uid_resolver.py 碰撞警告措辞**："取第一个" → "取第一个存在的UID"；删除 `unique_contents` 死代码（约 :191）。
8. **user_collector.py 活跃时段时间zone**：`datetime.fromtimestamp` 活跃时段统计显式用东八区（`timezone(timedelta(hours=8))`），消除部署机器时区依赖；同时把追番硬编码 URL 改用 config 的 `USER_BANGUMI_URL`（若 config 没有则新增，gitignore 不提交）；`:428` "注册时间推断"注释改准确表述。
9. **up_analyzer.py**：删函数内重复 `import time`；`_tokenize` 内 `import re` 提到模块顶；`:114` 失效条件（`not result["last_post"]` 恒真）删除或修正意图。
10. **profile_analyzer.py**：删未使用导入 `SPAM_HIGH_THRESHOLD, SPAM_MEDIUM_THRESHOLD`。

**验证：** 逐项 python -c 断言（遮蔽修复后卡片头部用户名正确、-352 末次不重签、时区无关性等）；`import` 全部模块正常。

**提交：** `fix: Minor清扫之小bug修复包（变量遮蔽/-352末次/失败抑制/时区等）`

---

### Task 2: 死代码与文档口径清理包

**Files:** 多个（逐项见下）

1. **auth.py:15** 删死导入 `import requests`。
2. **report.py:88** 删 dead code `dyn_contents`（计算后从未渲染——确认后删除）。
3. **comment.py:114** 告警前缀 `[评论]` 统一为 `[Comment]`。
4. **main.py argparse** `--force` help 文案更新为准确描述（清除缓存强制重采）。
5. **uid_resolver.py 碰撞假阳性口径统一**（阶段4终审 Minor #3）：彩虹表路径候选全不存在返回 None 时 method 也用 `METHOD_CRC32_COLLISION`（与 fallback 一致）。
6. **.gitignore** 加 `data/crc_table.bin.partial`。
7. **spam_detector.py 规则4 注释说明**：`avg_interval` 跨整天取平均的局限（集中爆发型漏检）加注注释说明，不改逻辑（改逻辑属设计变更，记录即可）。

**验证：** grep 确认删除项无残留引用；import 正常。

**提交：** `refactor: Minor清扫之死代码与文档口径清理包`

---

### Task 3: config.example.py + LLM 模块健壮性

**Files:**
- Create: `config.example.py`（仓库根目录）
- Modify: `src/llm_analyzer.py`、`README.md`

**Step 1:** config.example.py：从工作区 `src/config.py` 生成脱敏模板（**逐行核对不含任何真实 Key/凭证**；LLM_API_KEY 留空），放仓库根目录，README 安装节加一句"复制 config.example.py 为 src/config.py 后按需修改"（同时 README 现有"配置"描述对齐）。这解决 fresh clone 缺常量直接 ImportError 的问题（阶段4/5/6 新增了多个常量）。
**注意：** `.gitignore` 排除的是 `src/config.py`，根目录 `config.example.py` 不被排除，可以提交。

**Step 2:** llm_analyzer.py 健壮性（审查 Important 项，当前无 Key 未启用但代码应正确）：
- `:120` `max_completion_tokens` 改 `max_tokens`（兼容多数 OpenAI 兼容端点）
- `:126` `raw_text = response.choices[0].message.content or ""`（None 防御）
- `:134-144` 批次循环加 try/except：单批失败打印警告 continue（不再丢弃全部已完成批次）
- `:51` 弹幕原文截断（如前 30 条/人，防超上下文）
- `:91-106` 解析结果为空时打印原始响应前 200 字符便于排查
- `:21` 删冗余 `or os.environ.get(...)`（config 已含 env 读取）

**验证：** mock OpenAI 客户端断言各路径；`python -c` 验证 config.example.py 可被复制为 src/config.py 后 import config 成功（在临时目录模拟，**不动真实 src/config.py**）。

**提交：** `feat: 配置模板config.example.py与LLM模块健壮性修复`

---

## Self-Review 记录

- 执行顺序：1 → 2 → 3（Task 3 独立）。
- 刻意不做：spam_detector 规则4 逻辑变更（设计变更，仅注释）；uid_resolver 双重 verify 优化（契约改动风险收益比不划算，已两次审查结论一致）；历史弹幕缓存（新需求，超出清扫范畴——如需应单独立项）。
- LLM 修复因无真实 Key 无法端到端验证，mock 覆盖。

## 全部任务完成后（控制器执行）

- [ ] `python quick_test.py --top 2` 冒烟
- [ ] 整体终审（base=main）→ 合并
- [ ] 全升级计划完成，更新审查报告文档状态
