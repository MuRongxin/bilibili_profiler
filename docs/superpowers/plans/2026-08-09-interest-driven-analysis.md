# 兴趣驱动的弹幕发送者分析 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 刷屏检测提前驱动选人、LLM 尬语检测、阈值制动态定员、评论贯通展示、投稿超链接、AI 画像分层（批量粗筛+重点深掘）。

**Architecture:** 依据 spec `docs/superpowers/specs/2026-08-09-interest-driven-analysis-design.md`。流程重排为：弹幕→刷屏检测(本地)→尬语检测(LLM,可降级)→评论→兴趣分解析(批量预验证)→采集→画像(注入评论/尬语)→LLM粗筛+深掘→报告(尬语榜/评论小节/超链接)。项目无 pytest，验证用 `PYTHONPATH=src .venv/bin/python` 内联脚本 + 端到端实跑。

**Tech Stack:** Python 3 标准库 + openai 客户端（DeepSeek）、现有 BiliAPIClient/SQLite 缓存。

**关键约定（每个任务都必须遵守）：**
- 限速是硬约束，新增 API/LLM 调用走现有封装，不绕过；
- 失败降级不中断：LLM/评论失败只警告；
- 不删除数据：刷屏/尬语只标记；
- 新常量同步写 `src/config.py`（gitignored，含真实 Key，绝不打印其内容）和 `config.example.py`（入库模板）；
- 代码注释/打印全中文；项目不得出现 "anthropic" 字样；
- 模块扁平导入（`from config import ...`，不带 `src.` 前缀）。

---

### Task 0: 提交工作区已验证的修复（前置）

工作区有本轮已端到端验证但尚未提交的修复（行缓冲+进度输出、UID 批量预验证三态化、WBI 风控二次失败全局冷却、AGENTS.md 同步）。先提交，使后续任务 diff 干净。

**Files:**
- Modify: `run.py`, `quick_test.py`, `login.py`, `login_bg.py`, `src/api_client.py`, `src/comment.py`, `src/danmaku_history.py`, `src/uid_resolver.py`, `src/user_collector.py`, `src/llm_analyzer.py`, `AGENTS.md`（均为已改动的现有文件，仅提交，不再编辑）

- [ ] **Step 1: 确认工作区状态符合预期**

Run: `git status --short`
Expected: 恰好上述 11 个文件为 `M`，无其他改动（`src/config.py` 因 gitignore 不出现）。

- [ ] **Step 2: 提交**

```bash
git add run.py quick_test.py login.py login_bg.py src/api_client.py src/comment.py src/danmaku_history.py src/uid_resolver.py src/user_collector.py src/llm_analyzer.py AGENTS.md
git commit -m "fix: 输出实时化（行缓冲+全程进度）+ UID批量预验证三态化 + WBI风控二次失败全局冷却"
```

Expected: 提交成功；`git status --short` 干净。

---

### Task 1: config 常量与 MAX_ANALYZE_USERS 清理

**Files:**
- Modify: `src/config.py:82`
- Modify: `config.example.py`（找到对应 `MAX_ANALYZE_USERS` 行，同样替换）
- Modify: `src/main.py:13`（import 行）、`src/main.py:551-552`（argparse）

- [ ] **Step 1: config.py 替换常量**

`src/config.py` 第 82 行：

```python
MAX_ANALYZE_USERS = 100            # 最大深度分析用户数
```

替换为：

```python
MAX_ANALYZE_USERS_HARD_CAP = 300   # 动态定员安全上限（兴趣命中者超过时按兴趣分截断）
LLM_DEEP_TOP_K = 20                # LLM 重点深掘人数（兴趣分 top K 单人单调用）
CRINGE_BATCH_SIZE = 200            # 尬语检测每批弹幕条数
```

`config.example.py` 中找到 `MAX_ANALYZE_USERS` 行做同样替换（示例文件不含 Key，直接照抄上面三行）。

- [ ] **Step 2: main.py import 行更新**

`src/main.py:13`：

```python
from config import MAX_ANALYZE_USERS, LLM_API_KEY, HISTORY_DANMAKU_ENABLED
```

改为：

```python
from config import MAX_ANALYZE_USERS_HARD_CAP, LLM_API_KEY, HISTORY_DANMAKU_ENABLED
```

- [ ] **Step 3: argparse --max-users 改为手动覆盖（默认 None=阈值制）**

`src/main.py:551-552`：

```python
    parser.add_argument("--max-users", type=int, default=MAX_ANALYZE_USERS,
                        help=f"最大分析用户数 (默认 {MAX_ANALYZE_USERS})")
```

改为：

```python
    parser.add_argument("--max-users", type=int, default=None,
                        help="手动硬上限覆盖阈值制动态定员 (默认不限制，阈值命中者全进)")
```

- [ ] **Step 4: 验证无残留引用（phase_resolve/phase_collect_users/run_analysis 的默认值在 Task 3 处理）**

Run: `PYTHONPATH=src .venv/bin/python -c "import ast; ast.parse(open('src/config.py').read()); print('config 语法OK')"` 及 `grep -rn "MAX_ANALYZE_USERS" src/ quick_test.py | grep -v HARD_CAP`
Expected: 语法 OK；grep 只剩 `src/main.py` 中 phase_resolve/phase_collect_users/run_analysis 三处默认值引用（Task 3 将改），以及 `run_batch` 透传（如有，保留不动）。

- [ ] **Step 5: Commit**

```bash
git add src/config.py config.example.py src/main.py
# 注意：src/config.py 在 .gitignore 中，git add 不会加入它，属预期；实际入库的是 config.example.py 与 main.py
git commit -m "refactor: 废止固定100人上限，新增动态定员/深掘/尬语检测常量"
```

---

### Task 2: 新模块 cringe_detector.py（LLM 尬语检测）

**Files:**
- Create: `src/cringe_detector.py`
- Modify: 无（main.py 接线在 Task 3）

- [ ] **Step 1: 创建模块**

```python
"""
尬语检测（LLM 判定）

对合并去重后的全部弹幕按批喂给 LLM，判定三类尬语：
中二抒情 / 尬夸捧杀 / 引战阴阳（spec 决策，不含"无关自我表演"）。
按发送者聚合输出，驱动兴趣分选人与报告尬语榜。
未配置 LLM_API_KEY 或全部批次失败时返回空 dict（降级不中断）。
"""
import json

from openai import OpenAI

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_MAX_TOKENS, CRINGE_BATCH_SIZE

# 尬语类别（与 spec 一致；prompt 与聚合均引用，勿散落硬编码字符串）
CRINGE_CATEGORIES = ["中二抒情", "尬夸捧杀", "引战阴阳"]


def _dedup_contents(danmaku_list: list[dict]) -> list[dict]:
    """按内容去重弹幕，附出现次数（降低 token 消耗）。返回 [{content, count}]，按次数降序"""
    counts = {}
    for dm in danmaku_list:
        c = (dm.get("content") or "").strip()
        if c:
            counts[c] = counts.get(c, 0) + 1
    items = [{"content": c, "count": n} for c, n in counts.items()]
    items.sort(key=lambda x: x["count"], reverse=True)
    return items


def _build_prompt(batch: list[dict], start_idx: int, video_title: str) -> str:
    """构建单批尬语判定 prompt（编号为全局下标，便于跨批映射）"""
    lines = [f'{start_idx + i}. {it["content"]}（出现{it["count"]}次）' for i, it in enumerate(batch)]
    return f"""你是中文互联网内容审核专家。以下是B站视频《{video_title}》的弹幕列表（已按内容去重）。
请逐条判定是否属于以下三类"尬语"之一：
- 中二抒情：咯噔文学、疼痛文学、过度深情、自我感动式抒情
- 尬夸捧杀：无脑吹、饭圈式夸张应援、明显违心的吹捧
- 引战阴阳：拉踩、对线、反串、阴阳怪气等攻击性内容
正常玩梗、合理讨论、普通应援不算尬语，宁漏勿冤。

弹幕列表：
{chr(10).join(lines)}

请严格只输出一个 JSON 数组，每个元素对应一条判定（只输出判为尬语的条目）：
[{{"i": 编号, "category": "中二抒情|尬夸捧杀|引战阴阳", "severity": 1到3的整数, "reason": "10字内理由"}}]
没有尬语就输出 []。不要输出任何 JSON 之外的内容。"""


def _parse_verdicts(raw_text: str) -> list[dict]:
    """从 LLM 响应提取 JSON 数组（容错：截取首个 [ 到末个 ]）"""
    left, right = raw_text.find("["), raw_text.rfind("]")
    if left == -1 or right <= left:
        return []
    try:
        data = json.loads(raw_text[left:right + 1])
    except json.JSONDecodeError:
        return []
    return [v for v in data if isinstance(v, dict) and "i" in v]


def detect_cringe_danmaku(danmaku_list: list[dict], sender_groups: dict[str, dict],
                          video_info: dict) -> dict[str, dict]:
    """
    尬语检测主入口

    Returns:
        {mid_hash: {
            "count": int,            # 该发送者被判尬语的去重内容条数
            "max_severity": int,     # 最高严重度 1-3
            "categories": [str],     # 涉及的尬语类别
            "examples": [{content, category, severity, reason}],  # 至多5条代表原文
        }}
        未配置 Key / 全部批次失败 / 无尬语时为相应子集或空 dict
    """
    if not LLM_API_KEY:
        print("[尬语] 未配置 LLM_API_KEY，跳过尬语检测")
        return {}

    items = _dedup_contents(danmaku_list)
    if not items:
        return {}

    # 内容 -> 发送者集合（同一内容可能被多人发送，各自归属）
    content_senders: dict[str, set] = {}
    for mid_hash, group in sender_groups.items():
        for c in group.get("contents", []):
            content_senders.setdefault((c or "").strip(), set()).add(mid_hash)

    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    title = video_info.get("title", "未知视频")
    verdicts = []
    batches = [items[i:i + CRINGE_BATCH_SIZE] for i in range(0, len(items), CRINGE_BATCH_SIZE)]
    failed = 0
    for bi, batch in enumerate(batches, 1):
        start_idx = (bi - 1) * CRINGE_BATCH_SIZE
        print(f"[尬语] 判定 {bi}/{len(batches)} 批（{len(batch)} 条，LLM请求中）...")
        try:
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": _build_prompt(batch, start_idx, title)}],
                max_tokens=LLM_MAX_TOKENS,
                temperature=0.3,  # 判定类任务低温，减少格式漂移
            )
            raw = resp.choices[0].message.content or ""
        except Exception as e:
            print(f"[尬语] 警告: 批次 {bi} 请求失败（{e}），跳过该批")
            failed += 1
            continue
        batch_verdicts = _parse_verdicts(raw)
        if not batch_verdicts and raw.strip() not in ("", "[]"):
            print(f"[尬语] 警告: 批次 {bi} 响应解析为空，原始响应前200字符: {raw[:200]!r}")
        for v in batch_verdicts:
            idx = v.get("i")
            if isinstance(idx, int) and 0 <= idx < len(items) and v.get("category") in CRINGE_CATEGORIES:
                v["_content"] = items[idx]["content"]
                verdicts.append(v)
        print(f"[尬语] 批次 {bi}: 判出 {len(batch_verdicts)} 条尬语")

    if failed == len(batches):
        print("[尬语] 警告: 全部批次失败，尬语检测降级为空")
        return {}

    # 按发送者聚合
    results: dict[str, dict] = {}
    for v in verdicts:
        content = v["_content"]
        for mid_hash in content_senders.get(content, ()):
            ent = results.setdefault(mid_hash, {"count": 0, "max_severity": 0,
                                                "categories": [], "examples": []})
            ent["count"] += 1
            sev = v.get("severity", 1)
            ent["max_severity"] = max(ent["max_severity"], sev if isinstance(sev, int) else 1)
            cat = v["category"]
            if cat not in ent["categories"]:
                ent["categories"].append(cat)
            if len(ent["examples"]) < 5:
                ent["examples"].append({
                    "content": content, "category": cat,
                    "severity": sev, "reason": v.get("reason", ""),
                })

    print(f"[尬语] 检测完成: {len(verdicts)} 条尬语，涉及 {len(results)} 个发送者")
    return results
```

- [ ] **Step 2: 离线聚焦验证（聚合与解析逻辑，不打 LLM）**

```bash
PYTHONPATH=src .venv/bin/python - <<'EOF'
from cringe_detector import _dedup_contents, _parse_verdicts, CRINGE_CATEGORIES

# 去重
dms = [{"content": " awsl "}, {"content": "awsl"}, {"content": "泪目"}, {"content": ""}]
items = _dedup_contents(dms)
assert items[0]["content"] == "awsl" and items[0]["count"] == 2, items
assert len(items) == 2, items

# 解析容错
assert _parse_verdicts('[{"i": 0, "category": "中二抒情", "severity": 2, "reason": "咯噔"}]')[0]["i"] == 0
assert _parse_verdicts('前面废话[{"i": 1, "category": "引战阴阳"}]尾巴') == [{"i": 1, "category": "引战阴阳"}]
assert _parse_verdicts("不是JSON") == []
assert _parse_verdicts("") == []
print("离线验证通过, 类别:", CRINGE_CATEGORIES)
EOF
```

Expected: `离线验证通过, 类别: ['中二抒情', '尬夸捧杀', '引战阴阳']`

- [ ] **Step 3: 在线小样本验证（真实 LLM，几条弹幕，验证 prompt/解析闭环）**

```bash
PYTHONPATH=src .venv/bin/python - <<'EOF'
from cringe_detector import detect_cringe_danmaku

dms = [{"content": "呜呜呜看完我哭了一整晚，这个世界不值得"}, {"content": "666"},
       {"content": "我家哥哥就是最棒的，不接受反驳"}, {"content": "前排"}]
groups = {"hash_a": {"contents": [d["content"] for d in dms[:2]], "count": 2},
          "hash_b": {"contents": [d["content"] for d in dms[2:]], "count": 2}}
res = detect_cringe_danmaku(dms, groups, {"title": "测试视频"})
print("聚合结果:", res)
# 不断言具体判定（LLM 自由心证），只验证结构
for h, ent in res.items():
    assert ent["count"] >= 1 and ent["max_severity"] >= 1 and ent["examples"], (h, ent)
    assert all(c in ("中二抒情", "尬夸捧杀", "引战阴阳") for c in ent["categories"])
print("在线验证通过")
EOF
```

Expected: 打印若干批次日志与 `在线验证通过`（聚合非空与否取决于 LLM，但结构必须合法；空 dict 也算通过——此时打印提示后人工确认 prompt 合理）。

- [ ] **Step 4: Commit**

```bash
git add src/cringe_detector.py
git commit -m "feat: 新增 LLM 尬语检测模块（三类尬语判定+发送者聚合）"
```

---

### Task 3: main.py 流程重排（刷屏提前 + 尬语接入 + 兴趣分选人）

**Files:**
- Modify: `src/main.py`（phase_spam 打印、`phase_cringe` 新增、phase_resolve 签名与选人、run_analysis 流程、phase_collect_users 去上限）

- [ ] **Step 1: phase_spam 打印改阶段号（逻辑不动）**

`src/main.py:258-270`，函数 docstring 与首行打印改为：

```python
def phase_spam(bvid: str, sender_groups: dict) -> dict:
    """阶段2.5: 刷屏检测（提前到解析前，以 spam_score 驱动兴趣分选人；
    检测完成后将真实结果回写数据库，修正阶段4落库时的占位值）"""
    print("\n[Phase 2.5] 刷屏行为检测...")
```

函数体内两行 `[Phase 4.5]` 打印同样改为 `[Phase 2.5]`（其余逻辑一行不动）。

- [ ] **Step 2: 新增 phase_cringe（放在 phase_spam 之后）**

```python
def phase_cringe(danmaku_list: list, sender_groups: dict, video_info: dict) -> dict:
    """阶段2.6: 尬语检测（LLM，未配置 Key 或失败时返回空 dict 降级）"""
    print("\n[Phase 2.6] 弹幕尬语检测...")
    try:
        return detect_cringe_danmaku(danmaku_list, sender_groups, video_info)
    except Exception as e:
        print(f"[Phase 2.6] 警告: 尬语检测失败（{e}），降级跳过")
        return {}
```

- [ ] **Step 3: import 行加入 cringe_detector**

`src/main.py:22` 附近，在 `from spam_detector import batch_detect_spam` 后加一行：

```python
from cringe_detector import detect_cringe_danmaku
```

- [ ] **Step 4: phase_resolve 改签名与兴趣分选人**

`src/main.py:133-134` 函数签名：

```python
def phase_resolve(bvid: str, sender_groups: dict, comment_uid_map: dict, client, max_users: int = MAX_ANALYZE_USERS, charge_uid_map: dict | None = None, command_uid_map: dict | None = None):
    """阶段4: 解析发送者UID（支持数据库缓存 + 按弹幕数取top N）"""
```

改为：

```python
def phase_resolve(bvid: str, sender_groups: dict, comment_uid_map: dict, client,
                  max_users: int | None = None, charge_uid_map: dict | None = None,
                  command_uid_map: dict | None = None,
                  spam_results: dict | None = None, cringe_results: dict | None = None):
    """阶段4: 解析发送者UID（数据库缓存 + 兴趣分驱动选人）

    选人规则（阈值制动态定员，spec 3）：spam_level∈{高,中} 或 尬语条数≥1 的发送者
    全部进入解析名单；max_users 为 None 时用 MAX_ANALYZE_USERS_HARD_CAP 兜底截断，
    显式传入 max_users（--max-users）时作为手动硬上限优先。
    """
```

第 188-194 行的选人段：

```python
    # 3. 按弹幕数降序排序，只取 top max_users 个
    sorted_unresolved = sorted(unresolved.items(), key=lambda x: x[1]["count"], reverse=True)
    to_resolve = sorted_unresolved[:max_users]

    skipped = len(sorted_unresolved) - len(to_resolve)
    if skipped > 0:
        print(f"[Phase 4] 跳过 {skipped} 个低弹幕发送者（超出 --max-users 限制）")
```

替换为：

```python
    # 3. 兴趣分驱动选人（阈值制：中/高刷屏 或 有尬语 全进；上限兜底/手动覆盖）
    spam_results = spam_results or {}
    cringe_results = cringe_results or {}

    def interest_key(mid_hash: str):
        spam = spam_results.get(mid_hash, {})
        cringe = cringe_results.get(mid_hash, {})
        return (spam.get("spam_score", 0.0), cringe.get("max_severity", 0),
                unresolved[mid_hash]["count"])

    must = [h for h in unresolved
            if spam_results.get(h, {}).get("spam_level") in ("高", "中")
            or cringe_results.get(h, {}).get("count", 0) >= 1]
    must.sort(key=interest_key, reverse=True)

    cap = max_users if max_users is not None else MAX_ANALYZE_USERS_HARD_CAP
    to_resolve_hashes = must[:cap]
    print(f"[Phase 4] 兴趣命中 {len(must)} 人（中/高刷屏或尬语），"
          f"截取 {len(to_resolve_hashes)} 人解析（上限 {cap}）")

    skipped = len(unresolved) - len(to_resolve_hashes)
    if skipped > 0:
        print(f"[Phase 4] 跳过 {skipped} 个低兴趣发送者（未命中刷屏/尬语阈值）")

    to_resolve = [(h, unresolved[h]) for h in to_resolve_hashes]
```

- [ ] **Step 5: phase_collect_users 去掉人数上限（名单已由阶段4定员）**

`src/main.py:273-274` 签名与 docstring：

```python
def phase_collect_users(resolved: dict, client, max_users: int = MAX_ANALYZE_USERS, force: bool = False):
    """阶段5: 深度采集用户数据（成功立即落库可断点续采；force=True 跳过缓存强制重采）"""
```

改为：

```python
def phase_collect_users(resolved: dict, client, force: bool = False):
    """阶段5: 深度采集用户数据（名单已由阶段4兴趣定员，此处不再设限；
    成功立即落库可断点续采；force=True 跳过缓存强制重采）"""
```

第 301 行 `uids_to_collect = deduped[:max_users]` 改为 `uids_to_collect = deduped`；
第 304 行打印 `print(f"[Phase 5] 需采集用户: {total} 人 (上限 {max_users})")` 改为 `print(f"[Phase 5] 需采集用户: {total} 人")`。

- [ ] **Step 6: run_analysis 流程重排**

`src/main.py:403` 签名 `def run_analysis(bvid: str, force: bool = False, max_users: int = MAX_ANALYZE_USERS):` 改为 `def run_analysis(bvid: str, force: bool = False, max_users: int | None = None):`。

第 429-447 行流程段（阶段3→4→4.5→5）：

```python
    # 阶段3: 评论 + 充电名单（comment_location_map 为 uid→IP属地，阶段6贯通进画像）
    comments, comment_uid_map, comment_location_map, charge_uid_map = phase_comment(video_info, client)

    # 阶段4: UID解析
    resolved = phase_resolve(bvid, sender_groups, comment_uid_map, client,
                             max_users=max_users, charge_uid_map=charge_uid_map,
                             command_uid_map=build_command_uid_map(command_dms))

    # 阶段4.5: 刷屏检测
    spam_results = phase_spam(bvid, sender_groups)

    # 合并刷屏数据到resolved
    for mid_hash in resolved:
        if mid_hash in spam_results:
            resolved[mid_hash]["spam_level"] = spam_results[mid_hash]["spam_level"]
            resolved[mid_hash]["spam_score"] = spam_results[mid_hash]["spam_score"]

    # 阶段5: 用户采集
    user_data_map = phase_collect_users(resolved, client, max_users=max_users, force=force)
```

替换为：

```python
    # 阶段2.5: 刷屏检测（本地，提前到解析前驱动选人）
    spam_results = phase_spam(bvid, sender_groups)

    # 阶段2.6: 尬语检测（LLM，可降级）
    cringe_results = phase_cringe(danmaku_list, sender_groups, video_info)

    # 阶段3: 评论 + 充电名单（comment_location_map 为 uid→IP属地，uid_comments 阶段6贯通进画像）
    comments, comment_uid_map, comment_location_map, charge_uid_map = phase_comment(video_info, client)
    # uid → 该用户在本视频的评论（按点赞降序），供阶段6注入画像与阶段7深掘证据包
    uid_comments: dict[int, list] = {}
    for c in comments:
        uid_comments.setdefault(c["uid"], []).append(c)
    for lst in uid_comments.values():
        lst.sort(key=lambda x: x.get("like", 0), reverse=True)

    # 阶段4: UID解析（兴趣分驱动选人）
    resolved = phase_resolve(bvid, sender_groups, comment_uid_map, client,
                             max_users=max_users, charge_uid_map=charge_uid_map,
                             command_uid_map=build_command_uid_map(command_dms),
                             spam_results=spam_results, cringe_results=cringe_results)

    # 合并刷屏/尬语数据到resolved（阶段5置信度过滤与阶段6画像注入均从此处取）
    for mid_hash in resolved:
        if mid_hash in spam_results:
            resolved[mid_hash]["spam_level"] = spam_results[mid_hash]["spam_level"]
            resolved[mid_hash]["spam_score"] = spam_results[mid_hash]["spam_score"]
        if mid_hash in cringe_results:
            resolved[mid_hash]["cringe"] = cringe_results[mid_hash]

    # 阶段5: 用户采集
    user_data_map = phase_collect_users(resolved, client, force=force)
```

`run_batch` 中若有 `max_users` 透传（`run_batch(..., max_users=max_users)` → `run_analysis(..., max_users=...)`），签名无需改（`int | None` 兼容）。

- [ ] **Step 7: 验证**

Run: `.venv/bin/python -m py_compile src/main.py && grep -n "MAX_ANALYZE_USERS" src/main.py | grep -v HARD_CAP`
Expected: 语法 OK；grep 无输出（引用已全部清理为 HARD_CAP 或 None 默认值）。

- [ ] **Step 8: Commit**

```bash
git add src/main.py
git commit -m "feat: 刷屏检测提前+尬语接入流程，兴趣分阈值制驱动UID解析选人"
```

---

### Task 4: 评论贯通（phase_analyze 注入评论与尬语）

**Files:**
- Modify: `src/main.py`（phase_analyze 签名与注入、run_analysis 调用点）

- [ ] **Step 1: phase_analyze 加 uid_comments 参数并注入**

`src/main.py:339-344` 签名段：

```python
def phase_analyze(resolved: dict, spam_results: dict, user_data_map: dict, sender_groups: dict,
                  comment_location_map: dict | None = None):
    """阶段6: 画像分析

    comment_location_map（uid→评论IP属地）在此处注入 user_data 而非依赖落库数据：
    users 表缓存的旧 user_data 没有该字段，每次运行时注入才能保证缓存命中路径也带出属地。
    """
```

改为：

```python
def phase_analyze(resolved: dict, spam_results: dict, user_data_map: dict, sender_groups: dict,
                  comment_location_map: dict | None = None, uid_comments: dict | None = None):
    """阶段6: 画像分析

    comment_location_map（uid→评论IP属地）与 uid_comments（uid→本视频评论）在此处
    注入而非依赖落库数据：users 表缓存的旧 user_data 没有这两个字段，
    每次运行时注入才能保证缓存命中路径也带出属地与评论。
    """
```

函数体开头 `comment_location_map = comment_location_map or {}` 后加一行：

```python
    uid_comments = uid_comments or {}
```

第 371-374 行画像构建段：

```python
            profile = analyze_profile(user_data, danmaku_stats, spam)
            # 碰撞风险标记传入报告，供"可能误识别"徽标展示
            profile["collision_risk"] = info.get("collision_risk", False)
            profiles.append(profile)
```

改为：

```python
            profile = analyze_profile(user_data, danmaku_stats, spam)
            # 碰撞风险标记传入报告，供"可能误识别"徽标展示
            profile["collision_risk"] = info.get("collision_risk", False)
            # 本视频评论（按点赞降序，至多10条）与尬语聚合，供报告展示与 LLM 深掘证据包
            profile["comments"] = uid_comments.get(uid, [])[:10]
            profile["cringe"] = info.get("cringe", {})
            profiles.append(profile)
```

- [ ] **Step 2: run_analysis 调用点传参**

`src/main.py` 中（原第 450 行附近）：

```python
    profiles = phase_analyze(resolved, spam_results, user_data_map, sender_groups, comment_location_map)
```

改为：

```python
    profiles = phase_analyze(resolved, spam_results, user_data_map, sender_groups,
                             comment_location_map, uid_comments)
```

- [ ] **Step 3: 验证**

Run: `.venv/bin/python -m py_compile src/main.py && PYTHONPATH=src .venv/bin/python -c "import main; print('import OK')"`
Expected: 语法 OK 且 `import OK`。

- [ ] **Step 4: Commit**

```bash
git add src/main.py
git commit -m "feat: 阶段6画像注入本视频评论与尬语聚合"
```

---

### Task 5: profile_analyzer 投稿带 bvid + spam_score 透传

**Files:**
- Modify: `src/profile_analyzer.py:232-238`（video_analysis）、`src/profile_analyzer.py:291-298`（danmaku dict）

- [ ] **Step 1: video_analysis 增加 recent（title+bvid）**

`src/profile_analyzer.py:232-238`：

```python
    # 视频分析
    videos = user_data.get("videos", [])
    video_analysis = {
        "count": len(videos),
        "total_play": sum(v.get("play", 0) for v in videos),
        "avg_play": sum(v.get("play", 0) for v in videos) / len(videos) if videos else 0,
        "recent_titles": [v.get("title", "") for v in videos],
    }
```

改为：

```python
    # 视频分析（recent 带 bvid，报告渲染为新标签页超链接）
    videos = user_data.get("videos", [])
    video_analysis = {
        "count": len(videos),
        "total_play": sum(v.get("play", 0) for v in videos),
        "avg_play": sum(v.get("play", 0) for v in videos) / len(videos) if videos else 0,
        "recent_titles": [v.get("title", "") for v in videos],
        "recent": [{"title": v.get("title", ""), "bvid": v.get("bvid", "")} for v in videos[:3]],
    }
```

（`user_collector.get_user_videos` 输出契约已含 bvid/title/play/created，无需改动采集层。）

- [ ] **Step 2: danmaku dict 透传 spam_score（LLM 深掘兴趣排序用）**

`src/profile_analyzer.py:291-298`：

```python
        "danmaku": {
            "count": danmaku_stats.get("count", 0),
            "contents": danmaku_stats.get("contents", []),
            "video_times": danmaku_stats.get("video_times", []),
            "repeat_rate": spam_stats.get("repeat_rate", 0),
            "spam_level": spam_stats.get("spam_level", "低"),
            "spam_reason": spam_stats.get("reason", ""),
        },
```

改为：

```python
        "danmaku": {
            "count": danmaku_stats.get("count", 0),
            "contents": danmaku_stats.get("contents", []),
            "video_times": danmaku_stats.get("video_times", []),
            "repeat_rate": spam_stats.get("repeat_rate", 0),
            "spam_level": spam_stats.get("spam_level", "低"),
            "spam_score": spam_stats.get("spam_score", 0.0),
            "spam_reason": spam_stats.get("reason", ""),
        },
```

- [ ] **Step 3: 离线验证**

```bash
PYTHONPATH=src .venv/bin/python - <<'EOF'
from profile_analyzer import analyze_profile
user_data = {"uid": 1, "level": 5, "videos": [{"title": "t1", "bvid": "BV1xx", "play": 10, "created": 1}],
             "followings": [], "favorite_folders": [], "dynamics": [], "bangumi": [], "dramas": []}
p = analyze_profile(user_data, {"count": 1, "contents": ["a"], "video_times": [0.0]},
                    {"spam_level": "高", "spam_score": 0.85, "reason": "r"})
assert p["video"]["recent"] == [{"title": "t1", "bvid": "BV1xx"}], p["video"]
assert p["danmaku"]["spam_score"] == 0.85, p["danmaku"]
assert p["video"]["recent_titles"] == ["t1"]  # 旧字段保留
print("离线验证通过")
EOF
```

Expected: `离线验证通过`

- [ ] **Step 4: Commit**

```bash
git add src/profile_analyzer.py
git commit -m "feat: 画像投稿带bvid（报告超链接）+ danmaku透传spam_score"
```

---

### Task 6: llm_analyzer 分层重构（粗筛简化 + 重点深掘）与 main.py 接线

**Files:**
- Modify: `src/llm_analyzer.py:13`（import）、`src/llm_analyzer.py:25-88`（_build_prompt 粗筛化）、`src/llm_analyzer.py`（新增深掘方法）
- Modify: `src/main.py:385-400`（phase_ai_analysis）、`src/main.py:452-461`（run_analysis 调用点）

- [ ] **Step 1: import 行加 LLM_DEEP_TOP_K**

`src/llm_analyzer.py:13`：

```python
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_MAX_TOKENS
```

改为：

```python
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_MAX_TOKENS, LLM_DEEP_TOP_K
```

- [ ] **Step 2: users_data 增加评论与尬语字段（粗筛也能看到证据）**

`src/llm_analyzer.py` `_build_prompt` 中 users_data 字典（第 49-52 行附近），在 `"danmaku_count"` 两行后追加两个字段：

```python
                "danmaku_count": dm.get("count", 0),
                "danmaku_contents": dm.get("contents", [])[:30],  # 只取前30条，防止刷屏用户prompt超长
                "spam_level": dm.get("spam_level", "低"),
                "comments": [c.get("content", "")[:50] for c in p.get("comments", [])[:5]],
                "cringe_categories": p.get("cringe", {}).get("categories", []),
                "tags": p.get("tags", []),
```

- [ ] **Step 3: _build_prompt 改为粗筛定位（标签+一句话定性）**

`src/llm_analyzer.py` 第 55-87 行整个 prompt f-string：

```python
        prompt = f"""你是一位专业的网络行为分析师和人格心理学家。请对以下每位B站用户进行**个体深度画像分析**。

这些用户都曾在视频《{title}》中发送弹幕。

## 分析维度（每个用户单独分析）

1. **人格类型**：基于MBTI或Big Five框架，结合其弹幕内容、签名、收藏夹命名、关注偏好推断
2. **心理动机**：他/她为什么在这个视频发这些弹幕？想表达什么？
3. **社交需求**：在B站社区中扮演什么角色、寻求什么？
4. **消费偏好**：内容品味、付费意愿、使用习惯
5. **异常评估**：如果刷屏等级为"高"或"中"，分析其心理状态

## 数据说明
- B站Lv.6代表硬核老用户；大会员表示有持续付费意愿
- 关注列表中的UP主类型反映信息食谱和价值观
- following_summary 是该用户关注UP主的分析：分区分布(top_categories)、大UP/小UP比例(big_creators/small_creators)、活跃UP占比(active_ratio)
- 收藏夹命名反映性格特征；标签是系统辅助判断

## 用户数据（共{len(users_data)}人）
{json.dumps(users_data, ensure_ascii=False, indent=2)}

## 输出要求

请严格按以下格式输出，每个用户一个section，**每个用户不超过200字**：

### [uid] 用户名
人格类型: （1-2句）
心理动机: （1-2句）
社交需求: （1-2句）
消费偏好: （1-2句）
异常评估: （1句）

每个用户之间用 `---` 分隔。不要输出群体总结。务必覆盖所有用户，不要遗漏。"""
```

替换为：

```python
        prompt = f"""你是一位网络行为分析师。请对以下每位B站用户做**快速粗筛**（重点人员后续会单独深度分析，这里只需勾画轮廓）。

这些用户都曾在视频《{title}》中发送弹幕。

## 数据说明
- B站Lv.6代表硬核老用户；大会员表示有持续付费意愿
- 关注列表中的UP主类型反映信息食谱和价值观
- following_summary 是该用户关注UP主的分析：分区分布(top_categories)、大UP/小UP比例(big_creators/small_creators)、活跃UP占比(active_ratio)
- 收藏夹命名反映性格特征；标签是系统辅助判断
- comments 是该用户在本视频评论区发表的评论；cringe_categories 是其弹幕被判定为尬语的类别

## 用户数据（共{len(users_data)}人）
{json.dumps(users_data, ensure_ascii=False, indent=2)}

## 输出要求

请严格按以下格式输出，每个用户一个section：

### [uid] 用户名
标签: （3-5个短标签，如 二次元核心/饭圈化表达/理性讨论者/机器人嫌疑）
定性: （一句话行为定性，不超过40字）

每个用户之间用 `---` 分隔。不要输出群体总结。务必覆盖所有用户，不要遗漏。"""
```

（`_parse_per_user` 与 `analyze` 批量循环不需要改，输出仍按 `### [uid]` 分段。）

- [ ] **Step 4: 新增深掘方法（追加在 `analyze` 方法之后）**

```python
    def _build_deep_prompt(self, p: dict, video_info: dict) -> str:
        """重点人员单人深掘 prompt：证据包（弹幕原文/评论/尬语判定/刷屏分析/四维度数据）"""
        title = video_info.get("title", "未知视频")
        dm = p.get("danmaku", {})
        cringe = p.get("cringe", {})
        evidence = {
            "uid": p.get("uid"),
            "name": p.get("name", "未知"),
            "level": p.get("level", 0),
            "sign": p.get("sign", ""),
            "vip": p.get("vip_status", 0) == 1,
            "follower": p.get("follower", 0),
            "archive_count": p.get("archive_count", 0),
            "tags": p.get("tags", []),
            "following_summary": p.get("following_summary", {}),
            "favorite_folders": p.get("favorite", {}).get("names", []),
            "danmaku_count": dm.get("count", 0),
            "danmaku_contents": dm.get("contents", [])[:50],  # 刷屏用户截断防超长
            "spam": {"level": dm.get("spam_level", "低"), "score": dm.get("spam_score", 0.0),
                     "reason": dm.get("spam_reason", "")},
            "cringe": {"count": cringe.get("count", 0),
                       "categories": cringe.get("categories", []),
                       "examples": cringe.get("examples", [])},
            "comments_in_video": [{"content": c.get("content", ""), "like": c.get("like", 0)}
                                  for c in p.get("comments", [])[:10]],
        }
        return f"""你是一位资深网络行为分析师。请对以下这位B站用户做**单人深度行为画像**。
他/她曾在视频《{title}》中发送弹幕，是本视频中值得重点关注的人物（刷屏得分高或存在尬语）。

## 证据包（JSON）
{json.dumps(evidence, ensure_ascii=False, indent=2)}

## 输出要求（严格按以下四节输出，结论必须引用证据包原文作为论据）

**行为定性**: （2-3句：这是个什么样的人，在本视频中扮演什么角色）
**动机分析**: （2-3句：他/她为什么发这些弹幕/评论，想获得什么）
**证据引用**: （列出2-4条最能支撑结论的弹幕或评论原文，并各配一句解读）
**风险等级**: （高/中/低 + 一句理由：对社区氛围的潜在影响）"""

    def _analyze_one_deep(self, p: dict, video_info: dict) -> str:
        """单人深掘调用，返回分析文本"""
        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )
        response = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": self._build_deep_prompt(p, video_info)}],
            max_tokens=self.max_tokens,
            temperature=1.0,
            top_p=0.95,
            stream=False,
        )
        return response.choices[0].message.content or ""

    def analyze_deep(self, profiles: list[dict], video_info: dict,
                     top_k: int = LLM_DEEP_TOP_K) -> dict[int, str]:
        """重点深掘：兴趣分 top K 单人单调用。兴趣分 = (spam_score, 尬语最高严重度, 弹幕数)"""
        def interest(p: dict):
            dm = p.get("danmaku", {})
            return (dm.get("spam_score", 0.0), p.get("cringe", {}).get("max_severity", 0),
                    dm.get("count", 0))

        targets = sorted(profiles, key=interest, reverse=True)[:top_k]
        results = {}
        for i, p in enumerate(targets, 1):
            print(f"  深掘 {i}/{len(targets)}: UID:{p.get('uid')} {p.get('name', '')}"
                  f"（LLM请求中，可能需要数十秒）...")
            try:
                text = self._analyze_one_deep(p, video_info)
                if text.strip():
                    results[p["uid"]] = text
                else:
                    print(f"  警告: UID:{p.get('uid')} 深掘响应为空，跳过")
            except Exception as e:
                # 失败降级：单用户失败不中断整体深掘
                print(f"  警告: UID:{p.get('uid')} 深掘失败（{e}），跳过")
        return results
```

- [ ] **Step 5: main.py phase_ai_analysis 改分层**

`src/main.py:385-400`：

```python
def phase_ai_analysis(video_info: dict, profiles: list[dict]) -> dict | None:
    """阶段7: LLM 深度画像分析"""
    if not LLM_API_KEY:
        print("\n[Phase 7/7] 跳过 (未在 config.py 或环境变量中设置 LLM_API_KEY)")
        return None

    print("\n[Phase 7/7] LLM 逐人画像分析...")
    try:
        analyzer = LLMAnalyzer()
        result = analyzer.analyze(profiles, video_info)
        per_user_count = len(result.get("per_user", {}))
        print(f"[Phase 7] 完成: {per_user_count}/{len(profiles)} 人生成AI画像")
        return result
    except Exception as e:
        print(f"[Phase 7] LLM 分析失败: {e}")
        return None
```

替换为：

```python
def phase_ai_analysis(video_info: dict, profiles: list[dict]):
    """阶段7: LLM 分层画像分析（7a 批量粗筛全员 + 7b 重点深掘 top K，结果直接注入 profile）"""
    if not LLM_API_KEY:
        print("\n[Phase 7] 跳过 (未在 config.py 或环境变量中设置 LLM_API_KEY)")
        return

    try:
        analyzer = LLMAnalyzer()

        print("\n[Phase 7a] LLM 批量粗筛（全员标签+定性）...")
        brief = analyzer.analyze(profiles, video_info)
        per_user = brief.get("per_user", {})
        for p in profiles:
            uid = p.get("uid")
            if uid in per_user:
                p["ai_brief"] = per_user[uid]
        print(f"[Phase 7a] 完成: {len(per_user)}/{len(profiles)} 人生成粗筛画像")

        print("\n[Phase 7b] LLM 重点深掘（兴趣分 top K 单人单调用）...")
        deep = analyzer.analyze_deep(profiles, video_info)
        for p in profiles:
            uid = p.get("uid")
            if uid in deep:
                p["ai_deep"] = deep[uid]
        print(f"[Phase 7b] 完成: {len(deep)} 人生成深度画像")
    except Exception as e:
        print(f"[Phase 7] LLM 分析失败: {e}")
```

- [ ] **Step 6: run_analysis 调用点简化**

`src/main.py`（原第 452-461 行）：

```python
    # 阶段7: LLM AI 逐人画像分析
    ai_analysis = phase_ai_analysis(video_info, profiles)

    # 将逐人AI分析注入profiles
    if ai_analysis:
        per_user = ai_analysis.get("per_user", {})
        for p in profiles:
            uid = p.get("uid")
            if uid in per_user:
                p["ai_analysis"] = per_user[uid]
```

替换为：

```python
    # 阶段7: LLM 分层画像分析（粗筛/深掘结果在 phase 内直接注入 profile）
    phase_ai_analysis(video_info, profiles)
```

- [ ] **Step 7: 离线验证（兴趣排序与 prompt 构建，不打 LLM）**

```bash
PYTHONPATH=src .venv/bin/python - <<'EOF'
from llm_analyzer import LLMAnalyzer

a = LLMAnalyzer()
profiles = [
    {"uid": 1, "name": "低兴趣", "danmaku": {"count": 1, "spam_score": 0.0}, "cringe": {}},
    {"uid": 2, "name": "高刷屏", "danmaku": {"count": 30, "spam_score": 0.85}, "cringe": {}},
    {"uid": 3, "name": "尬语王", "danmaku": {"count": 5, "spam_score": 0.1},
     "cringe": {"count": 3, "max_severity": 3, "categories": ["中二抒情"], "examples": []}},
]
# 兴趣排序：高刷屏(0.85) > 尬语王(0.1,sev3) > 低兴趣
targets = sorted(profiles, key=lambda p: (p["danmaku"].get("spam_score", 0.0),
                 p.get("cringe", {}).get("max_severity", 0), p["danmaku"].get("count", 0)), reverse=True)
assert [t["uid"] for t in targets] == [2, 3, 1]
prompt = a._build_deep_prompt(profiles[2], {"title": "测试"})
assert "证据包" in prompt and "风险等级" in prompt and "中二抒情" in prompt
print("离线验证通过")
EOF
```

Expected: `离线验证通过`

- [ ] **Step 8: Commit**

```bash
git add src/llm_analyzer.py src/main.py
git commit -m "feat: AI画像分层重构（全员粗筛标签化 + topK单人深掘带证据包）"
```

---

### Task 7: report.py（投稿超链接 + 评论小节 + 尬语榜 + 分层AI展示）

**Files:**
- Modify: `src/report.py:83-86`（vid）、`src/report.py:139-152`（AI section）、`src/report.py:184-189`（弹幕行为段）、`src/report.py:203`（投稿段）、`src/report.py:259`（ai_count）、`src/report.py` CSS 区与 charts-grid/filter-bar 之间（尬语榜）

- [ ] **Step 1: 卡片头部数据准备（vid/AI/尬语/评论）**

`src/report.py:83-86`：

```python
    # 视频
    vid = profile.get("video", {})
    vid_count = vid.get("count", 0)
    vid_titles = vid.get("recent_titles", [])[:3]
```

改为：

```python
    # 视频（recent 带 bvid，渲染为新标签页超链接）
    vid = profile.get("video", {})
    vid_count = vid.get("count", 0)
    vid_recent = vid.get("recent", [])[:3]
```

`src/report.py:139-152` AI section 开头两行：

```python
    # AI画像分析
    ai_text = profile.get("ai_analysis", "")
    ai_section = ""
    if ai_text:
```

改为：

```python
    # AI画像分析（深掘优先，粗筛兜底，兼容旧字段）
    ai_deep = profile.get("ai_deep", "")
    ai_text = ai_deep or profile.get("ai_brief", "") or profile.get("ai_analysis", "")
    ai_heading = "🤖 AI 深度画像" if ai_deep else "🤖 AI 粗筛画像"
    ai_section = ""
    if ai_text:
```

同段中 `<h4>🤖 AI 深度画像</h4>` 改为 `<h4>{ai_heading}</h4>`。

紧接着 AI section 代码块之后，加入尬语内联标记与评论小节的构建代码：

```python
    # 尬语内联标记（弹幕行为行尾）
    cringe = profile.get("cringe", {})
    cringe_note = (f'，其中尬语 {cringe["count"]} 条（{"、".join(cringe.get("categories", []))}）'
                   if cringe.get("count") else "")

    # 本视频评论小节（按点赞降序，至多10条，来自阶段6注入）
    comments = profile.get("comments", [])
    cmt_section = ""
    if comments:
        items = []
        for c in comments:
            ts = c.get("ctime", 0)
            date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
            items.append(f"<li>{esc(c.get('content',''))} "
                         f"<span class=\"dm-time\">👍{c.get('like',0)} {date}</span></li>")
        cmt_section = f'''
            <div class="section">
                <h4>💬 TA 在本视频的评论</h4>
                <ol class="dm-list">{"".join(items)}</ol>
            </div>'''
```

- [ ] **Step 2: 弹幕行为段加尬语行尾 + 其后插入评论小节**

`src/report.py:184-189`：

```python
            <div class="section">
                <h4>🎤 弹幕行为 <span class="spam-badge {spam_class}">{esc(spam_level)}风险</span></h4>
                <div class="detail">共发送 {esc(dm_count)} 条弹幕</div>
                <ol class="dm-list">{dm_list}</ol>
                {f'<div class="reason">判定: {esc(spam_reason)}</div>' if spam_reason else ''}
            </div>
```

改为：

```python
            <div class="section">
                <h4>🎤 弹幕行为 <span class="spam-badge {spam_class}">{esc(spam_level)}风险</span></h4>
                <div class="detail">共发送 {esc(dm_count)} 条弹幕{cringe_note}</div>
                <ol class="dm-list">{dm_list}</ol>
                {f'<div class="reason">判定: {esc(spam_reason)}</div>' if spam_reason else ''}
            </div>
            {cmt_section}
```

- [ ] **Step 3: 最近投稿渲染为超链接**

`src/report.py:203`：

```python
            {f'''<div class="section"><h4>🎬 最近投稿</h4><ul class="list">{''.join(f'<li>{esc(t)}</li>' for t in vid_titles)}</ul></div>''' if vid_titles else ''}
```

改为：

```python
            {f'''<div class="section"><h4>🎬 最近投稿</h4><ul class="list">{''.join(f'<li><a href="https://www.bilibili.com/video/{esc(v.get("bvid",""))}" target="_blank" rel="noopener">{esc(v.get("title",""))}</a></li>' for v in vid_recent if v.get("bvid"))}</ul></div>''' if vid_recent else ''}
```

- [ ] **Step 4: ai_count 统计口径**

`src/report.py:259`：

```python
    ai_count = sum(1 for p in profiles if p.get("ai_analysis"))
```

改为：

```python
    ai_count = sum(1 for p in profiles if p.get("ai_deep") or p.get("ai_brief") or p.get("ai_analysis"))
```

- [ ] **Step 5: 尬语榜（数据构建 + CSS + 插入位置）**

`generate_html_report` 中，`up_wc_js = "{" + ",".join(up_wc_entries) + "}"` 行之后、 `html = f'''` 之前，加入：

```python
    # 尬语榜：按发送者聚合（最高严重度、条数降序），无命中时不渲染
    cringe_entries = [p for p in profiles if p.get("cringe", {}).get("count", 0) >= 1]
    cringe_entries.sort(key=lambda p: (p["cringe"].get("max_severity", 0), p["cringe"]["count"]),
                        reverse=True)
    cringe_board_html = ""
    if cringe_entries:
        rows = []
        for p in cringe_entries:
            cr = p["cringe"]
            example = (cr.get("examples") or [{}])[0]
            rows.append(
                f'<tr><td><a href="https://space.bilibili.com/{esc(p.get("uid", 0))}" target="_blank" rel="noopener">{esc(p.get("name", "未知"))}</a></td>'
                f'<td>{esc(cr["count"])}</td>'
                f'<td>{esc("、".join(cr.get("categories", [])))}</td>'
                f'<td>{esc(cr.get("max_severity", 0))}</td>'
                f'<td>{esc(example.get("content", ""))}<br>'
                f'<span class="cringe-reason">{esc(example.get("category", ""))}: {esc(example.get("reason", ""))}</span></td></tr>'
            )
        cringe_board_html = f'''
    <div class="cringe-board">
        <h3>🤡 弹幕尬语榜（{len(cringe_entries)} 人命中）</h3>
        <table>
            <thead><tr><th>用户</th><th>尬语条数</th><th>类别</th><th>最高严重度</th><th>代表原文</th></tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
    </div>'''
```

CSS 区（`.ai-text br` 规则之后）加入：

```python
	/* 尬语榜 */
	.cringe-board {{ background:white; padding:25px; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,0.04); margin-bottom:30px; }}
	.cringe-board h3 {{ font-size:17px; margin-bottom:15px; color:#555; }}
	.cringe-board table {{ width:100%; border-collapse:collapse; font-size:14px; }}
	.cringe-board th, .cringe-board td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #f0f0f0; vertical-align:top; }}
	.cringe-board th {{ color:#999; font-weight:500; }}
	.cringe-board a {{ color:#00a1d6; text-decoration:none; }}
	.cringe-reason {{ font-size:12px; color:#999; }}
```

页面结构中，`{region_chart_html}\n    </div>`（charts-grid 收尾）之后、`<div class="filter-bar">` 之前插入一行：

```python
    {cringe_board_html}
```

- [ ] **Step 6: 离线验证（合成 profile 生成报告，检查三块新内容）**

```bash
PYTHONPATH=src .venv/bin/python - <<'EOF'
from report import generate_html_report

profiles = [{
    "uid": 123, "name": "测试用户", "level": 5, "tags": [], "danmaku": {"count": 2, "contents": ["a","b"], "video_times": [0.0, 1.0], "spam_level": "高", "spam_reason": "r"},
    "video": {"count": 1, "recent": [{"title": "我的投稿", "bvid": "BV1test"}]},
    "comments": [{"content": "我也评论了", "like": 9, "ctime": 1754000000}],
    "cringe": {"count": 2, "max_severity": 3, "categories": ["中二抒情"],
               "examples": [{"content": "哭了一整晚", "category": "中二抒情", "severity": 3, "reason": "咯噔"}]},
    "ai_deep": "**行为定性**: 测试", "ai_brief": "标签: 测试",
}]
html = generate_html_report({"title": "测试视频", "bvid": "BVx", "stat": {}}, profiles)
assert 'href="https://www.bilibili.com/video/BV1test" target="_blank"' in html, "投稿超链接缺失"
assert "TA 在本视频的评论" in html and "我也评论了" in html, "评论小节缺失"
assert "弹幕尬语榜" in html and "哭了一整晚" in html, "尬语榜缺失"
assert "AI 深度画像" in html, "深掘标题缺失"
assert "其中尬语 2 条" in html, "尬语内联标记缺失"
# 无命中时不渲染尬语榜
html2 = generate_html_report({"title": "t", "bvid": "BVy", "stat": {}},
                             [{"uid": 1, "name": "x", "danmaku": {}, "video": {}, "cringe": {}}])
assert "弹幕尬语榜" not in html2, "空尬语不应渲染榜单"
print("离线验证通过")
EOF
```

Expected: `离线验证通过`

- [ ] **Step 7: Commit**

```bash
git add src/report.py
git commit -m "feat: 报告投稿超链接/评论小节/尬语榜/分层AI展示"
```

---

### Task 8: quick_test.py 同步兴趣分与评论注入

**Files:**
- Modify: `quick_test.py:17`（import）、`quick_test.py:53-64`（选人）、`quick_test.py:67-73`（评论）、`quick_test.py:97-101`（画像注入）、`quick_test.py:103-115`（AI）

- [ ] **Step 1: import 行加 detect_cringe_danmaku**

`quick_test.py:17` 附近，在 `from spam_detector import batch_detect_spam` 后加：

```python
from cringe_detector import detect_cringe_danmaku
```

- [ ] **Step 2: 选人加入尬语维度**

`quick_test.py:53-61`：

```python
    # 3. 刷屏检测 → 取 Top N
    print("[3/6] 刷屏检测...")
    spam_results = batch_detect_spam(sender_groups)
    scored = [
        (mid_hash, r["spam_score"], r["spam_level"])
        for mid_hash, r in spam_results.items()
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    targets = scored[:top_n]
```

改为：

```python
    # 3. 刷屏检测 + 尬语检测 → 兴趣分 Top N（对齐主流程兴趣口径）
    print("[3/6] 刷屏检测 + 尬语检测...")
    spam_results = batch_detect_spam(sender_groups)
    cringe_results = detect_cringe_danmaku(danmaku_list, sender_groups, video_info) if LLM_API_KEY else {}
    scored = [
        (mid_hash, r["spam_score"], r["spam_level"])
        for mid_hash, r in spam_results.items()
    ]
    scored.sort(key=lambda x: (x[1], cringe_results.get(x[0], {}).get("max_severity", 0)),
                reverse=True)
    targets = scored[:top_n]
```

- [ ] **Step 3: 评论保留并构建 uid_comments**

`quick_test.py:67-73`：

```python
    # 4. 收集评论
    print("[4/6] 收集评论...")
    try:
        _, comment_uid_map, _ = collect_comment_data(video_info.get("aid", 0), client)
    except Exception as e:
        # 对齐主流程 phase_comment：评论采集失败降级为仅用CRC32破解，只警告不中断
        print(f"   评论采集失败 (将仅用CRC32破解): {e}")
        comment_uid_map = {}
```

改为：

```python
    # 4. 收集评论
    print("[4/6] 收集评论...")
    comments = []
    try:
        comments, comment_uid_map, _ = collect_comment_data(video_info.get("aid", 0), client)
    except Exception as e:
        # 对齐主流程 phase_comment：评论采集失败降级为仅用CRC32破解，只警告不中断
        print(f"   评论采集失败 (将仅用CRC32破解): {e}")
        comment_uid_map = {}
    uid_comments: dict[int, list] = {}
    for c in comments:
        uid_comments.setdefault(c["uid"], []).append(c)
    for lst in uid_comments.values():
        lst.sort(key=lambda x: x.get("like", 0), reverse=True)
```

- [ ] **Step 4: 画像注入 comments/cringe**

`quick_test.py:97-101`：

```python
        dm_stats = {"count": group["count"], "contents": group["contents"], "video_times": group.get("video_times", [])}
        spam = spam_results.get(mid_hash, {})
        profile = analyze_profile(user_data, dm_stats, spam)
        profile["collision_risk"] = collision_risk
        profiles.append(profile)
```

改为：

```python
        dm_stats = {"count": group["count"], "contents": group["contents"], "video_times": group.get("video_times", [])}
        spam = spam_results.get(mid_hash, {})
        profile = analyze_profile(user_data, dm_stats, spam)
        profile["collision_risk"] = collision_risk
        profile["comments"] = uid_comments.get(uid, [])[:10]
        profile["cringe"] = cringe_results.get(mid_hash, {})
        profiles.append(profile)
```

- [ ] **Step 5: AI 调用对齐分层字段（粗筛全员 + 深掘 top_n 人）**

`quick_test.py:103-115`：

```python
    # 6. AI 分析（批量；Key 判断对齐主流程，走 config 含环境变量读取）
    if profiles and LLM_API_KEY:
        print(f"\n[6/6] AI 画像分析 ({len(profiles)}人)...")
        try:
            analyzer = LLMAnalyzer()
            result = analyzer.analyze(profiles, video_info, batch_size=10)
            per_user = result.get("per_user", {})
            for p in profiles:
                uid = p.get("uid")
                if uid in per_user:
                    p["ai_analysis"] = per_user[uid]
        except Exception as e:
            print(f"   AI 分析失败: {e}")
```

改为：

```python
    # 6. AI 分析（粗筛+深掘；Key 判断对齐主流程，走 config 含环境变量读取）
    if profiles and LLM_API_KEY:
        print(f"\n[6/6] AI 画像分析 ({len(profiles)}人)...")
        try:
            analyzer = LLMAnalyzer()
            result = analyzer.analyze(profiles, video_info, batch_size=10)
            per_user = result.get("per_user", {})
            for p in profiles:
                uid = p.get("uid")
                if uid in per_user:
                    p["ai_brief"] = per_user[uid]
            deep = analyzer.analyze_deep(profiles, video_info, top_k=top_n)
            for p in profiles:
                uid = p.get("uid")
                if uid in deep:
                    p["ai_deep"] = deep[uid]
        except Exception as e:
            print(f"   AI 分析失败: {e}")
```

- [ ] **Step 6: 验证**

Run: `.venv/bin/python -m py_compile quick_test.py && PYTHONPATH=src .venv/bin/python -c "import ast; ast.parse(open('quick_test.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add quick_test.py
git commit -m "feat: quick_test 同步兴趣分选人/评论注入/AI分层字段"
```

---

### Task 9: AGENTS.md 同步 + 端到端验证

**Files:**
- Modify: `AGENTS.md`（项目概述流程、运行参数说明、代码结构、开发约定）

- [ ] **Step 1: AGENTS.md 更新**

四处修改：

1. 项目概述段主流程描述：
`采集该视频的全部弹幕，破解弹幕发送者的匿名 mid_hash（CRC32 反向搜索 + 评论区明文 UID 交叉验证），再对每个发送者做四维度深度画像` 之后、`, 可选调用 LLM 逐人生成 AI 分析` 改为 `, 先本地刷屏检测 + LLM 尬语检测（中二抒情/尬夸捧杀/引战阴阳），按兴趣分（中/高刷屏或尬语命中）动态定员解析发送者，再做四维度深度画像，LLM 分层分析（全员粗筛 + top K 单人深掘）`（整句顺写，保持中文连贯）。

2. 运行与安装段 `--max-users` 说明：
`python run.py BV1vu4y1b7Y9 --max-users 50 # 限制最大深度分析用户数` 改为 `python run.py BV1vu4y1b7Y9 --max-users 50 # 手动覆盖动态定员（默认阈值命中者全进，安全上限300）`

3. 代码结构树：
- `main.py` 行注释改为：`# 主控流程：登录→弹幕→刷屏检测→尬语检测→评论→兴趣分UID解析→用户采集→画像分析→LLM分层分析→报告`
- 在 `spam_detector.py` 行后插入：`├── cringe_detector.py   # LLM 尬语检测（三类判定+发送者聚合，未配置 LLM_API_KEY 自动跳过）`
- `llm_analyzer.py` 行注释改为：`# LLMAnalyzer：批量粗筛（全员标签+定性）+ 重点深掘（top K 单人单调用带证据包，未配置 Key 自动跳过）`

4. 开发约定段采集规模行：
`采集规模由 config.py 中的 MAX_* 常量控制（评论 20 页、关注 50 页、默认最多深度分析 100 人等），调优时改这里而不是散落在代码里的数字。` 改为 `采集规模由 config.py 中的 MAX_* 常量控制（评论 100 页、关注 50 页、动态定员安全上限 MAX_ANALYZE_USERS_HARD_CAP=300、深掘 LLM_DEEP_TOP_K=20、尬语批大小 CRINGE_BATCH_SIZE=200 等），调优时改这里而不是散落在代码里的数字。`

- [ ] **Step 2: 全量语法/导入检查**

Run: `.venv/bin/python -m py_compile run.py quick_test.py src/main.py src/cringe_detector.py src/llm_analyzer.py src/report.py src/profile_analyzer.py && PYTHONPATH=src .venv/bin/python -c "import main; print('import OK')"`
Expected: `import OK`

- [ ] **Step 3: 端到端实跑（后台，真实 API，约 30-60 分钟）**

```bash
PYTHONPATH=src .venv/bin/python run.py BV13MrQBPEec 2>&1 | tee /tmp/bp_e2e.log
```

跑完后检查（以下 grep 均需有输出）：

```bash
grep -E "\[Phase 2.5\]|\[尬语\] 检测完成|兴趣命中" /tmp/bp_e2e.log
grep -E "\[Phase 7a\] 完成|\[Phase 7b\] 完成" /tmp/bp_e2e.log
REPORT=$(ls -t data/reports/report_BV13MrQBPEec_*.html | head -1)
grep -c "弹幕尬语榜" "$REPORT"
grep -c "TA 在本视频的评论" "$REPORT"
grep -c 'bilibili.com/video/BV' "$REPORT"
grep -c "AI 深度画像" "$REPORT"
```

Expected:
- 日志含 `[Phase 2.5]` 刷屏检测、`[尬语] 检测完成`、`兴趣命中 N 人`（N ≥ 16，含全部高/中风险）、Phase 7a/7b 完成行；
- 报告含尬语榜（如有尬语命中）、评论小节、投稿超链接（`target="_blank"`）、AI 深度画像小节；
- 深度名单人数不受 100 限制（若兴趣命中 >100，Phase 5 采集人数 = 命中数，直至 300 上限）；
- 全流程无 `[失败]` 行（风控冷却机制生效）。

- [ ] **Step 4: quick_test 冒烟（对齐 AGENTS.md 验证约定）**

```bash
PYTHONPATH=src .venv/bin/python quick_test.py BV13MrQBPEec --top 2
```

Expected: 跑通，输出报告路径；报告中 top 2 用户含深掘画像。

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md
git commit -m "docs: AGENTS.md 同步兴趣驱动流程与新模块说明"
```

---

## 备注

- 每 Task 的 commit 步骤如遇用户策略要求逐次确认，先询问再执行。
- Task 3 与 Task 4 都改 `src/main.py`，必须按顺序执行（Task 4 的旧代码锚点基于 Task 3 完成后的状态）。
- 执行中若发现 B站接口行为与计划假设不符（如尬语 prompt 输出格式漂移），先小样本验证再调整 prompt，不要直接改判定口径。
