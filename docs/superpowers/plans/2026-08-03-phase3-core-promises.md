# 阶段 3：核心承诺修复（--force / 断点续采 / 存储）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 兑现文档宣称的 `--force` 强制重采与断点续采能力；清理存储层死代码与资源泄漏。

**上游文档：** 路线图阶段 3；审查报告 Critical #2/#3、Important（max-users 透传、spam 脏数据、uid=None 永不重试、progress 死代码、aid=0 空跑）。

**测试约定：** 无 pytest，用 `PYTHONPATH=src .venv/bin/python -c` 内联断言（可操作临时 SQLite 文件，**绝不动真实 `data/profiler.db`**——测试时 monkeypatch `storage.DB_PATH` 到临时路径）。每任务一个 commit。

**关键背景（来自审查报告，行号以 main 分支当前代码为准，可能有少量漂移）：**
- `src/main.py`：`run_analysis(bvid, force, max_users)` 约 247-300；`force=True` 目前只跳过 load_progress 提示打印；`phase_resolve` 约 64 行 `load_senders(bvid)` 命中缓存；spam 检测后 `save_sender` 写的是初始 spam_level="低"/score=0.0（约 98-99）且从不回写；`phase_collect_users(resolved, client)` 约 277 行漏传 max_users；阶段5（约 163-186）采集结果只存内存，阶段6（约 209-218）才 save_user_data；uid=None 的失败 sender 被 load_senders 缓存永久跳过（约 71）；`aid=0` 仍发起评论采集（约 259-262）。
- `src/storage.py`：`clear_progress`（约 242）存在但无人调用；各函数连接无 try/finally；progress 表只在流程末尾写一次"完成"，加载的进度不参与任何跳过逻辑（死代码）。
- 决策（路线图已定）：删除 progress 表读写逻辑，senders/users 缓存作为唯一续采机制，README 注明。

---

### Task 1: storage.py 清理（clear_video_cache + 连接管理 + 删 progress 死代码）

**Files:**
- Modify: `src/storage.py`

**Step 1:** 读 storage.py 全文。新增：

```python
def clear_video_cache(bvid: str):
    """清除指定视频的全部缓存（senders/users/progress），供 --force 强制重采"""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM senders WHERE bvid=?", (bvid,))
        conn.execute("DELETE FROM user_data WHERE bvid=?", (bvid,))  # 表名/列名以实际为准
        conn.commit()
```

（表结构以实际代码为准；若 user_data 表无主键关联 bvid 则调整——读代码确认。）

**Step 2:** 所有函数的数据库连接改 `with closing(sqlite3.connect(DB_PATH)) as conn:`（`from contextlib import closing`），确保异常时连接关闭。行为不变。

**Step 3:** 删除 progress 死代码：`save_progress`/`load_progress`/`clear_progress` 函数与 progress 表建表语句删除（已存在的库文件中的表不管，代码不再引用）。grep 确认 `main.py` 中对这三个函数的调用——main.py 侧的清理由 Task 2 完成，本 Task 先只删 storage 侧会导致 main.py 暂时 import 失败，**因此本 Task 需同步删除 main.py 中的 import 和调用点**（load_progress 提示、save_progress 调用），保持仓可运行。两侧同一 commit。

**Step 4:** 验证（临时 DB 路径）：clear_video_cache 后 load_senders/has_user_data 返回空；正常 save/load 回归；`import main` 正常；grep progress 无残留。

**Step 5:** 提交：`refactor: storage连接管理统一closing并删除progress死代码新增clear_video_cache`

---

### Task 2: --force 生效 + --max-users 透传 + aid=0 短路

**Files:**
- Modify: `src/main.py`

**Step 1:** `run_analysis` 中 `init_db()` 后：

```python
if force:
    clear_video_cache(bvid)
    print(f"[Main] --force 已清除 {bvid} 的缓存，全部重新采集")
```

（from storage import 加 clear_video_cache；删除原 force 只跳打印的逻辑——Task 1 已删 load_progress。）

**Step 2:** `phase_collect_users(resolved, client)` 调用处透传 `max_users=max_users`（函数签名加参数，筛选逻辑用传入值替代默认常量）。

**Step 3:** 评论采集处 `aid=0` 短路：`if not aid: print 警告并跳过评论采集`（回退纯 CRC32 破解的既有降级路径自然生效）。

**Step 4:** 验证：mock 或临时 DB——force=True 后 load_senders 为空；`run.py --help` 正常；`import main` 正常。端到端 --force 真实验证留到阶段末冒烟。

**Step 5:** 提交：`fix: --force真正清除缓存强制重采并透传max-users`

---

### Task 3: 真断点续采（阶段5立即落库 + seen_uids 去重）

**Files:**
- Modify: `src/main.py`（阶段5循环，约 163-186）
- 可能 Modify: `src/storage.py`（若 save_user_data 签名不支持只存 data 需小改）

**Step 1:** 阶段5 `collect_user_data` 成功后**立即**写库。当前 `save_user_data` 在阶段6才调用（带 profile）。方案：阶段5采集成功即调 `save_user_data(bvid, uid, data, profile=None)`（storage 侧 profile 参数允许 None，存空 JSON `{}`，阶段6分析后 UPDATE 覆盖——读 storage.py 确认现有 SQL 是 INSERT OR REPLACE 还是分开，按需最小改动）。

**Step 2:** 阶段5循环前加 `seen_uids = set()` 去重：同一 uid 被多个 mid_hash 命中时只采集一次，后续直接复用（内存 map 或刚落库的缓存）。

**Step 3:** 验证（临时 DB + mock collect_user_data）：模拟跑 3 个用户后中断，检查 DB 中已有 3 条 user_data；重跑时这 3 个命中缓存跳过；同一 uid 两个 mid_hash 只采集一次。

**Step 4:** 提交：`fix: 阶段5采集结果立即落库实现真断点续采`

---

### Task 4: spam 结果回写 + uid=None 失败重试

**Files:**
- Modify: `src/main.py`、`src/storage.py`

**Step 1:** storage.py 新增 `update_sender_spam(bvid, mid_hash, spam_level, spam_score)`（UPDATE senders 表）；main.py 阶段4.5 刷屏检测后批量回写所有 sender 的真实检测结果（修复"库中永远是初始脏值"）。

**Step 2:** phase_resolve 的缓存命中逻辑调整：`uid IS NULL` 的缓存记录不再视为"已解析"——纳入重试（评论交叉验证随时间变好）。注意迭代效率：打印重试数量。

**Step 3:** 验证（临时 DB）：写入 spam 初始值的 sender，回写后读出真实值；uid=None 的缓存 sender 在重跑时进入重试列表。

**Step 4:** 提交：`fix: spam检测结果回写数据库与解析失败sender支持重试`

---

### Task 5: phase_analyze 逐人容错 + play 强转 int + 空弹幕提前退出

**Files:**
- Modify: `src/main.py`（phase_analyze 循环约 209-218、phase_danmaku 后）
- Modify: `src/user_collector.py`（play 等数值字段采集处）

**Step 1:** phase_analyze 循环内逐人 try/except：单用户 analyze_profile 异常打印中文警告并跳过该用户（对齐阶段5粒度），不中断整个阶段。

**Step 2:** user_collector.py 采集投稿数据处，`play`/`view` 等数值字段强转 int（B站部分场景返回 `"--"` 字符串）：`int(v.get("play") or 0) if str(v.get("play", "")).isdigit() else 0` 或等价防御（读实际代码选最小改动）。

**Step 3:** phase_danmaku 后加提前退出：`if not danmaku_list: print("[Main] 弹幕为空，终止分析"); return`（避免 0 弹幕白跑全流程产出空报告——阶段2终审建议）。

**Step 4:** 验证：mock analyze_profile 抛异常 → 其他用户正常完成；`"--"` play 值 → 不抛 TypeError。

**Step 5:** 提交：`fix: 画像分析逐人容错与数值字段类型防御`

---

### Task 6: README 更新续采机制说明

**Files:**
- Modify: `README.md`

**Step 1:** 找到 README 中描述缓存/续采的章节（若无则在用法章节后新增小节），说明：
- 断点续采机制：senders/user_data 存于 `data/profiler.db`，中断后重跑自动跳过已完成的解析与采集
- `--force` 清除该视频全部缓存重新采集
- 不再使用 progress 表

**Step 2:** 提交：`docs: README补充断点续采与--force机制说明`

---

## Self-Review 记录

- 执行顺序：1 → 2 → 3 → 4 → 5 → 6（Task 1 提供 clear_video_cache 给 Task 2；Task 1 同步处理 main.py 的 progress 引用保持仓可运行）。
- Spec 覆盖：路线图阶段 3 全部项 + 阶段2终审的空弹幕提前退出建议（Task 5 Step 3）。
- 跨任务冲突：均改 main.py，顺序执行。

## 全部任务完成后（控制器执行）

- [ ] `python quick_test.py --top 3` 冒烟 + 一次 `run.py --force` 小规模真实验证（可选，视耗时）
- [ ] 整体终审（base=main）→ 合并
