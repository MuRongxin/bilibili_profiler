# 阶段 4：性能提速 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** CRC32 破解从 ~75 秒/人降到毫秒级；并发采集受控落地；spam 检测与风控冷却的已知性能/稳健性缺口修复。

**上游文档：** 路线图阶段 4；审查报告 Important（破解耗时、spam O(n²)、up_analyzer 并发）；阶段2终审 Important #1（-412 全局冷却）。

**技术选型（控制器已定）：纯标准库实现彩虹表，不引入 numpy。**
- 表文件 `data/crc_table.bin`：定长 8 字节记录 `(crc32:uint32, uid:uint32)` 按 crc 排序，5000 万条 ≈ 400MB。
- 构建：multiprocessing 分片（每片 100 万 uid），片内 `zlib.crc32` 计算后排序写临时文件，`heapq.merge` k 路归并出最终文件（流式、低内存）。预计耗时 1-3 分钟，一次性。
- 查询：`mmap` + 二分查找（约 26 次 8 字节读取），毫秒级；同 crc 多条目（碰撞）返回全部候选 uid。

**测试约定：** 无 pytest，python -c / 临时脚本验证（构建测试用小范围 uid 如 10 万，不构建全量表）；绝不在验证中发真实请求。每任务一个 commit。

---

### Task 1: crc_rainbow.py 彩虹表模块

**Files:**
- Create: `src/crc_rainbow.py`
- Modify: `src/config.py`（新增常量，gitignore 不提交）
- Modify: `.gitignore`（忽略 `data/crc_table.bin` 与临时分片 `data/crc_tmp_*`）

**config.py 新增：**
```python
CRC_TABLE_PATH = os.path.join(DATA_DIR, "crc_table.bin")  # 路径写法对齐现有 DB_PATH
CRC_TABLE_MAX_UID = 50_000_000  # 彩虹表覆盖的UID上限（>10位新UID无法反推，不覆盖）
CRC_BUILD_CHUNK = 1_000_000     # 构建分片大小
```

**模块接口（照此实现）：**
```python
def build_table(max_uid: int = CRC_TABLE_MAX_UID, workers: int = None):
    """构建彩虹表（一次性，带进度打印）。workers 默认 cpu_count()。
    流程：多进程分片计算 zlib.crc32(str(uid).encode()) → 片内排序写 data/crc_tmp_N.bin
    → heapq.merge 归并为 CRC_TABLE_PATH（8字节记录 <II 小端: crc, uid）→ 清理临时文件。"""

def lookup(crc32_hash: str) -> list[int]:
    """查表：输入8位hex字符串，返回所有匹配uid（碰撞时多个）。
    表不存在返回空列表（调用方负责触发构建或降级）。mmap 只读，进程级缓存句柄。"""

def table_exists() -> bool:
    """表文件是否存在且大小符合预期（条目数×8字节）。"""
```

**验证：** 用小 max_uid=100_000 构建临时表 → 断言 `lookup(format(zlib.crc32(b"12345"), "08x")) == [12345]`；构造已知碰撞（Task 7 评论任务曾用中间相遇法找到碰撞对，可用 `zlib.crc32` 直接验证两个 uid 同 crc 的情形）断言返回多个候选；查询耗时 < 10ms；table_exists 对缺失/截断文件返回 False。

**提交：** `feat: CRC32彩虹表构建与毫秒级查询模块`

---

### Task 2: uid_resolver 集成彩虹表

**Files:**
- Modify: `src/uid_resolver.py`（`crack_crc32` 约 60-90、`resolve_sender` 暴力路径）

**Step 1:** `crack_crc32(crc32_hash)` 改为：先 `table_exists()`，存在则 `lookup()` 返回候选列表，逐个 `verify_uid_exists` 取第一个存在的（保持现有函数返回单个 uid 的契约；多个候选都验证一遍，全部存在时取第一个但打印碰撞警告）；表不存在时**自动触发一次性构建**（打印提示与预计耗时），构建失败降级为旧的纯 Python 增量搜索逻辑（保留旧函数改名 `_crack_crc32_fallback`）。

**Step 2:** 保留 `max_search` 参数语义（上限即 CRC_TABLE_MAX_UID）；>10 位 UID 不在表覆盖范围，自然返回未命中（无需特殊处理，docstring 注明）。

**Step 3:** resolve_sender 的暴力路径逻辑不变（置信度压级与 collision_risk 已是阶段1成果），只是 crack 变快。

**验证：** 小表 mock——`lookup` 返回多候选时第一个 verify 失败的会取下一个；表不存在时触发 build（mock build_table 断言被调用）；build 抛异常降级 fallback（mock 一个已知 hash 验证旧逻辑结果一致）。

**提交：** `feat: UID解析接入彩虹表破解速度提升至毫秒级`

---

### Task 3: -412 全局冷却时间戳

**Files:**
- Modify: `src/api_client.py`

**背景（阶段2终审 Important #1）：** 多线程下各自冷却——线程 A 命中 -412 睡 600s 期间线程 B-E 继续向已风控路径发请求。

**实现：**
- `__init__` 加 `self._risk_cooldown_until = 0.0`。
- `get()`/`post()` 的 -412 分支（业务码与 HTTP 412 共两处）：命中时设置 `self._risk_cooldown_until = time.time() + wait`。
- `_sleep_if_needed` 开头：若 `time.time() < self._risk_cooldown_until`，先睡到该时刻（全局生效，所有线程一起等）。打印一次中文冷却提示（避免刷屏：仅当剩余时间 > 1s 时打印）。

**验证：** 设置 `_risk_cooldown_until = now + 2`，两线程同时 get → 都等待至冷却结束（mock time.sleep 记录）；-412 命中后时间戳被设置。

**提交：** `feat: -412风控冷却改为全局共享时间戳`

---

### Task 4: 受控并发采集 + followings 采样上限

**Files:**
- Modify: `src/up_analyzer.py`（`summarize_followings` 约 151）
- Modify: `src/user_collector.py`（调用处约 416-418）
- Modify: `src/config.py`（新增常量，gitignore 不提交）

**背景：** 审查 Critical #7——5 线程共享客户端（阶段2已线程安全化，此隐患已解），且默认 `sample_size=0` 分析全部关注列表（单人最多额外 2000 请求）。调研实证：他人关注列表仅能看前 100，`MAX_FOLLOWING_PAGES=50`（假设 1000 人）是错的。

**Step 1:** config.py 新增：
```python
COLLECT_WORKERS = 3       # 并发采集线程数（客户端已线程安全，限速为全局共享）
MAX_FOLLOWING_PAGES = 5   # 他人关注列表仅能看前100个（5页×20），调研实证
MAX_UP_SAMPLE = 20        # summarize_followings 深度分析的UP主采样上限
```

**Step 2:** up_analyzer.py 的 ThreadPoolExecutor max_workers 改为 `COLLECT_WORKERS`（import from config）；docstring 注明客户端已线程安全、限速全局共享。

**Step 3:** user_collector.py 调用 `summarize_followings` 处传 `sample_size=MAX_UP_SAMPLE`。

**验证：** mock client 断言 max_workers 使用配置值；sample_size=20 时最多分析 20 个 UP 主；导入正常。

**提交：** `feat: 并发采集收敛为受控3线程并限制关注采样上限`

---

### Task 5: spam_detector 相似度去重优化

**Files:**
- Modify: `src/spam_detector.py`（约 36-41 的两两 SequenceMatcher）

**背景：** 两两比较 O(n²)，刷屏用户上千条弹幕会卡数分钟。

**实现：** 先 `Counter(contents)` 去重，只对唯一内容两两比较（唯一内容数通常远小于总数）；相似度得分的聚合语义保持（重复次数已有单独规则评分，相似度规则只关心"内容是否雷同"——用唯一内容比较即可，docstring 注明）。顺手处理 `detect_bot_pattern` 的未使用参数 `video_times`（删除或保留标注——读代码确认调用方后决定，保持调用方同步）。

**验证：** 构造 2000 条弹幕（其中唯一内容 50 条）→ 比较次数从 200 万降到 1225，断言耗时 < 2s 且 spam 判定结果与旧逻辑在同分布数据上一致（小样本对比）。

**提交：** `perf: spam相似度检测先对内容去重再两两比较`

---

## Self-Review 记录

- 执行顺序：1 → 2 → 3 → 4 → 5（Task 2 依赖 Task 1 模块；3/4/5 独立但均顺序执行）。
- Spec 覆盖：路线图阶段 4 全部项 + 阶段2终审 Important #1（Task 3）。">10位 UID 跳过"经分析为自然行为（表不覆盖即未命中），Task 2 docstring 注明。
- 彩虹表构建为运行时一次性自动触发（Task 2 Step 1），无需单独命令；全量构建的真实耗时验证留到阶段末冒烟（可选）。

## 全部任务完成后（控制器执行）

- [ ] `python quick_test.py --top 3` 冒烟（观察解析阶段提速；首次会触发彩虹表全量构建，记录耗时）
- [ ] 整体终审（base=main）→ 合并
