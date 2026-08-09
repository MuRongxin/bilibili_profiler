# 问题弹幕检测扩展 + LLM 成本优化 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将尬语检测扩展为 7 类问题弹幕判定以扩大选人命中面，同时通过 LLM 结果落库缓存与砍掉 7a 全员粗筛把 token 开销压到近零增长。

**Architecture:** 在 `cringe_detector.py` 的同一批 LLM 调用中扩展判定类别（token 不涨）；新增 `llm_cache` SQLite 表缓存问题弹幕判定（按 bvid+内容hash）与重点深掘结果（按 uid+证据包hash）；`llm_analyzer.py` 只保留深掘；报告"尬语榜"更名"问题弹幕榜"并分色标签。

**Tech Stack:** Python 3 标准库（hashlib/json/sqlite3）+ openai 客户端（DeepSeek 兼容接口）。无测试框架，验证靠离线脚本 + 实跑。

**Spec:** `docs/superpowers/specs/2026-08-09-problem-danmaku-llm-cache-design.md`

**项目约定（每个任务都必须遵守）：**
- 运行/验证一律 `PYTHONPATH=src .venv/bin/python ...`
- 注释/打印/文档全中文；模块扁平导入（`from config import ...`）
- `src/config.py` 含真实 API Key，绝不打印/提交其内容
- 失败降级不中断；LLM 全部失败返回空

---

### Task 1: cringe_detector.py 扩展为 7 类问题弹幕判定

**Files:**
- Modify: `src/cringe_detector.py`

- [ ] **Step 1: 改模块 docstring 与常量**

文件开头（第 1-16 行）替换为：

```python
"""
问题弹幕检测（LLM 判定）

对合并去重后的全部弹幕按批喂给 LLM，判定七类问题弹幕：
中二抒情 / 尬夸捧杀 / 引战阴阳 / 人身攻击 / 恶意剧透 / 广告引流 / 键政敏感。
按发送者聚合输出，驱动兴趣分选人与报告问题弹幕榜。
未配置 LLM_API_KEY 或全部批次失败时返回空 dict（降级不中断）。

历史说明：模块与函数名沿用 cringe（尬语）命名是兼容旧调用方的最小改动，
实际判定范围已扩展为"问题弹幕"。
"""
import hashlib
import json

from openai import OpenAI

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_MAX_TOKENS, CRINGE_BATCH_SIZE
from storage import load_llm_cache, save_llm_cache

# 问题弹幕类别（与 spec 一致；prompt 与聚合均引用，勿散落硬编码字符串）
PROBLEM_CATEGORIES = ["中二抒情", "尬夸捧杀", "引战阴阳", "人身攻击", "恶意剧透", "广告引流", "键政敏感"]
```

注意：`from storage import ...` 与 `import hashlib` 是 Task 3 要用的，这里一并引入，避免二次改动 import 区。

- [ ] **Step 2: 更新 prompt（新增 4 类定义）**

`_build_prompt` 的 return 字符串替换为：

```python
    return f"""你是中文互联网内容审核专家。以下是B站视频《{video_title}》的弹幕列表（已按内容去重）。
请逐条判定是否属于以下七类"问题弹幕"之一：
- 中二抒情：咯噔文学、疼痛文学、过度深情、自我感动式抒情
- 尬夸捧杀：无脑吹、饭圈式夸张应援、明显违心的吹捧
- 引战阴阳：拉踩、对线、反串、阴阳怪气等攻击性内容
- 人身攻击：辱骂、诅咒、攻击其他观众/UP主/视频角色
- 恶意剧透：泄露剧情关键信息、结局、反转
- 广告引流：打广告、推广、引流到其他平台或商品
- 键政敏感：借题发挥的政治隐喻、键政引战
正常玩梗、合理讨论、普通应援不算问题弹幕，宁漏勿冤。

弹幕列表：
{chr(10).join(lines)}

请严格只输出一个 JSON 数组，每个元素对应一条判定（只输出判为问题弹幕的条目）：
[{{"i": 编号, "category": "中二抒情|尬夸捧杀|引战阴阳|人身攻击|恶意剧透|广告引流|键政敏感", "severity": 1到3的整数, "reason": "10字内理由"}}]
没有问题弹幕就输出 []。不要输出任何 JSON 之外的内容。"""
```

- [ ] **Step 3: 类别校验与打印文案更新**

- 第 115 行 `v.get("category") in CRINGE_CATEGORIES` → `v.get("category") in PROBLEM_CATEGORIES`
- `detect_cringe_danmaku` docstring 中"尬语检测主入口"→"问题弹幕检测主入口"；返回说明里"该发送者被判尬语的去重内容条数"→"该发送者被判问题弹幕的去重内容条数"
- 全部打印前缀 `[尬语]` → `[问题弹幕]`（共 7 处：第 76、96、106、111、119、122、146 行附近）
- 第 146 行 `f"[尬语] 检测完成: {len(verdicts)} 条尬语，涉及 {len(results)} 个发送者"` → `f"[问题弹幕] 检测完成: {len(verdicts)} 条问题弹幕，涉及 {len(results)} 个发送者"`

- [ ] **Step 4: 离线验证**

Run:
```bash
PYTHONPATH=src .venv/bin/python - <<'EOF'
from cringe_detector import PROBLEM_CATEGORIES, _parse_verdicts, _build_prompt
assert PROBLEM_CATEGORIES == ["中二抒情", "尬夸捧杀", "引战阴阳", "人身攻击", "恶意剧透", "广告引流", "键政敏感"]
# _parse_verdicts 容错行为不变
assert _parse_verdicts("垃圾前缀[{\"i\": 0, \"category\": \"人身攻击\", \"severity\": 2, \"reason\": \"辱骂\"}]后缀")[0]["category"] == "人身攻击"
assert _parse_verdicts("没有JSON") == []
# prompt 包含全部 7 类
p = _build_prompt([{"content": "测试", "count": 1}], 0, "测试视频")
for c in PROBLEM_CATEGORIES:
    assert c in p, c
print("离线验证通过，类别:", PROBLEM_CATEGORIES)
EOF
```
Expected: `离线验证通过，类别: [...7类...]`（storage import 依赖 Task 2 的函数；本任务在 Task 2 之后执行，或先临时注释 storage import 验证后恢复——执行顺序见 Task 2 说明）

- [ ] **Step 5: Commit**

```bash
git add src/cringe_detector.py
git commit -m "feat: 尬语检测扩展为7类问题弹幕判定（新增人身攻击/恶意剧透/广告引流/键政敏感）"
```

---

### Task 2: storage.py 新增 llm_cache 表与读写函数

**Files:**
- Modify: `src/storage.py`

说明：Task 1 的 import 依赖本任务的函数，**实际执行顺序为先做本任务、再做 Task 1**（编排时按 Task 2 → Task 1 执行；编号反映逻辑归属）。

- [ ] **Step 1: init_db 增加建表**

在 `init_db()` 中 `global_uid_map` 建表语句之后、`conn.commit()` 之前插入：

```python
        # LLM 结果缓存表（问题弹幕判定 + 重点深掘，跨运行复用省 token）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS llm_cache (
                cache_key   TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
        ''')
```

- [ ] **Step 2: 新增读写函数**

文件末尾追加新一节：

```python
# ========== LLM 结果缓存（省 token：重跑同视频/同证据包零调用） ==========

def load_llm_cache(cache_key: str) -> str | None:
    """读取 LLM 缓存；不存在或读取异常均返回 None（视为未命中，不中断流水线）"""
    try:
        with closing(get_db()) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT result_json FROM llm_cache WHERE cache_key = ?", (cache_key,))
            row = cursor.fetchone()
        return row["result_json"] if row else None
    except Exception:
        return None


def save_llm_cache(cache_key: str, result_json: str):
    """写入 LLM 缓存；异常只打印警告（缓存失败不影响主流程）"""
    try:
        with closing(get_db()) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO llm_cache (cache_key, result_json, created_at)
                VALUES (?, ?, ?)
            ''', (cache_key, result_json, datetime.now().isoformat()))
            conn.commit()
    except Exception as e:
        print(f"[Storage] 警告: LLM 缓存写入失败（{e}），忽略")
```

- [ ] **Step 3: clear_video_cache 清除该视频判定缓存**

在 `clear_video_cache()` 的 `cursor.execute("DELETE FROM videos WHERE bvid = ?", (bvid,))` 之后插入：

```python
        # 该视频的问题弹幕判定缓存一并清除（key 前缀 cringe:{bvid}:）；
        # 深掘缓存 key 为 deep:{uid}:...，按用户跨视频复用，不清
        cursor.execute("DELETE FROM llm_cache WHERE cache_key LIKE ?", (f"cringe:{bvid}:%",))
```

同时把 `clear_video_cache` docstring 的要点列表追加一行：

```python
    - 删除 llm_cache 中该 bvid 的问题弹幕判定缓存（cringe:{bvid}:*），深掘缓存（deep:*）保留
```

- [ ] **Step 4: 离线验证（用真实库，事后清理测试行）**

注意：`storage.py` 是 `from config import DB_PATH`（值拷贝），改 `config.DB_PATH` 不影响已导入的 storage，因此直接用真实库验证、测试行用 `test:` 前缀事后删除。

Run:
```bash
PYTHONPATH=src .venv/bin/python - <<'EOF'
from storage import init_db, load_llm_cache, save_llm_cache
init_db()
assert load_llm_cache("test:不存在") is None
save_llm_cache("test:demo", '{"ok": 1}')
assert load_llm_cache("test:demo") == '{"ok": 1}'
save_llm_cache("test:demo", '{"ok": 2}')  # upsert 覆盖
assert load_llm_cache("test:demo") == '{"ok": 2}'
# 清理测试行
from storage import get_db
from contextlib import closing
with closing(get_db()) as conn:
    conn.execute("DELETE FROM llm_cache WHERE cache_key LIKE 'test:%'")
    conn.commit()
assert load_llm_cache("test:demo") is None
print("llm_cache 读写验证通过")
EOF
```
Expected: `llm_cache 读写验证通过`

- [ ] **Step 5: Commit**

```bash
git add src/storage.py
git commit -m "feat: 新增 llm_cache 表与读写函数，--force 同步清除视频判定缓存"
```

---

### Task 3: cringe_detector.py 接入判定缓存

**Files:**
- Modify: `src/cringe_detector.py`（`detect_cringe_danmaku` 函数，Task 1 已改好 import 与文案）

- [ ] **Step 1: 函数头部加缓存命中分支**

在 `items = _dedup_contents(danmaku_list)` 与 `if not items: return {}` 之后、`content_senders` 构建之前插入：

```python
    # 判定结果缓存：同一视频去重内容集合未变 → 直接复用（重跑零 LLM 调用）
    bvid = video_info.get("bvid", "")
    cache_key = ""
    if bvid:
        digest = hashlib.sha256(
            json.dumps(items, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        cache_key = f"cringe:{bvid}:{digest}"
        cached = load_llm_cache(cache_key)
        if cached is not None:
            try:
                results = json.loads(cached)
                print(f"[问题弹幕] 缓存命中（{len(results)} 个发送者），跳过 LLM 判定")
                return results
            except json.JSONDecodeError:
                print("[问题弹幕] 警告: 缓存内容损坏，重新判定")
```

- [ ] **Step 2: 函数尾部写回缓存**

`print(f"[问题弹幕] 检测完成: ...")` 之前插入：

```python
    if cache_key:
        save_llm_cache(cache_key, json.dumps(results, ensure_ascii=False))
```

- [ ] **Step 3: 离线验证缓存读写路径（真实库，事后清理）**

Run:
```bash
PYTHONPATH=src .venv/bin/python - <<'EOF'
import hashlib, json
from cringe_detector import _dedup_contents
from storage import load_llm_cache, save_llm_cache, get_db
from contextlib import closing

items = _dedup_contents([{"content": "a"}, {"content": "a"}, {"content": "b"}])
assert items[0] == {"content": "a", "count": 2}  # 按次数降序
digest = hashlib.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
key = f"cringe:BV_TEST_CACHE:{digest}"
save_llm_cache(key, json.dumps({"deadbeef": {"count": 1, "max_severity": 2, "categories": ["引战阴阳"], "examples": []}}, ensure_ascii=False))
hit = json.loads(load_llm_cache(key))
assert hit["deadbeef"]["categories"] == ["引战阴阳"]
with closing(get_db()) as conn:
    conn.execute("DELETE FROM llm_cache WHERE cache_key LIKE 'cringe:BV_TEST_CACHE:%'")
    conn.commit()
print("判定缓存读写路径验证通过")
EOF
```
Expected: `判定缓存读写路径验证通过`

- [ ] **Step 4: Commit**

```bash
git add src/cringe_detector.py
git commit -m "feat: 问题弹幕判定接入 llm_cache，同视频弹幕集合未变重跑零 token"
```

---

### Task 4: llm_analyzer.py 砍 7a 粗筛 + 深掘接入缓存

**Files:**
- Modify: `src/llm_analyzer.py`

- [ ] **Step 1: 删除 7a 相关方法**

删除以下四个方法（整段删除）：`_build_prompt`（第 26-81 行）、`_parse_per_user`（第 83-98 行）、`_analyze_batch`（第 100-123 行）、`analyze`（第 125-152 行）。

- [ ] **Step 2: import 区更新**

第 9-14 行替换为：

```python
import hashlib
import re
import json
import time
from datetime import datetime
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_MAX_TOKENS, LLM_DEEP_TOP_K
from storage import load_llm_cache, save_llm_cache
```

（`re` 保留与否取决于删除后是否还有引用——`_parse_per_user` 删后 `re` 无引用则一并去掉 import；`datetime` 同理，`analyze` 删除后无引用则去掉。实现时 grep 确认后清理。）

模块 docstring 第 1-7 行更新为：

```python
"""
通用 LLM 分析器 — 兼容所有 OpenAI 协议的 API（仅保留重点深掘；全员粗筛已砍，省 token）

通过环境变量切换厂商，零代码改动:
    export LLM_API_KEY="sk-xxx"
    export LLM_BASE_URL="https://api.xiaomimimo.com/v1"
    export LLM_MODEL="mimo-v2.5-pro"
"""
```

- [ ] **Step 3: 抽取证据包构建方法**

`_build_deep_prompt` 中 `evidence = {...}` 的构建逻辑抽成独立方法，prompt 构建与缓存 hash 共用同一份数据：

```python
    def _build_evidence(self, p: dict, video_info: dict) -> dict:
        """深掘证据包（缓存 hash 与 prompt 构建共用同一份数据，保证 hash 能反映证据变化）"""
        dm = p.get("danmaku", {})
        cringe = p.get("cringe", {})
        return {
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
```

`_build_deep_prompt` 相应改为：

```python
    def _build_deep_prompt(self, p: dict, video_info: dict) -> str:
        """重点人员单人深掘 prompt：证据包（弹幕原文/评论/问题弹幕判定/刷屏分析/四维度数据）"""
        title = video_info.get("title", "未知视频")
        evidence = self._build_evidence(p, video_info)
        return f"""你是一位资深网络行为分析师。请对以下这位B站用户做**单人深度行为画像**。
他/她曾在视频《{title}》中发送弹幕，是本视频中值得重点关注的人物（刷屏得分高或存在问题弹幕）。

## 证据包（JSON）
{json.dumps(evidence, ensure_ascii=False, indent=2)}

## 输出要求（严格按以下四节输出，结论必须引用证据包原文作为论据）

**行为定性**: （2-3句：这是个什么样的人，在本视频中扮演什么角色）
**动机分析**: （2-3句：他/她为什么发这些弹幕/评论，想获得什么）
**证据引用**: （列出2-4条最能支撑结论的弹幕或评论原文，并各配一句解读）
**风险等级**: （高/中/低 + 一句理由：对社区氛围的潜在影响）"""
```

- [ ] **Step 4: analyze_deep 接入缓存**

`analyze_deep` 整段替换为：

```python
    def analyze_deep(self, profiles: list[dict], video_info: dict,
                     top_k: int = LLM_DEEP_TOP_K) -> dict[int, str]:
        """重点深掘：兴趣分 top K 单人单调用。兴趣分 = (spam_score, 问题弹幕最高严重度, 弹幕数)。
        证据包未变时命中 llm_cache 直接复用（零 LLM 调用）"""
        def interest(p: dict):
            dm = p.get("danmaku", {})
            return (dm.get("spam_score", 0.0), p.get("cringe", {}).get("max_severity", 0),
                    dm.get("count", 0))

        targets = sorted(profiles, key=interest, reverse=True)[:top_k]
        results = {}
        for i, p in enumerate(targets, 1):
            uid = p.get("uid")
            evidence = self._build_evidence(p, video_info)
            digest = hashlib.sha256(
                json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
            cache_key = f"deep:{uid}:{digest}"
            cached = load_llm_cache(cache_key)
            if cached:
                results[uid] = cached
                print(f"  深掘 {i}/{len(targets)}: UID:{uid} {p.get('name', '')}（缓存命中，跳过 LLM）")
                continue
            print(f"  深掘 {i}/{len(targets)}: UID:{uid} {p.get('name', '')}"
                  f"（LLM请求中，可能需要数十秒）...")
            try:
                text = self._analyze_one_deep(p, video_info)
            except Exception as e:
                # 超时等多为瞬态 API 波动（实测曾整批连续超时后自行恢复），
                # 退避后重试一次；仍失败才降级跳过，不中断整体深掘
                print(f"  警告: UID:{uid} 深掘失败（{e}），20 秒后重试一次...")
                time.sleep(20)
                try:
                    text = self._analyze_one_deep(p, video_info)
                except Exception as e2:
                    print(f"  警告: UID:{uid} 重试仍失败（{e2}），跳过")
                    continue
            if text.strip():
                results[uid] = text
                save_llm_cache(cache_key, text)
            else:
                print(f"  警告: UID:{uid} 深掘响应为空，跳过")
        return results
```

- [ ] **Step 5: 离线验证（mock LLM，验证缓存命中路径零调用）**

Run:
```bash
PYTHONPATH=src .venv/bin/python - <<'EOF'
import json
from llm_analyzer import LLMAnalyzer
from storage import get_db
from contextlib import closing

az = LLMAnalyzer()
profile = {
    "uid": 999000111, "name": "测试员", "level": 4, "sign": "", "vip_status": 0,
    "follower": 10, "archive_count": 0, "tags": ["新用户"],
    "following_summary": {}, "favorite": {"names": []},
    "danmaku": {"count": 3, "contents": ["a", "b", "c"], "spam_level": "高", "spam_score": 0.8, "spam_reason": "测试"},
    "cringe": {"count": 1, "categories": ["引战阴阳"], "max_severity": 2, "examples": []},
    "comments": [],
}
vi = {"title": "测试视频"}
calls = []
az._analyze_one_deep = lambda p, v: (calls.append(1), "深掘结果文本")[1]

r1 = az.analyze_deep([profile], vi, top_k=5)
assert r1[999000111] == "深掘结果文本" and len(calls) == 1, "首次应调用 LLM"
r2 = az.analyze_deep([profile], vi, top_k=5)
assert r2[999000111] == "深掘结果文本" and len(calls) == 1, "第二次应命中缓存不再调用"
# 证据变化 → 缓存失效
profile["danmaku"]["spam_score"] = 0.9
r3 = az.analyze_deep([profile], vi, top_k=5)
assert len(calls) == 2, "证据包变化后应重新调用"
# 清理测试缓存
with closing(get_db()) as conn:
    conn.execute("DELETE FROM llm_cache WHERE cache_key LIKE 'deep:999000111:%'")
    conn.commit()
print("深掘缓存验证通过（首次调用→缓存命中→证据变化重调）")
EOF
```
Expected: `深掘缓存验证通过（首次调用→缓存命中→证据变化重调）`

- [ ] **Step 6: Commit**

```bash
git add src/llm_analyzer.py
git commit -m "feat: 砍掉7a全员粗筛只保留深掘，深掘结果接入 llm_cache（证据包未变零调用）"
```

---

### Task 5: main.py 移除 7a 调用 + 全流程"问题弹幕"文案

**Files:**
- Modify: `src/main.py`
- Modify: `quick_test.py`

- [ ] **Step 1: phase_ai_analysis 整段替换（第 432-465 行）**

```python
def phase_ai_analysis(video_info: dict, profiles: list[dict]):
    """阶段7: LLM 重点深掘（兴趣分 top K 单人单调用，结果直接注入 profile；
    全员粗筛已砍——命中人数扩大后粗筛是 token 大头，普通用户由规则标签勾画轮廓）"""
    if not LLM_API_KEY:
        print("\n[Phase 7] 跳过 (未在 config.py 或环境变量中设置 LLM_API_KEY)")
        return

    try:
        analyzer = LLMAnalyzer()
    except Exception as e:
        print(f"[Phase 7] LLM 分析失败: {e}")
        return

    print("\n[Phase 7] LLM 重点深掘（兴趣分 top K 单人单调用）...")
    try:
        deep = analyzer.analyze_deep(profiles, video_info)
        for p in profiles:
            uid = p.get("uid")
            if uid in deep:
                p["ai_deep"] = deep[uid]
        print(f"[Phase 7] 完成: {len(deep)} 人生成深度画像")
    except Exception as e:
        print(f"[Phase 7] LLM 分析失败: {e}")
```

- [ ] **Step 2: 文案更新（尬语 → 问题弹幕）**

main.py 中以下处（行号为改动前参考）：
- 第 305 行 `"""阶段2.6: 尬语检测（LLM，未配置 Key 或失败时返回空 dict 降级）"""` → `"""阶段2.6: 问题弹幕检测（LLM，未配置 Key 或失败时返回空 dict 降级）"""`
- 第 306 行 `print("\n[Phase 2.6] 弹幕尬语检测...")` → `print("\n[Phase 2.6] 弹幕问题内容检测（LLM）...")`
- 第 310 行 `print(f"[Phase 2.6] 警告: 尬语检测失败（{e}），降级跳过")` → `print(f"[Phase 2.6] 警告: 问题弹幕检测失败（{e}），降级跳过")`
- 第 140 行 docstring `spam_level∈{高,中} 或 尬语条数≥1 的发送者` → `spam_level∈{高,中} 或 问题弹幕≥1 条的发送者`
- 第 204 行 `cringe.get("max_severity", 0)` 所在 `interest_key` 注释无尬语字样则不动
- 第 214 行 `print(f"[Phase 4] 兴趣命中 {len(must)} 人（中/高刷屏或尬语），"` → `print(f"[Phase 4] 兴趣命中 {len(must)} 人（中/高刷屏或问题弹幕），"`
- 第 497 行注释 `# 阶段2.6: 尬语检测（LLM，可降级）` → `# 阶段2.6: 问题弹幕检测（LLM，可降级）`
- 第 515 行注释 `# 合并刷屏/尬语数据到resolved` → `# 合并刷屏/问题弹幕数据到resolved`
- 第 526 行注释 `# 阶段6: 画像分析（评论IP属地/本视频评论/尬语在此贯通进画像）` → `# 阶段6: 画像分析（评论IP属地/本视频评论/问题弹幕在此贯通进画像）`
- 第 530 行注释 `# 阶段7: LLM 分层画像分析（粗筛/深掘结果在 phase 内直接注入 profile）` → `# 阶段7: LLM 重点深掘（结果在 phase 内直接注入 profile）`

quick_test.py：
- 第 54-55 行 `# 3. 刷屏检测 + 尬语检测 → 兴趣分 Top N（对齐主流程兴趣口径）` 与 `print("[3/6] 刷屏检测 + 尬语检测...")` 中"尬语检测"→"问题弹幕检测"
- 第 60-61 行注释与打印中"尬语检测失败"→"问题弹幕检测失败"

注意：变量名 `cringe_results`、`cringe` 键名一律不改（下游 report.py/llm_analyzer.py 按此消费，改名是无收益的大范围 churn）。

- [ ] **Step 3: 编译与 import 验证**

Run: `.venv/bin/python -m py_compile src/main.py quick_test.py src/llm_analyzer.py src/cringe_detector.py src/storage.py && PYTHONPATH=src .venv/bin/python -c "import main; print('import OK')"`
Expected: `import OK`

- [ ] **Step 4: Commit**

```bash
git add src/main.py quick_test.py
git commit -m "refactor: main 流程移除 7a 粗筛调用，全流程文案升级为问题弹幕"
```

---

### Task 6: report.py 问题弹幕榜 + ai_brief 清理

**Files:**
- Modify: `src/report.py`

- [ ] **Step 1: 类别分色常量**

文件顶部 import 区之后（模块级）新增：

```python
# 问题弹幕类别分色（问题弹幕榜标签底色；七类与 cringe_detector.PROBLEM_CATEGORIES 对齐）
PROBLEM_CATEGORY_COLORS = {
    "中二抒情": "#9c6ade",
    "尬夸捧杀": "#f06292",
    "引战阴阳": "#e53935",
    "人身攻击": "#b71c1c",
    "恶意剧透": "#fb8c00",
    "广告引流": "#8d6e63",
    "键政敏感": "#546e7a",
}


def _category_chips(categories: list) -> str:
    """类别列表 → 分色标签 HTML（未知类别用灰色兜底）"""
    chips = []
    for cat in categories:
        color = PROBLEM_CATEGORY_COLORS.get(cat, "#999999")
        chips.append(f'<span style="display:inline-block;background:{color};color:#fff;'
                     f'font-size:12px;border-radius:4px;padding:1px 8px;margin:1px 2px;">{esc(cat)}</span>')
    return "".join(chips)
```

（确认 `esc` 在文件中定义位置——若 `_category_chips` 定义在 `esc` 之前会 NameError，实现时把这两个定义放在 `esc` 之后。）

- [ ] **Step 2: AI 区块清理 ai_brief（第 139-142 行）**

```python
    # AI画像分析（仅深掘；ai_analysis 为兼容旧报告数据保留）
    ai_deep = profile.get("ai_deep", "")
    ai_text = ai_deep or profile.get("ai_analysis", "")
    ai_heading = "🤖 AI 深度画像"
```

- [ ] **Step 3: 弹幕行为行内联标记（第 156-159 行）**

```python
    # 问题弹幕内联标记（弹幕行为行尾）
    cringe = profile.get("cringe", {})
    cringe_note = (f'，其中问题弹幕 {cringe["count"]} 条（{"、".join(cringe.get("categories", []))}）'
                   if cringe.get("count") else "")
```

- [ ] **Step 4: ai_count 统计（第 291-292 行）**

```python
    # 统计AI画像覆盖（深掘/旧字段任一存在即计数）
    ai_count = sum(1 for p in profiles if p.get("ai_deep") or p.get("ai_analysis"))
```

- [ ] **Step 5: 尬语榜 → 问题弹幕榜（第 342-367 行）**

整段替换为：

```python
    # 问题弹幕榜：按发送者聚合（最高严重度、条数降序），无命中时不渲染
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
                f'<td>{_category_chips(cr.get("categories", []))}</td>'
                f'<td>{esc(cr.get("max_severity", 0))}</td>'
                f'<td>{esc(example.get("content", ""))}<br>'
                f'<span class="cringe-reason">{esc(example.get("category", ""))}: {esc(example.get("reason", ""))}</span></td></tr>'
            )
        cringe_board_html = f'''
    <div class="cringe-board">
        <h3>🚨 问题弹幕榜（{len(cringe_entries)} 人命中）</h3>
        <table>
            <thead><tr><th>用户</th><th>命中条数</th><th>类别</th><th>最高严重度</th><th>代表原文</th></tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
    </div>'''
```

- [ ] **Step 6: 排序注释更新（第 271 行）**

`# 用户卡片按风险等级排序展示：高→中→低；同级按兴趣分（刷屏分/尬语严重度/弹幕数）降序` → `# 用户卡片按风险等级排序展示：高→中→低；同级按兴趣分（刷屏分/问题弹幕严重度/弹幕数）降序`

- [ ] **Step 7: 离线渲染冒烟（构造含新类别的假 profile 渲染，确认无异常且新文案出现）**

Run:
```bash
PYTHONPATH=src .venv/bin/python - <<'EOF'
from report import generate_html_report
profile = {
    "uid": 12345, "name": "测试用户", "level": 5, "face": "", "sign": "测试签名",
    "sex": "男", "vip_status": 0, "follower": 100, "following": 50, "like_num": 200,
    "archive_count": 3, "dynamic": {"count": 10}, "tags": ["新用户", "重度刷屏"],
    "danmaku": {"count": 8, "contents": ["测试弹幕"] * 8, "spam_level": "高", "spam_score": 0.85, "spam_reason": "大量重复"},
    "cringe": {"count": 2, "max_severity": 3, "categories": ["人身攻击", "键政敏感"],
               "examples": [{"content": "测试弹幕", "category": "人身攻击", "severity": 3, "reason": "辱骂观众"}]},
    "comments": [], "ai_deep": "**行为定性**: 测试", "collision_risk": False,
    "following_summary": {}, "favorite": {}, "bangumi_titles": [], "drama_titles": [],
}
html = generate_html_report({"title": "测试视频", "bvid": "BV_TEST"}, [profile])
assert "问题弹幕榜" in html and "人身攻击" in html and "键政敏感" in html
assert "尬语" not in html, "报告不应再出现'尬语'字样"
assert "AI 深度画像" in html and "AI 粗筛画像" not in html
print("报告渲染冒烟通过（问题弹幕榜 + 新类别 + 无尬语字样）")
EOF
```
Expected: `报告渲染冒烟通过（问题弹幕榜 + 新类别 + 无尬语字样）`
（若 `generate_html_report` 签名或必需要素不同，实现时以实际函数签名为准调整调用。）

- [ ] **Step 8: Commit**

```bash
git add src/report.py
git commit -m "feat: 报告尬语榜升级问题弹幕榜（7类分色标签），移除粗筛画像区块"
```

---

### Task 7: 文档同步（AGENTS.md / README.md / spec 提交）

**Files:**
- Modify: `AGENTS.md`
- Modify: `README.md`（若有尬语/LLM 相关描述）
- Add: `docs/superpowers/specs/2026-08-09-problem-danmaku-llm-cache-design.md`

- [ ] **Step 1: AGENTS.md 更新**

- 代码结构 `cringe_detector.py` 行 → `├── cringe_detector.py   # LLM 问题弹幕检测（七类判定+发送者聚合+llm_cache缓存，未配置 LLM_API_KEY 自动跳过）`
- `llm_analyzer.py` 行 → `├── llm_analyzer.py      # LLMAnalyzer：重点深掘（兴趣分 top K 单人单调用+llm_cache缓存；全员粗筛已砍，未配置 Key 自动跳过）`
- `storage.py` 行 → `├── storage.py           # SQLite 持久化（data/profiler.db），支撑断点续采与 LLM 结果缓存（llm_cache 表）`
- 主流程描述行（第 10 行附近）中"刷屏检测→用户采集"链条补"问题弹幕检测"：`- 主流程（登录→弹幕→评论→UID解析→刷屏检测→用户采集→画像分析→LLM分析→报告）` → `- 主流程（登录→弹幕→刷屏检测→问题弹幕检测→评论→UID解析→用户采集→画像分析→LLM深掘→报告）`
- 实现时通读 AGENTS.md，把其余提到"尬语""分层 LLM 分析"的句子同步为"问题弹幕""LLM 深掘"。

- [ ] **Step 2: README.md 检查更新**

Run: `grep -n "尬语\|粗筛\|分层" README.md`
若有匹配，按同一口径更新；无匹配则跳过。

- [ ] **Step 3: Commit（含此前未提交的 spec 文档）**

```bash
git add AGENTS.md README.md docs/superpowers/specs/2026-08-09-problem-danmaku-llm-cache-design.md docs/superpowers/plans/2026-08-09-problem-danmaku-llm-cache.md
git commit -m "docs: 问题弹幕+LLM缓存设计与计划文档，AGENTS/README 同步新流程"
```

---

### Task 8: 端到端实跑验证（两轮）

**Files:** 无改动，仅运行验证。需要有效 Cookie（data/cookie.json）与 DeepSeek Key（src/config.py 或 LLM_API_KEY 环境变量）。

- [ ] **Step 1: 全量编译 + import 检查**

Run: `.venv/bin/python -m py_compile run.py quick_test.py src/*.py && PYTHONPATH=src .venv/bin/python -c "import main, report, llm_analyzer, cringe_detector, storage; print('全部 import OK')"`
Expected: `全部 import OK`

- [ ] **Step 2: 第一轮实跑（正常 LLM 调用）**

Run: `PYTHONPATH=src .venv/bin/python run.py BV1wZMy6DE31`（前台长时运行，日志持续输出）
检查点：
- `[Phase 2.6]` 日志显示分批判定，最终"检测完成: N 条问题弹幕，涉及 M 个发送者"
- `[Phase 4]` 兴趣命中人数明显多于改动前的 25 人
- `[Phase 7]` 只有"LLM 重点深掘"，无 7a 批次日志
- 报告中出现"问题弹幕榜"与分色类别标签，无"尬语"字样

- [ ] **Step 3: 第二轮实跑（缓存命中验证）**

Run: `PYTHONPATH=src .venv/bin/python run.py BV1wZMy6DE31`（不加 --force，紧接第一轮）
检查点：
- `[问题弹幕] 缓存命中（M 个发送者），跳过 LLM 判定`（M 与第一轮一致）
- 深掘日志全部显示"缓存命中，跳过 LLM"
- 报告正常生成

注意：两轮之间弹幕池可能有新弹幕流入导致内容集合 hash 变化、判定缓存未命中——这属预期（spec 4.2：v1 不做增量合并），不是 bug；若发生，记录日志说明即可。

- [ ] **Step 4: quick_test 冒烟**

Run: `PYTHONPATH=src .venv/bin/python quick_test.py BV1wZMy6DE31 --top 3`
Expected: 全流程跑通无异常，日志文案为"问题弹幕检测"

---

## Self-Review 记录

- Spec 覆盖：§3 问题弹幕扩展→Task 1/3；§4 缓存→Task 2/3/4；§5 砍 7a→Task 4/5/6（ai_brief 清理）；§6 报告→Task 6；§7 错误处理→沿用各任务降级分支；§8 验证→Task 8。§4.2 deep 缓存按 uid 不清、--force 只清 cringe 前缀→Task 2 Step 3。
- 类型一致性：`load_llm_cache/save_llm_cache` 在 Task 2 定义，Task 1/3/4 引用一致；`_build_evidence` Task 4 定义并在 `analyze_deep`/`_build_deep_prompt` 共用；`PROBLEM_CATEGORY_COLORS`/`_category_chips` Task 6 定义并同任务使用。
- 已知先后依赖：Task 2 必须先于 Task 1（import 依赖），执行顺序为 Task 2 → 1 → 3 → 4 → 5 → 6 → 7 → 8。
