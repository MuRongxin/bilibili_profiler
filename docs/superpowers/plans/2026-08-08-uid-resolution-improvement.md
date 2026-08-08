# UID 破解能力改进 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把弹幕发送者 mid_hash→UID 的解析率提到最高：MITM 秒级反查覆盖全部 ≤10 位 UID（替换 50M 彩虹表），全局映射库跨视频复用，新增充电名单/互动弹幕两个明文 UID 源，评论采集扩容。

**Architecture:** 依据 spec `docs/superpowers/specs/2026-08-08-uid-resolution-improvement-design.md`。P0 用 CRC32 仿射性质做"前缀+5位定长后缀"中间相遇反查（10 万条内存小表，纯标准库）；P1 在 SQLite 加全局 `global_uid_map` 表；P2 两个明文源（`elec/show` 充电名单、`dm/web/view` commandDms）在 main 层按优先级合并进交叉验证映射；P3 仅调 config 常量+日志。

**Tech Stack:** Python 3 标准库（zlib/sqlite3/lxml 已有），无新依赖。项目无 pytest，验证用 `PYTHONPATH=src .venv/bin/python` 内联断言脚本（临时文件跑完即删）+ `quick_test.py` 冒烟。

**执行前准备：** 创建分支 `git checkout -b upgrade/uid-resolution`（main 干净时）。所有 commit 在此分支，Task 7 结束后合并回 main。

**已知调用点清单（2026-08-08 实测，勿重复调查）：**
- `resolve_sender` 外部调用点仅 `quick_test.py:80`（位置参数 5 个，新参数必须放最后且带默认值）
- `resolve_all_senders` 调用点仅 `src/main.py:159`
- `CRC32_MAX_SEARCH` 引用点：`src/config.py:60`、`src/uid_resolver.py`（import 及两处默认参数）、`config.example.py:62`
- `crc_rainbow` 引用点仅 `src/uid_resolver.py:14`（`build_table, lookup, table_exists`）
- `phase_comment` 调用点仅 `src/main.py:385`

---

### Task 1: P0a — MITM 反查引擎（重写 src/crc_rainbow.py）

**Files:**
- Modify: `src/crc_rainbow.py`（整体重写，文件名保留，对外仍暴露 `lookup`）
- Test: 临时脚本 `/tmp/test_mitm.py`（跑完删除）

算法原理（实现者须知）：`zlib.crc32` 可链式调用（`crc32(a+b) == crc32(b, crc32(a))`），且对定长 5 字节后缀 s，`f_s(x) = crc32(s, x)` 在 GF(2) 上是 x 的仿射函数，其线性部分与 s 内容无关（与 5 个 0x00 字节相同）。因此 `f_s(x) = _advance5(x) ^ crc32(b"\x00"*5) ^ crc32(s)`，其中 `_advance5(x)` 为从链式值 x 推进 5 个零字节。预计算全部 10 万个 5 位后缀串的 crc，查询时枚举 ≤5 位前缀（共 99999 个），反查所需后缀 crc 即得候选。

- [ ] **Step 1: 写失败的验证脚本**

写 `/tmp/test_mitm.py`：

```python
"""MITM 反查引擎验证：随机 UID 抽样 + 暴力枚举对照 + 边界值"""
import sys, time, zlib, random
sys.path.insert(0, "src")
from crc_rainbow import lookup   # 新实现只需这一个函数

def brute_candidates(target: int, lo: int, hi: int) -> set:
    return {u for u in range(lo, hi) if zlib.crc32(str(u).encode()) == target}

random.seed(42)

# 1) 边界 UID 必须能反查到
edge_uids = [0, 1, 9, 10, 99999, 100000, 999999, 10**7, 10**8, 10**9,
             2_147_483_647, 4_294_967_295, 9_999_999_999]
for uid in edge_uids:
    h = format(zlib.crc32(str(uid).encode()), "08x")
    res = lookup(h)
    assert uid in res, f"边界 UID {uid} 未命中: {res}"
    assert all(zlib.crc32(str(u).encode()) == zlib.crc32(str(uid).encode()) for u in res), \
        f"返回了未验证的候选: {res}"
print(f"边界 UID 全部通过（{len(edge_uids)} 个）")

# 2) 随机 UID 各数量级抽样，且与暴力枚举区间对照（MITM 结果 ⊇ 暴力命中）
for lo, hi in [(0, 100_000), (100_000, 50_000_000), (50_000_000, 500_000_000),
               (500_000_000, 2_200_000_000), (2_200_000_000, 10_000_000_000)]:
    for _ in range(20):
        uid = random.randrange(lo, hi)
        target = zlib.crc32(str(uid).encode())
        h = format(target, "08x")
        res = set(lookup(h))
        assert uid in res, f"UID {uid} 未命中"
        # 暴力对照：以 uid 为中心 ±300 区间内的全部命中必须是 res 的子集
        brute = brute_candidates(target, max(lo, uid - 300), min(hi, uid + 301))
        assert brute <= res, f"MITM 漏掉暴力命中的候选: {brute - res}"
print("随机抽样 + 暴力对照全部通过（5 个数量级 × 20 个）")

# 3) 候选数 sanity：≤10 位空间每个 hash 平均约 2.3 个候选
total_cand = sum(len(lookup(format(zlib.crc32(str(u).encode()), "08x")))
                 for u in random.sample(range(10**8, 10**9), 50))
print(f"平均候选数 {total_cand / 50:.2f}（应在 1~6 之间）")
assert 1 <= total_cand / 50 <= 6

# 4) 非法输入
assert lookup("xyz") == []
assert lookup("") == []
assert lookup("123456789") == []   # 超过 8 位 hex（>32bit）

# 5) 性能：单次查询秒级
t0 = time.time()
lookup(format(zlib.crc32(b"1234567890"), "08x"))
print(f"含建表首次查询 {time.time() - t0:.2f}s")
t0 = time.time()
for _ in range(5):
    lookup(format(zlib.crc32(str(random.randrange(10**10)).encode()), "08x"))
dt = (time.time() - t0) / 5
print(f"稳态单次查询 {dt:.3f}s")
assert dt < 2.0, "查询超时"

print("MITM 引擎全部验证通过")
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src .venv/bin/python /tmp/test_mitm.py`
Expected: FAIL（现有 `lookup` 依赖彩虹表文件，边界 UID >5000 万全部未命中，第一个 assert 即失败）

- [ ] **Step 3: 整体重写 src/crc_rainbow.py**

完整替换文件内容为：

```python
"""
MITM（中间相遇）CRC32 反查：mid_hash → 全部候选 UID

算法：zlib.crc32 可链式调用，且对定长 5 字节后缀 s，f_s(x)=crc32(s,x) 是 x 的
仿射函数，线性部分与 s 内容无关（同 5 个 0x00 字节）。故
    f_s(x) = _advance5(x) ^ crc32(b"\\x00"*5) ^ crc32(s)
预计算 10 万个 5 位后缀串（"00000"~"99999"）的 crc 建内存小表（首次查询时
惰性构建，秒级，无需落盘大表）；查询时枚举 ≤5 位前缀（6~10 位 UID 共 99999 个
前缀），反查所需后缀 crc 得候选，最后逐候选 zlib 校验保证精确。

参考：esterTion/BiliBili_crc2mid、Aruelius/crc32-crack（MoePus MITM）。
覆盖范围：全部 ≤10 位 UID（16 位随机长 UID 数学上不可解，见 spec 第 9 节）。
纯标准库实现，不引入第三方依赖。
"""
import zlib

from config import MITM_MAX_UID

_SUFFIX_DIGITS = 5                    # 后缀定长（十进制位）
_SUFFIX_COUNT = 10 ** _SUFFIX_DIGITS  # 100000
_MAX_PREFIX = 10 ** _SUFFIX_DIGITS    # 前缀最多 5 位：99999

# 惰性构建的进程级缓存
_suffix_crc_map: dict | None = None   # crc32("%05d" % n) -> [n, ...]
_small_uid_map: dict | None = None    # crc32(str(uid)) -> [uid, ...]，uid < 100000
_prefix_crc: list | None = None       # [crc32(str(p)) for p in range(100000)]
_zeros5_crc: int = 0                  # crc32(b"\x00" * 5)
_crc_byte_table: list | None = None   # 标准 CRC32 字节推进表（多项式 0xEDB88320）


def _get_byte_table() -> list:
    """标准 CRC32 表（与 zlib 相同的多项式），用于手写字节推进"""
    global _crc_byte_table
    if _crc_byte_table is None:
        table = []
        for i in range(256):
            c = i
            for _ in range(8):
                c = (0xEDB88320 ^ (c >> 1)) if (c & 1) else (c >> 1)
            table.append(c)
        _crc_byte_table = table
    return _crc_byte_table


def _advance5(crc: int) -> int:
    """从链式值 crc 推进 5 个 0x00 字节，等价于 zlib.crc32(b"\\x00"*5, crc)"""
    table = _get_byte_table()
    state = crc ^ 0xFFFFFFFF
    for _ in range(5):
        state = table[state & 0xFF] ^ (state >> 8)
    return state ^ 0xFFFFFFFF


def _ensure_tables():
    """首次查询时惰性构建全部内存表（约 1-2 秒，之后驻留内存仅几 MB）"""
    global _suffix_crc_map, _small_uid_map, _prefix_crc, _zeros5_crc
    if _suffix_crc_map is not None:
        return
    suffix_map: dict[int, list] = {}
    small_map: dict[int, list] = {}
    prefix_crc = [0] * _SUFFIX_COUNT
    for n in range(_SUFFIX_COUNT):
        suffix_map.setdefault(zlib.crc32(("%05d" % n).encode()), []).append(n)
        small_map.setdefault(zlib.crc32(str(n).encode()), []).append(n)
        prefix_crc[n] = zlib.crc32(str(n).encode())
    _suffix_crc_map = suffix_map
    _small_uid_map = small_map
    _prefix_crc = prefix_crc
    _zeros5_crc = zlib.crc32(b"\x00" * 5)


def lookup(crc32_hash: str, max_uid: int = MITM_MAX_UID) -> list:
    """
    MITM 反查：输入 8 位 hex（如 '4200b4cd'），返回全部候选 UID（升序）。

    覆盖 ≤10 位 UID（由 max_uid 控制，默认 MITM_MAX_UID=10^10）。
    每个返回候选都经过 zlib 校验，数学上精确；候选数平均约 2.3 个
    （10^10 空间对 2^32 哈希空间），消歧由调用方负责。
    输入非法时返回空列表。
    """
    try:
        target = int(crc32_hash.strip(), 16)
    except (ValueError, AttributeError):
        return []
    if not (0 <= target <= 0xFFFFFFFF):
        return []

    _ensure_tables()
    max_uid = min(max_uid, MITM_MAX_UID)
    results = set()

    # 1) UID < 100000（不足 6 位，无前缀可拆）：直接查小表
    for uid in _small_uid_map.get(target, ()):
        if uid <= max_uid:
            results.add(uid)

    # 2) 6~10 位 UID：前缀（str(prefix)）+ 5 位定长后缀
    z5 = _zeros5_crc
    suffix_map = _suffix_crc_map
    prefix_crc = _prefix_crc
    for prefix in range(1, _MAX_PREFIX):
        uid_base = prefix * _SUFFIX_COUNT
        if uid_base > max_uid:
            break
        need = target ^ _advance5(prefix_crc[prefix]) ^ z5
        for n in suffix_map.get(need, ()):
            uid = uid_base + n
            if uid <= max_uid:
                results.add(uid)

    # 3) 逐候选 zlib 校验（仿射推导理论上精确，校验防御实现错误）并排序
    return sorted(u for u in results if zlib.crc32(str(u).encode()) == target)
```

注意：旧文件中的 `build_table` / `table_exists` / mmap 相关全部删除，不再保留；`lookup` 签名变为 `lookup(crc32_hash, max_uid=MITM_MAX_UID)`，旧调用方仅 `uid_resolver.py` 一处（Task 2 适配）。此 Task 完成后 `import crc_rainbow` 会触发 `from config import MITM_MAX_UID`——该常量由 Task 2 加入 config.py，**本 Task 的 Step 4 之前先临时把 Task 2 的 config.py 改动一并做掉**（见下），否则 import 失败：

`src/config.py`：删除 60-66 行（`CRC32_MAX_SEARCH`、`CRC32_OLD_MAX`、`CRC_TABLE_PATH`、`CRC_TABLE_MAX_UID`、`CRC_BUILD_CHUNK` 及"破解配置"/"CRC32 彩虹表配置"两节注释），替换为：

```python
# ========== 破解配置 ==========
MITM_MAX_UID = 10_000_000_000   # MITM 反查覆盖上限：全部 ≤10 位 UID（16位随机长UID不可解）
```

但 `src/uid_resolver.py:13` 还在 `from config import CRC32_MAX_SEARCH, USER_CARD_URL`，config 改动后 uid_resolver import 会崩——Task 1 的验证脚本不 import uid_resolver，所以 Step 4 不受影响；uid_resolver 的适配属 Task 2。中间态仓库不可运行主流程，属预期（Task 1+2 连续完成后再跑冒烟）。

- [ ] **Step 4: 运行验证脚本**

Run: `PYTHONPATH=src .venv/bin/python /tmp/test_mitm.py`
Expected: 全部通过；稳态单次查询 < 2s（预期 0.1~0.5s）；平均候选数 1~6。跑完 `rm /tmp/test_mitm.py`。

- [ ] **Step 5: Commit**

```bash
git add src/crc_rainbow.py src/config.py
git commit -m "feat: MITM中间相遇反查引擎替换彩虹表，覆盖全部≤10位UID

- crc_rainbow.py 整体重写：10万条内存小表惰性构建，单次查询秒级，候选经zlib校验
- 删除 build_table/table_exists/mmap 彩虹表机制（本地 data/crc_table.bin 不再被引用，用户可自行删除）
- config: 移除 CRC_TABLE_*/CRC32_MAX_SEARCH/CRC32_OLD_MAX，新增 MITM_MAX_UID=10^10"
```

---

### Task 2: P0b — uid_resolver 接入 MITM 与碰撞消歧流水线

**Files:**
- Modify: `src/uid_resolver.py`（crack_crc32 重写、删 _crack_crc32_fallback、resolve_sender 消歧流水线、resolve_all_senders 透传）
- Modify: `config.example.py`（同步 Task 1 的 config.py 改动）
- Test: 临时脚本 `/tmp/test_resolver.py`（跑完删除）

- [ ] **Step 1: 写失败的验证脚本**

写 `/tmp/test_resolver.py`（mock 客户端，覆盖 5 种消歧场景）：

```python
"""resolve_sender 消歧流水线验证（mock API，不联网）"""
import sys, zlib
sys.path.insert(0, "src")
from unittest.mock import patch, MagicMock
import uid_resolver
from uid_resolver import (resolve_sender, crack_crc32,
                          METHOD_COMMENT_VERIFY, METHOD_CRC32_CRACK,
                          METHOD_CRC32_COLLISION, METHOD_UNKNOWN)

client = MagicMock()

def hash_of(uid):
    return format(zlib.crc32(str(uid).encode()), "08x")

# crack_crc32 新契约：返回全部候选 list
cands = crack_crc32(hash_of(37704035))
assert isinstance(cands, list) and 37704035 in cands, f"crack_crc32 契约错误: {cands}"
print("crack_crc32 返回候选列表 OK")

# 场景1：评论映射直接命中 → 评论区验证
with patch.object(uid_resolver, "verify_uid_exists", return_value=(True, {"name": "A"})):
    uid, conf, method, info, risk = resolve_sender(
        hash_of(123456), ["弹幕1"], {hash_of(123456): 123456}, client)
    assert (uid, method, conf, risk) == (123456, METHOD_COMMENT_VERIFY, "中", False), \
        f"场景1失败: {uid} {method} {conf} {risk}"
print("场景1 评论直接命中 OK")

# 场景2：评论未命中，MITM 候选与评论 UID 集合唯一交集 → 评论区验证
real = 37704035
h = hash_of(real)
cands = crack_crc32(h)
other = [c for c in cands if c != real]
comment_map = {"deadbeef": real}   # 评论里有 real 的明文 UID（别的 crc 条目）
if other:
    with patch.object(uid_resolver, "verify_uid_exists",
                      side_effect=lambda u, c: (u == real, {"name": "B"} if u == real else {})):
        uid, conf, method, info, risk = resolve_sender(h, ["d1", "d2"], comment_map, client)
        assert uid == real and method == METHOD_COMMENT_VERIFY and conf == "高" and not risk, \
            f"场景2失败: {uid} {method} {conf} {risk}"
    print(f"场景2 候选∩评论唯一交集 OK（本 hash 共 {len(cands)} 候选）")
else:
    print("场景2 跳过（该 hash 无碰撞候选，交集情形已被场景1覆盖）")

# 场景3：候选恰好 1 个存在 → CRC32破解，置信度中，collision_risk=True
with patch.object(uid_resolver, "verify_uid_exists",
                  side_effect=lambda u, c: (u == real, {"name": "C"} if u == real else {})):
    uid, conf, method, info, risk = resolve_sender(h, ["d1", "d2"], {}, client)
    assert uid == real and method == METHOD_CRC32_CRACK and conf == "中" and risk, \
        f"场景3失败: {uid} {method} {conf} {risk}"
print("场景3 恰好1个存在 OK")

# 场景4：多个存在 → 取最小 UID，置信度低，collision_risk=True，result 含全部候选
with patch.object(uid_resolver, "verify_uid_exists", return_value=(True, {"name": "D"})):
    uid, conf, method, info, risk = resolve_sender(h, ["d1"], {}, client)
    assert uid == min(cands) and method == METHOD_CRC32_CRACK and conf == "低" and risk, \
        f"场景4失败: {uid} {method} {conf} {risk}"
print("场景4 多候选取最小 OK")

# 场景5：候选全部不存在 → CRC32碰撞，uid=None
with patch.object(uid_resolver, "verify_uid_exists", return_value=(False, {})):
    uid, conf, method, info, risk = resolve_sender(h, ["d1"], {}, client)
    assert uid is None and method == METHOD_CRC32_COLLISION, f"场景5失败: {uid} {method}"
print("场景5 候选全不存在 OK")

# 场景6：完全未命中（构造一个在 ≤10 位空间无 preimage 概率极低的输入无法保证，
# 改为验证未知路径仍存在）：非法 hash → 未知
uid, conf, method, info, risk = resolve_sender("ffffffff", ["d1"], {}, client)
assert method in (METHOD_UNKNOWN, METHOD_CRC32_COLLISION), f"场景6失败: {method}"
print(f"场景6 未命中路径 OK（method={method}）")

print("resolve_sender 消歧流水线全部验证通过")
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src .venv/bin/python /tmp/test_resolver.py`
Expected: FAIL（import uid_resolver 即崩：`from config import CRC32_MAX_SEARCH` 已不存在）

- [ ] **Step 3: 重写 src/uid_resolver.py 的相关部分**

3a. import 行（第 13-14 行）改为：

```python
from config import MITM_MAX_UID, USER_CARD_URL
from crc_rainbow import lookup
```

3b. 删除 `_table_build_attempted` 全局变量及注释（约 28-29 行）、整个 `crack_crc32` 旧实现（约 40-88 行）、整个 `_crack_crc32_fallback`（约 91-156 行）。替换为：

```python
def crack_crc32(crc32_hash: str, max_search: int = MITM_MAX_UID) -> list:
    """
    MITM 反查 mid_hash，返回全部候选 UID（升序，每个都经 zlib 校验）

    ≤10 位空间每个 hash 平均约 2.3 个候选，消歧（评论交集/存在性验证/
    置信度压制）由调用方 resolve_sender 负责。16 位随机长 UID 数学上不可解。

    Args:
        crc32_hash: 弹幕 mid_hash（8位hex）
        max_search: UID 覆盖上限（语义由 MITM_MAX_UID 承接，保留参数名兼容旧调用）

    Returns:
        候选 UID 列表（可能为空=未命中）
    """
    return lookup(crc32_hash, max_uid=max_search)
```

3c. `resolve_sender`（约 202-275 行）整体替换为：

```python
def resolve_sender(
    mid_hash: str,
    danmaku_contents: list[str],
    comment_uid_map: dict[str, int],
    client: BiliAPIClient,
    max_search: int = MITM_MAX_UID,
    method_map: dict | None = None,
) -> Tuple[Optional[int], str, str, Optional[dict], bool, list]:
    """
    解析发送者UID，综合多种方法

    优先级：
    1. 明文交叉验证（评论/充电名单/互动弹幕/全局库合并映射，100%准确）
    2. MITM 反查 + 消歧（候选∩明文UID集合唯一→按明文验证；否则存在性验证）
    3. 标记为未知

    Args:
        mid_hash: 弹幕中的用户标识
        danmaku_contents: 该发送者的所有弹幕内容
        comment_uid_map: 明文源合并映射 crc32->UID（评论优先，见 main.phase_resolve）
        client: API客户端
        max_search: UID 覆盖上限（MITM_MAX_UID）
        method_map: 伴随映射 mid_hash->来源方法名（充电名单/互动弹幕等），
                    缺省时命中一律记"评论区验证"（向后兼容）

    Returns:
        (uid, confidence, method, user_info, collision_risk, candidates)
        candidates: MITM 全部候选（未走 MITM 路径时为空列表），供报告/全局库判断
    """
    uid = None
    method = METHOD_UNKNOWN
    confidence = "无"
    user_info = None
    candidates: list = []

    # === 方法1：明文交叉验证（合并映射，来源由 method_map 标注） ===
    uid = cross_verify_with_comments(mid_hash, comment_uid_map)
    if uid:
        exists, user_info = verify_uid_exists(uid, client)
        if exists:
            method = (method_map or {}).get(mid_hash, METHOD_COMMENT_VERIFY)
            confidence = "高" if len(danmaku_contents) >= 2 else "中"
            return uid, confidence, method, user_info, False, candidates
        uid = None  # 理论上不会发生，但做防御

    # === 方法2：MITM 反查 + 碰撞消歧 ===
    candidates = crack_crc32(mid_hash, max_search=max_search)
    if not candidates:
        return None, confidence, method, user_info, False, candidates

    # 2a. 候选 ∩ 明文 UID 集合唯一 → 等同明文验证（最可靠的消歧）
    plain_uids = set(comment_uid_map.values())
    inter = [u for u in candidates if u in plain_uids]
    if inter:
        for u in inter:
            exists, user_info = verify_uid_exists(u, client)
            if exists:
                method = (method_map or {}).get(mid_hash, METHOD_COMMENT_VERIFY)
                confidence = "高" if len(danmaku_contents) >= 2 else "中"
                return u, confidence, method, user_info, False, candidates
        # 交集候选全部注销 → 继续走 2b 对剩余候选验证
        candidates = [u for u in candidates if u not in inter]

    # 2b. 逐个存在性验证
    if len(candidates) > 1:
        print(f"[Resolver] 注意: hash {mid_hash} 有 {len(candidates)} 个碰撞候选，逐个验证存在性")
    existing = []
    for u in candidates:
        exists, info = verify_uid_exists(u, client)
        if exists:
            existing.append((u, info))

    if len(existing) == 1:
        # 恰好 1 个存在 → 方法 CRC32破解，置信度上限"中"（沿用压置信度规则）
        uid, user_info = existing[0]
        method = METHOD_CRC32_CRACK
        confidence = "中" if len(danmaku_contents) >= 2 else "低"  # 单条弹幕碰撞风险更高
        return uid, confidence, method, user_info, True, candidates
    if len(existing) > 1:
        # 多个存在 → 取最小 UID（注册更早、更可能活跃），置信度"低"
        existing.sort(key=lambda x: x[0])
        uid, user_info = existing[0]
        method = METHOD_CRC32_CRACK
        confidence = "低"
        print(f"[Resolver] 警告: hash {mid_hash} 有 {len(existing)} 个候选同时存在，"
              f"取最小 UID:{uid}（误识别风险高）")
        return uid, confidence, method, user_info, True, candidates

    # 候选全部不存在 → 碰撞假阳性
    return None, "无", METHOD_CRC32_COLLISION, None, False, candidates
```

注意返回值从 5 元组变 **6 元组**（末尾加 candidates）。

3d. `resolve_all_senders`：签名加 `method_map: dict | None = None` 参数并透传；调用处（约 306-308 行）改为解包 6 元组；result dict 加 `"candidates": candidates`：

```python
def resolve_all_senders(
    sender_groups: dict[str, dict],
    comment_uid_map: dict[str, int],
    client: BiliAPIClient,
    max_search: int = MITM_MAX_UID,
    method_map: dict | None = None,
) -> dict[str, dict]:
```

循环体内：

```python
        uid, confidence, method, user_info, collision_risk, candidates = resolve_sender(
            mid_hash, contents, comment_uid_map, client, max_search, method_map
        )

        results[mid_hash] = {
            "uid": uid,
            "confidence": confidence,
            "method": method,
            "user_info": user_info or {},
            "danmaku_count": group["count"],
            "contents": contents,
            "collision_risk": collision_risk,
            "candidates": candidates,
            "timestamps": group["timestamps"],
            "video_times": group["video_times"],
            "colors": group["colors"],
            "pages": group["pages"],
        }
```

3e. `quick_test.py:80`：解包改 6 元组（`uid, confidence, method, _, collision_risk, _ = resolve_sender(...)`）。

3f. `config.example.py`：同步 Task 1 的 config.py 改动（删 `CRC32_MAX_SEARCH`/`CRC32_OLD_MAX`/`CRC_TABLE_PATH`/`CRC_TABLE_MAX_UID`/`CRC_BUILD_CHUNK`，加 `MITM_MAX_UID = 10_000_000_000` 及同样注释）。

- [ ] **Step 4: 运行验证**

Run: `PYTHONPATH=src .venv/bin/python /tmp/test_resolver.py && PYTHONPATH=src .venv/bin/python -m py_compile src/*.py quick_test.py && PYTHONPATH=src .venv/bin/python -c "import main"`
Expected: 全部通过。跑完 `rm /tmp/test_resolver.py`。

- [ ] **Step 5: Commit**

```bash
git add src/uid_resolver.py config.example.py quick_test.py
git commit -m "feat: uid_resolver接入MITM与碰撞消歧流水线

- crack_crc32 改返回全部候选（删旧增量搜索_fallback与彩虹表依赖）
- resolve_sender: 候选∩明文UID集合唯一交集→按明文验证；存在性验证后
  恰好1个→置信度中；多个存在→取最小UID置信度低并记录全部候选
- resolve_sender/resolve_all_senders 返回值与结果dict新增 candidates 字段
- 新增 method_map 可选参数（为P1/P2来源标注预留，缺省向后兼容）"
```

---

### Task 3: P1 — 全局 mid_hash→UID 映射库

**Files:**
- Modify: `src/storage.py`（新表 + 两个函数）
- Modify: `src/main.py:123-210`（phase_resolve 读写全局库）
- Test: 临时脚本 `/tmp/test_global_map.py`（跑完删除）

- [ ] **Step 1: 写失败的验证脚本**

写 `/tmp/test_global_map.py`：

```python
"""全局映射库验证：建表、upsert、hit_count、读取"""
import sys, os, tempfile
sys.path.insert(0, "src")

# 用临时数据库隔离测试
import config
config.DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")

import storage
storage.init_db()

# 写入
storage.save_global_uid("4200b4cd", 37704035, "评论区验证")
storage.save_global_uid("deadbeef", 123456, "充电名单")
# 重复命中 → hit_count+1，last_seen 刷新
storage.save_global_uid("4200b4cd", 37704035, "评论区验证")

m = storage.load_global_uid_map()
assert set(m.keys()) == {"4200b4cd", "deadbeef"}, m
assert m["4200b4cd"]["uid"] == 37704035
assert m["4200b4cd"]["source"] == "评论区验证"
assert m["4200b4cd"]["hit_count"] == 2, m["4200b4cd"]
assert m["deadbeef"]["hit_count"] == 1
print(f"全局映射库验证通过: {m}")
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src .venv/bin/python /tmp/test_global_map.py`
Expected: FAIL（`AttributeError: save_global_uid`）

- [ ] **Step 3: storage.py 实现**

3a. `init_db()` 的 users 表建表语句后追加：

```python
        # 全局 mid_hash→UID 映射表（跨视频复用，只增不删）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS global_uid_map (
                mid_hash TEXT PRIMARY KEY,
                uid INTEGER NOT NULL,
                source TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 1
            )
        ''')
```

3b. 文件末尾（`clear_video_cache` 之后）追加：

```python
# ========== 全局 mid_hash→UID 映射库（跨视频复用） ==========

def save_global_uid(mid_hash: str, uid: int, source: str):
    """
    upsert 全局映射：新条目 hit_count=1；重复命中 hit_count+1 并刷新 last_seen
    source: 评论区验证 / CRC32破解 / 充电名单 / 互动弹幕
    """
    now = datetime.now().isoformat()
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO global_uid_map (mid_hash, uid, source, first_seen, last_seen, hit_count)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(mid_hash) DO UPDATE SET
                uid=excluded.uid, source=excluded.source,
                last_seen=excluded.last_seen, hit_count=hit_count+1
        ''', (mid_hash, uid, source, now, now))
        conn.commit()


def load_global_uid_map() -> dict:
    """读取全局映射库：{mid_hash: {"uid": int, "source": str, "hit_count": int}}"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT mid_hash, uid, source, hit_count FROM global_uid_map")
        rows = cursor.fetchall()
    return {r["mid_hash"]: {"uid": r["uid"], "source": r["source"], "hit_count": r["hit_count"]}
            for r in rows}
```

- [ ] **Step 4: 运行验证**

Run: `PYTHONPATH=src .venv/bin/python /tmp/test_global_map.py`
Expected: PASS。跑完 `rm /tmp/test_global_map.py`。

- [ ] **Step 5: main.py phase_resolve 接入全局库**

5a. `src/main.py:17` import 行追加两个函数：

```python
from storage import clear_video_cache, update_sender_spam, save_global_uid, load_global_uid_map
```

5b. `phase_resolve` 第 2 步（unresolved 筛选）之前插入全局库加载与映射合并——将 `phase_resolve` 开头到调用 `resolve_all_senders` 之间的逻辑改为：

```python
    # 1. 从数据库加载已缓存的解析结果
    cached = load_senders(bvid)
    cached_map = {r["mid_hash"]: r for r in cached}
    print(f"[Phase 4] 数据库缓存: {len(cached_map)} 个已解析")

    # 1.5 全局 mid_hash→UID 映射库（跨视频累积）：与当视频评论映射合并，
    #     评论验证优先，全局库兜底；method_map 标注每个 mid_hash 的来源
    global_map = load_global_uid_map()
    plain_uid_map = dict(comment_uid_map)  # 评论映射复制为底
    method_map = {h: "评论区验证" for h in comment_uid_map}
    global_hit = 0
    for h, ent in global_map.items():
        if h not in plain_uid_map:
            plain_uid_map[h] = ent["uid"]
            method_map[h] = ent["source"]
            global_hit += 1
    print(f"[Phase 4] 全局映射库: {len(global_map)} 条（补充 {global_hit} 条到交叉验证映射）")
```

`resolve_all_senders(to_resolve_dict, comment_uid_map, client)` 调用（main.py:159）改为：

```python
        new_resolved = resolve_all_senders(to_resolve_dict, plain_uid_map, client,
                                           method_map=method_map)
```

5c. 保存新解析结果的循环（main.py:162-173）中，在 `save_sender(...)` 之后追加全局库写入（排除"多候选取最小"的不可靠条目，沿用"不删除数据"约定只增不删）：

```python
            # 沉淀到全局映射库：多候选取最小的碰撞风险条目不入库（spec 4.2）
            if info["uid"] is not None and not (
                    info["method"] == METHOD_CRC32_CRACK and len(info.get("candidates", [])) > 1):
                save_global_uid(mid_hash, info["uid"], info["method"])
```

- [ ] **Step 6: 端到端验证（真实数据库，隔离备份）**

```bash
cp data/profiler.db /tmp/profiler.db.bak
PYTHONPATH=src .venv/bin/python -c "
import storage
storage.init_db()
storage.save_global_uid('4200b4cd', 37704035, '评论区验证')
m = storage.load_global_uid_map()
assert m['4200b4cd']['uid'] == 37704035, m
print('真实数据库全局表读写 OK:', m)
"
PYTHONPATH=src .venv/bin/python -m py_compile src/main.py && PYTHONPATH=src .venv/bin/python -c "import main"
```

Expected: 全部通过（init_db 幂等，不影响既有数据；备份保留至 Task 7 结束确认无异常后删除）。

- [ ] **Step 7: Commit**

```bash
git add src/storage.py src/main.py
git commit -m "feat: 全局mid_hash→UID映射库（跨视频复用）

- storage: global_uid_map 表 + save/load（upsert含hit_count与last_seen）
- phase_resolve: 全局库与评论映射合并（评论优先全局兜底），method_map标注来源
- 解析成功即沉淀（多候选取最小的碰撞风险条目除外）"
```

---

### Task 4: P2a — 充电鸣谢名单明文 UID 源

**Files:**
- Modify: `src/config.py`（+`config.example.py` 同步）：新增 `CHARGE_LIST_URL`
- Modify: `src/comment.py`（新增 `fetch_charge_uid_map`）
- Modify: `src/main.py`（phase_comment 返回充电映射，phase_resolve 合并）

接口（已核实 bilibili-API-collect charge_list.md）：`GET https://api.bilibili.com/x/web-interface/elec/show`，参数 `mid`（UP主mid）+ `aid`（或 bvid），响应 `data.list[]` 含 `pay_mid`（明文）、`uname`、`rank`；`code=62001` 表示不需要展示充电信息（无充电数据，正常降级），`-404` 无视频。list 为**本月**充电用户。

- [ ] **Step 1: config 加常量**

`src/config.py` COMMENT_REPLY_URL 行后加：

```python
CHARGE_LIST_URL = "https://api.bilibili.com/x/web-interface/elec/show"  # 视频充电鸣谢名单（含明文pay_mid）
```

`config.example.py` 同样位置同步。

- [ ] **Step 2: comment.py 新增 fetch_charge_uid_map**

文件末尾追加：

```python
def fetch_charge_uid_map(bvid: str, aid: int, up_mid: int, client) -> dict:
    """
    视频充电鸣谢名单 → crc32->UID 映射（明文 UID 源，置信度同评论验证）

    充电用户是重度粉丝，与弹幕发送者重合率高。名单仅含本月充电用户；
    无充电数据（code=62001）或接口异常时返回空映射，零成本降级。

    Returns:
        {crc32_hex: uid}
    """
    uid_map = {}
    try:
        data = client.get(CHARGE_LIST_URL, params={"mid": up_mid, "aid": aid})
        if data.get("code") != 0:
            # 62001=不需要展示充电信息（无充电数据），属正常情况不告警
            if data.get("code") != 62001:
                print(f"[Comment] 充电名单获取失败: {data.get('message')}（降级跳过）")
            return {}
        for item in (data.get("data") or {}).get("list") or []:
            pay_mid = item.get("pay_mid")
            if pay_mid:
                uid_map[calc_crc32(int(pay_mid))] = int(pay_mid)
        if uid_map:
            print(f"[Comment] 充电名单: {len(uid_map)} 个明文UID")
    except Exception as e:
        print(f"[Comment] 充电名单获取异常: {e}（降级跳过）")
        return {}
    return uid_map
```

`comment.py` import 区：`from config import ...` 加 `CHARGE_LIST_URL`；新增 `from uid_resolver import calc_crc32`（注意：**会产生 comment→uid_resolver  import 方向**，而 uid_resolver 不 import comment，无循环依赖，安全）。

- [ ] **Step 3: main.py 合并充电映射**

3a. `phase_comment`（main.py:109-120）改签名并接入（up_mid 从 video_info.owner.mid 取，由调用方传入）：

```python
def phase_comment(video_info: dict, client):
    """阶段3: 采集评论 + 充电名单（失败不影响后续流程）"""
    print("\n[Phase 3/6] 采集评论区数据...")
    aid = video_info.get("aid", 0)
    if not aid:
        print("[Phase 3] 警告: 未获取到有效 aid，跳过评论采集（将仅用MITM破解）")
        return [], {}, {}, {}
    comments, comment_uid_map, comment_location_map = [], {}, {}
    try:
        comments, comment_uid_map, comment_location_map = collect_comment_data(aid, client)
    except Exception as e:
        print(f"[Phase 3] 评论采集失败 (将仅用其他来源): {e}")
    # 充电名单（独立降级：评论失败也照常尝试）
    up_mid = (video_info.get("owner") or {}).get("mid", 0)
    charge_uid_map = {}
    if up_mid:
        charge_uid_map = fetch_charge_uid_map(video_info.get("bvid", ""), aid, up_mid, client)
    return comments, comment_uid_map, comment_location_map, charge_uid_map
```

import 区加 `from comment import collect_comment_data, fetch_charge_uid_map`（替换现有 collect_comment_data 导入行）。

3b. `run_analysis`（main.py:385）调用改为：

```python
    # 阶段3: 评论 + 充电名单（comment_location_map 为 uid→IP属地，阶段6贯通进画像）
    comments, comment_uid_map, comment_location_map, charge_uid_map = phase_comment(video_info, client)
```

3c. `phase_resolve` 签名加 `charge_uid_map: dict | None = None`，在全局库合并之前插入充电名单合并（优先级：评论 > 充电 > 全局，后者不覆盖前者）：

```python
    # 1.6 充电名单合并（明文证据，置信度同评论验证；评论优先）
    charge_hit = 0
    for h, uid in (charge_uid_map or {}).items():
        if h not in plain_uid_map:
            plain_uid_map[h] = uid
            method_map[h] = "充电名单"
            charge_hit += 1
    if charge_hit:
        print(f"[Phase 4] 充电名单: 补充 {charge_hit} 条到交叉验证映射")
```

调用处（main.py:388）改为：

```python
    resolved = phase_resolve(bvid, sender_groups, comment_uid_map, client,
                             max_users=max_users, charge_uid_map=charge_uid_map)
```

（`charge_uid_map` 放 kwargs 最后，保持既有位置参数兼容。）

- [ ] **Step 4: 真实接口验证（联网，需有效 Cookie）**

```bash
PYTHONPATH=src .venv/bin/python -c "
from auth import get_auth_client
from comment import fetch_charge_uid_map
client = get_auth_client()
# bilibili-API-collect 文档示例视频：老E(mid=53456) av967773538/BV1up4y1y77i，有充电数据
m = fetch_charge_uid_map('BV1up4y1y77i', 967773538, 53456, client)
assert len(m) > 0, '示例视频应有充电名单'
import zlib
for crc, uid in list(m.items())[:3]:
    assert zlib.crc32(str(uid).encode()) == int(crc, 16), 'crc 校验失败'
print('充电名单真实接口验证 OK:', len(m), '个UID')
# 无充电视频降级验证：随便一个无充电视频应返回空映射且不报错
"
```

Expected: PASS，打印名单条数。若示例视频失效（62001），换任意近期有充电的热门视频重试。

- [ ] **Step 5: Commit**

```bash
git add src/config.py config.example.py src/comment.py src/main.py
git commit -m "feat: 充电鸣谢名单明文UID源（P2a）

- comment.fetch_charge_uid_map: elec/show接口提取pay_mid明文，62001/异常零成本降级
- phase_comment 改传 video_info（取owner.mid），返回新增充电映射
- phase_resolve 合并优先级：评论>充电名单>全局库，命中方法记'充电名单'"
```

---

### Task 5: P2b — 互动弹幕 commandDms 明文 UID 源

**Files:**
- Modify: `src/config.py`（+`config.example.py`）：新增 `DANMAKU_VIEW_URL`
- Modify: `src/danmaku.py`（新增 `fetch_command_dms` + `build_command_uid_map`）
- Modify: `src/main.py`（phase_danmaku 采集、phase_resolve 合并）

接口（已核实 bilibili-API-collect danmaku_view_proto.md）：`GET https://api.bilibili.com/x/v2/dm/web/view?type=1&oid={cid}&pid={aid}`，**需 SESSDATA**，返回 protobuf `DmWebViewReply`，field 9 = `commandDms`（repeated CommandDm，wire type 2）；CommandDm 内 field 3 = mid（varint int64）、field 4 = command（string，`#UP#`/`#LINK#`/`#ATTENTION#`）、field 5 = content（string）。wire 手写解析复用 `danmaku_history._read_varint/_skip_field`（项目先例）。

**Spec 裁剪说明（用户可否决）：** spec 5.1 说 commandDms"并入 up_analyzer"。实现仅把条目存进 `video_info["command_dms"]` 供后续使用，up_analyzer 不改动——其展示价值低，核心价值是明文 mid 交叉验证。

- [ ] **Step 1: config 加常量**

`src/config.py` DANMAKU_HISTORY_SEG_URL 行后加：

```python
DANMAKU_VIEW_URL = "https://api.bilibili.com/x/v2/dm/web/view"  # 弹幕元数据（含互动弹幕明文mid，需SESSDATA）
```

`config.example.py` 同步。

- [ ] **Step 2: danmaku.py 新增解析函数**

文件末尾追加：

```python
# ========== 互动弹幕（commandDms，含明文 mid） ==========

def _parse_command_dm(data: bytes) -> dict:
    """解析单个 CommandDm 嵌套消息：mid=3(varint), command=4, content=5"""
    fields = {}
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        field_no, wire_type = tag >> 3, tag & 0x07
        if wire_type == 0:
            value, pos = _read_varint(data, pos)
            fields[field_no] = value
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            fields[field_no] = data[pos:pos + length]
            pos += length
        else:
            pos = _skip_field(data, pos, wire_type)

    def _s(field_no: int) -> str:
        raw = fields.get(field_no, b"")
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else ""

    return {
        "mid": fields.get(3, 0),
        "command": _s(4),
        "content": _s(5),
    }


def fetch_command_dms(video_info: dict, client: BiliAPIClient) -> list[dict]:
    """
    采集互动弹幕 commandDms（UP主头像弹幕/关联视频/引导关注，含明文 mid）

    接口需 SESSDATA；protobuf wire 手写解析（复用 danmaku_history 先例）。
    多分P视频只采集第一P（互动弹幕按 cid 维度，主价值是UP主mid）。
    失败降级返回空列表，不影响主流程。

    Returns:
        [{"mid": int, "command": str, "content": str}]
    """
    try:
        cid = get_cid_for_page(video_info, 0)
        aid = video_info.get("aid", 0)
        if not cid:
            return []
        raw = client.get_raw(DANMAKU_VIEW_URL, params={"type": 1, "oid": cid, "pid": aid})
        items = []
        pos = 0
        while pos < len(raw):
            tag, pos = _read_varint(raw, pos)
            field_no, wire_type = tag >> 3, tag & 0x07
            if field_no == 9 and wire_type == 2:  # commandDms
                length, pos = _read_varint(raw, pos)
                items.append(_parse_command_dm(raw[pos:pos + length]))
                pos += length
            else:
                pos = _skip_field(raw, pos, wire_type)
        if items:
            print(f"[Danmaku] 互动弹幕: {len(items)} 条（含明文mid）")
        return items
    except Exception as e:
        print(f"[Danmaku] 互动弹幕获取失败: {e}（降级跳过）")
        return []


def build_command_uid_map(command_dms: list[dict]) -> dict:
    """互动弹幕明文 mid → crc32->UID 映射"""
    uid_map = {}
    for item in command_dms:
        mid = item.get("mid")
        if mid:
            uid_map[calc_crc32(int(mid))] = int(mid)
    return uid_map
```

import 区追加：`from config import DANMAKU_VIEW_URL`（并入现有 config 导入）、`from danmaku_history import _read_varint, _skip_field`（加注释"# 复用历史弹幕的 wire 解析"）、`from uid_resolver import calc_crc32`（uid_resolver→danmaku 无依赖，无循环）。

注意：`client.get_raw` 当前无重试（已知 Minor 项），此处一次请求失败即降级，可接受。

- [ ] **Step 3: main.py 接线**

3a. `phase_danmaku` 末尾 return 前采集互动弹幕并存入 video_info（先找到该函数的 return 语句，在其前插入）：

```python
    # 互动弹幕（含明文mid，需SESSDATA；失败降级不影响主流程）
    command_dms = fetch_command_dms(video_info, client)
    video_info["command_dms"] = command_dms
```

`phase_danmaku` 的返回值改为 4 元组 `return video_info, danmaku_list, sender_groups, command_dms`；`run_analysis` 调用处（main.py:376）改为：

```python
    video_info, danmaku_list, sender_groups, command_dms = phase_danmaku(bvid, client)
```

danmaku import 行加 `fetch_command_dms, build_command_uid_map`。

3b. `phase_resolve` 签名加 `command_uid_map: dict | None = None`，在充电名单合并后插入（优先级：评论 > 充电 > 互动弹幕 > 全局）：

```python
    # 1.7 互动弹幕明文mid合并（基本只有UP主；评论/充电优先）
    cmd_hit = 0
    for h, uid in (command_uid_map or {}).items():
        if h not in plain_uid_map:
            plain_uid_map[h] = uid
            method_map[h] = "互动弹幕"
            cmd_hit += 1
    if cmd_hit:
        print(f"[Phase 4] 互动弹幕: 补充 {cmd_hit} 条到交叉验证映射")
```

`run_analysis` 的 phase_resolve 调用处改为：

```python
    resolved = phase_resolve(bvid, sender_groups, comment_uid_map, client,
                             max_users=max_users, charge_uid_map=charge_uid_map,
                             command_uid_map=build_command_uid_map(command_dms))
```

- [ ] **Step 4: 真实接口验证（联网）**

```bash
PYTHONPATH=src .venv/bin/python -c "
from auth import get_auth_client
from danmaku import get_video_info, fetch_command_dms, build_command_uid_map
client = get_auth_client()
# bilibili-API-collect 文档示例：av797164471 有1条#UP#互动弹幕（mid=501183549）
info = get_video_info('BV1vJ411C7kR', client) if False else None
# 示例av号的BV号不确定，改用直接构造 video_info
vi = {'aid': 797164471, 'cid': 236871317, 'pages': [{'cid': 236871317}]}
items = fetch_command_dms(vi, client)
print('互动弹幕:', items)
assert len(items) >= 1 and items[0]['mid'] == 501183549, '应解析出示例互动弹幕'
m = build_command_uid_map(items)
import zlib
assert m[format(zlib.crc32(b'501183549'), '08x')] == 501183549
print('互动弹幕真实接口验证 OK')
"
```

Expected: PASS（示例视频互动弹幕长期稳定；若失效，改找一个有 `#UP#` 弹幕的视频验证，并在 commit message 注明）。同时跑 `PYTHONPATH=src .venv/bin/python -m py_compile src/*.py && PYTHONPATH=src .venv/bin/python -c "import main"`。

- [ ] **Step 5: Commit**

```bash
git add src/config.py config.example.py src/danmaku.py src/main.py
git commit -m "feat: 互动弹幕commandDms明文UID源（P2b）

- danmaku.fetch_command_dms: dm/web/view手写wire解析field9 commandDms，提取明文mid
- build_command_uid_map 并入交叉验证映射（优先级：评论>充电>互动弹幕>全局库）
- command_dms 条目存入 video_info 供后续使用"
```

---

### Task 6: P3 — 评论采集扩容

**Files:**
- Modify: `src/config.py:69-70`（+`config.example.py` 同步）
- Modify: `src/comment.py`（fetch_comments 截断警告）

- [ ] **Step 1: 调整常量**

`src/config.py`：

```python
MAX_COMMENT_PAGES = 100      # 评论最大翻页数（约20条/页，上限~2000条主评论）
COMMENT_REPLY_MAX_PAGES = 25  # 每条主评论的子评论最多补采页数（pn分页，每页20条，上限500条）
```

`config.example.py` 同步。

- [ ] **Step 2: fetch_comments 截断警告**

读 `src/comment.py` 的 `fetch_comments`（约 201 行起），找到翻页循环因"达到 max_pages"（而非真重复页/接口失败）正常结束的位置，在函数 return 前加截断提示。实现方式：循环结束处记录是否因上限截断，例如：

```python
    # 在 for page in range(1, max_pages + 1) 循环中，break 的分支均设置 truncated = False；
    # 循环完整跑完（未 break）时 truncated = True
    if truncated:
        print(f"[Comment] 已达采集上限 {max_pages} 页，评论区可能未采完（可调大 MAX_COMMENT_PAGES）")
```

子评论补采 `_fetch_sub_replies`（约 62-97 行）同样在被 `COMMENT_REPLY_MAX_PAGES` 截断时（`pn * 20 < total` 且循环耗尽）打印一次：

```python
        print(f"[Comment] 子评论补采达上限 {COMMENT_REPLY_MAX_PAGES} 页 (root={root_rpid} rcount={total})，剩余截断")
```

（该行已有 rcount 截断的相关逻辑，警告加在既有截断分支内，不新增控制流。）

- [ ] **Step 3: 验证**

```bash
PYTHONPATH=src .venv/bin/python -m py_compile src/comment.py src/config.py
PYTHONPATH=src .venv/bin/python -c "from config import MAX_COMMENT_PAGES, COMMENT_REPLY_MAX_PAGES; assert (MAX_COMMENT_PAGES, COMMENT_REPLY_MAX_PAGES) == (100, 25); print('P3 常量 OK')"
```

- [ ] **Step 4: Commit**

```bash
git add src/config.py config.example.py src/comment.py
git commit -m "feat: 评论采集扩容（P3）

- MAX_COMMENT_PAGES 20→100，COMMENT_REPLY_MAX_PAGES 5→25
- 达上限截断时打印明确警告（评论是量最大的明文UID源）"
```

---

### Task 7: 端到端验证 + 文档更新 + 合并

**Files:**
- Modify: `README.md`、`AGENTS.md`、`docs/bilibili_api_reference.md`

- [ ] **Step 1: 全量编译与导入检查**

```bash
PYTHONPATH=src .venv/bin/python -m py_compile src/*.py *.py
PYTHONPATH=src .venv/bin/python -c "
import importlib
for m in ['config','api_client','auth','danmaku','danmaku_history','comment','uid_resolver','crc_rainbow','spam_detector','user_collector','profile_analyzer','llm_analyzer','up_analyzer','report','exporter','storage','main']:
    importlib.import_module(m)
print('全部模块导入 OK')
"
grep -rn "CRC32_MAX_SEARCH\|CRC_TABLE\|build_table\|table_exists\|_crack_crc32_fallback" --include="*.py" src/ *.py || echo "旧符号已全部清除"
```

Expected: 导入 OK；grep 无残留（最后一行输出"旧符号已全部清除"）。

- [ ] **Step 2: MITM 全链路冒烟（quick_test --top 5，真实联网）**

```bash
.venv/bin/python quick_test.py BV1vu4y1b7Y9 --top 5 2>&1 | tail -40
```

验证点（对照改进前：2696 发送者中 top2 仅 1 人解析成功且为 CRC32破解/中）：
- top5 发送者全部或大部分解析成功；
- 方法分布出现"评论区验证/充电名单/CRC32破解"，CRC32破解均带 candidates；
- 无 traceback；报告正常生成。

- [ ] **Step 3: 全局库复用验证**

对**另一个视频**（同一 UP 主最佳）再跑一次 `quick_test.py --top 3`，日志 `[Phase 4] 全局映射库: N 条` 应显示 N>0，且控制台出现来源标注（充电名单/互动弹幕/评论区验证）。然后用 sqlite 抽查：

```bash
PYTHONPATH=src .venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('data/profiler.db')
rows = conn.execute('SELECT source, COUNT(*), SUM(hit_count) FROM global_uid_map GROUP BY source').fetchall()
print('全局库分布:', rows)
assert sum(r[1] for r in rows) > 0
"
```

- [ ] **Step 4: 文档更新**

4a. `AGENTS.md`：
- 代码结构表 `crc_rainbow.py` 行改为：`├── crc_rainbow.py       # MITM中间相遇CRC32反查（10万条内存小表，覆盖全部≤10位UID，秒级）`
- `uid_resolver.py` 行改为：`├── uid_resolver.py        # mid_hash 破解：评论/充电/互动弹幕/全局库交叉验证 + MITM 反查消歧`
- 数据流段落中"已解析的 sender"缓存描述后补一句：`全局映射库 global_uid_map 跨视频沉淀可靠 mid_hash→UID 映射（多候选碰撞条目不沉淀），随使用次数提升解析率。`
- `storage.py` 行末尾补 `+ 全局映射库`。

4b. `README.md`：grep `彩虹表|5000万|破解` 找到相关描述，更新为 MITM 口径（覆盖全部 ≤10 位 UID、无需构建大表）；特性列表补充"充电名单/互动弹幕明文交叉验证"与"评论扩容至100页"。

4c. `docs/bilibili_api_reference.md`：追加两个接口条目（按该文件既有格式）：
- `GET /x/web-interface/elec/show`（视频充电鸣谢名单，参数 mid+aid，data.list[].pay_mid 明文，62001=无充电数据）
- `GET /x/v2/dm/web/view`（弹幕元数据 protobuf，需 SESSDATA，field9 commandDms 含明文 mid，仅 ≤1 次/视频）

4d. `docs/superpowers/plans/2026-08-08-uid-resolution-improvement.md`（本计划）勾选已完成步骤。

- [ ] **Step 5: 最终 commit + 合并 main**

```bash
git add README.md AGENTS.md docs/bilibili_api_reference.md docs/superpowers/plans/2026-08-08-uid-resolution-improvement.md
git commit -m "docs: MITM反查与新UID源的文档更新（README/AGENTS/API参考）"
git checkout main && git merge upgrade/uid-resolution --no-edit && git branch -d upgrade/uid-resolution
rm -f /tmp/profiler.db.bak
```

---

## Self-Review 记录（计划作者已核对）

- Spec 覆盖：P0→Task1-2，P1→Task3，P2→Task4-5，P3→Task6，验证方式→Task7；spec 3.2 的"本地 crc_table.bin 用户手动删除"已写入 Task1 commit message；spec 5.1"并入 up_analyzer"裁剪为存 video_info（Task5 已标注，用户可否决）。
- 类型一致性：`resolve_sender` 6 元组返回在 Task2 定义，quick_test.py 同步；`candidates` 字段在 Task2 加入 result dict，Task3 的全局库过滤条件引用它；`method_map` 参数贯穿 Task2-5 签名一致；`plain_uid_map` 在 Task3 定义、Task4/5 复用同名变量。
- 中间态说明：Task1 完成后 uid_resolver 暂时 import 失败，Task2 修复，两任务须连续执行（已在 Task1 Step3 注明）。
