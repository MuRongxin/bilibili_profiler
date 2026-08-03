# 阶段 1：安全与数据正确性修复 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除会让分析结论错误或凭证泄露的问题（对应审查报告 Critical #1/#5/#6/#8 及相关 Important 项）。

**Architecture:** 保持现有扁平模块结构；所有修复为局部改动，不改任何模块接口（除 Task 8 的字段改名，需同步所有消费方）。

**Tech Stack:** Python 3 标准库（html、re、json、os、tempfile）。

**上游文档：** 路线图 `docs/superpowers/plans/2026-08-03-upgrade-roadmap.md` 阶段 1；缺陷细节 `docs/code_review_2026-08-03.md`。

**测试约定（重要）：** 项目无单元测试框架（AGENTS.md 明确），**不引入 pytest**。每个任务的验证方式为：
1. 任务描述中给出的 `python -c "..."` 内联断言（在仓库根目录、先 `source .venv/bin/activate` 后运行；涉及 src 模块的用 `PYTHONPATH=src python -c ...`）
2. 全部任务完成后由控制器统一跑 `python quick_test.py` 冒烟（需真实网络与 Cookie）

**通用约束：**
- 所有注释、print 输出、commit message 用中文
- 模块间扁平导入（`from config import ...`），不得加 `src.` 前缀
- 每任务一个 commit，message 格式：`fix: <中文描述>`（conventional commits）
- 不得提交 `data/`、`src/config.py` 之外的敏感文件；`src/config.py` 本身被 gitignore，改动它时确认 `git status` 中不出现它

---

### Task 1: 移除硬编码 LLM Key + README 免责声明

**Files:**
- Modify: `src/config.py`（约第 67 行）
- Modify: `README.md`

**背景：** `src/config.py` 第 67 行附近 `LLM_API_KEY = os.environ.get("LLM_API_KEY", "<真实Key>")` 把可用密钥硬编码为默认值。该 Key 应视为已泄露（用户已被告知需自行去服务商后台轮换，本任务只移除源码中的默认值）。

- [ ] **Step 1:** 将默认值改为空字符串：

```python
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
```

（保持 `os.environ.get` 形式不变，仅替换默认值。）

- [ ] **Step 2:** 确认 `src/llm_analyzer.py:21` 附近的 `LLM_API_KEY or os.environ.get(...)` 冗余回退不受影响（空 Key 时 `main.py` 的 `phase_ai_analysis` 会自动跳过，这是既有行为，不要改）。

- [ ] **Step 3:** 验证：`PYTHONPATH=src python -c "from config import LLM_API_KEY; assert LLM_API_KEY == '', '默认值应为空'"`（在 unset LLM_API_KEY 的 shell 中运行）

- [ ] **Step 4:** 在 `README.md` 末尾追加（保持 README 现有语言风格）：

```markdown
## 免责声明

本项目仅供个人学习与研究用途。使用者应遵守 bilibili 用户协议与相关法律法规，不得将本工具用于任何侵犯他人隐私、批量爬取牟利或其他违法违规用途。使用本项目产生的一切后果由使用者自行承担。
```

- [ ] **Step 5:** 确认 `git status` 中 `src/config.py` 不出现（它被 gitignore），`git add README.md` 后提交：`docs: 移除硬编码LLM Key默认值并补充免责声明`

---

### Task 2: 修复纯数字 mid_hash 被误判损毁

**Files:**
- Modify: `src/danmaku.py`（约第 45–55 行）

**背景：** `danmaku.py` 约 49–51 行：

```python
if uid_raw.isdigit():
    mid_hash = format(int(uid_raw), "08x")
```

约 2.3% 的 CRC32 hash 恰好全是十进制数字（如 `12345678`），会被误当作"明文数字 UID"转码成完全不同的值，对应发送者永远无法解析。`uid_hint` 补救路径不存在（`group_by_sender` 不保存它）。

- [ ] **Step 1:** 阅读 `src/danmaku.py` 全文，找到该分支及其上下文（含 `uid_hint` 相关代码）。

- [ ] **Step 2:** 删除 `isdigit()` 特判分支，统一按 hex 处理：`mid_hash = uid_raw.lower()`。若 `uid_hint` 变量因此成为死代码，一并删除（先 grep 确认 `uid_hint` 在 danmaku.py 之外无消费方）。

- [ ] **Step 3:** 验证（构造含纯数字 hash 的 XML 内联测试）：

```bash
PYTHONPATH=src python -c "
from danmaku import parse_danmaku_xml
xml = '''<?xml version=\"1.0\"?><i><d p=\"100.5,1,25,16777215,1700000000,0,12345678,0\">测试弹幕</d><d p=\"101.0,1,25,16777215,1700000001,0,abcdef01,0\">第二条</d></i>'''
result = parse_danmaku_xml(xml)
hashes = sorted(d['mid_hash'] for d in result)
assert hashes == ['12345678', 'abcdef01'], hashes
print('OK: 纯数字hash不再被转码', hashes)
"
```

（注意：`parse_danmaku_xml` 的确切函数名与返回结构以 `src/danmaku.py` 实际代码为准，若签名不同请按实际调整断言。）

- [ ] **Step 4:** 提交：`fix: 纯数字mid_hash不再被误判为明文UID转码`

---

### Task 3: CRC32 暴力破解置信度压级 + 碰撞风险标注

**Files:**
- Modify: `src/uid_resolver.py`（约第 132–210 行的 `resolve_sender`）
- Modify: `src/report.py`（用户卡片/汇总中展示解析置信度的位置）

**背景：** 实测 CRC32 碰撞使暴力破解可能返回错误 UID（`calc_crc32(1)` 破解返回 1146140827），碰撞 UID 往往真实存在，`verify_uid_exists` 挡不住。当前弹幕数 ≥5 就标"高"置信度（`uid_resolver.py:192-193`），置信度只反映弹幕数量，与破解可靠性无关。

- [ ] **Step 1:** 阅读 `src/uid_resolver.py` 全文，理清 `resolve_sender` 两条路径（评论区交叉验证 vs 暴力破解）的返回结构。

- [ ] **Step 2:** 修改暴力破解路径：
  - 置信度上限压到"中"：无论弹幕数多少，暴力破解来源的结果置信度不得为"高"（交叉验证路径不变，仍可为"高"）
  - 返回结构中新增字段 `collision_risk: True`（暴力路径）/ `False`（交叉验证路径）。若返回是 dict 直接加键；若是元组（如 4 元组），改为 dict 或追加第 5 元素——**先 grep `resolve_sender` 的全部调用方**（`main.py`、`quick_test.py`），同步修改解包代码，保持调用方可用

- [ ] **Step 3:** `src/report.py` 中找到渲染解析置信度的位置，为 `collision_risk=True` 的用户追加一个"可能误识别"徽标（如 `<span class="badge-warn">可能误识别</span>`，样式复用现有 badge CSS，没有合适样式就加一个简单内联 style）。**注意：本任务的 report.py 改动只需文本插入，HTML 转义统一由 Task 4 处理，不要提前做转义改造。**

- [ ] **Step 4:** 验证：

```bash
PYTHONPATH=src python -c "
from uid_resolver import calc_crc32, crack_crc32
h = calc_crc32(1)
cracked = crack_crc32(h)
print('碰撞实证: uid=1 的hash被破解为', cracked)
" 
```

（碰撞存在是已知事实，此步骤仅为记录；核心验证是置信度逻辑——构造 mock 调用确认暴力路径返回"中"且 `collision_risk=True`。可用 `python -c` 内联 mock `verify_uid_exists` 后调用 `resolve_sender`。）

- [ ] **Step 5:** 提交：`fix: CRC32暴力破解置信度压级并标注碰撞误识别风险`

---

### Task 4: report.py 全量 XSS 转义 + 粗体渲染修复

**Files:**
- Modify: `src/report.py`

**背景：** 报告把被分析用户可控的字符串（弹幕内容、昵称、签名、头像 URL、UP主标题等）几乎原样插入 HTML，仅 AI 文本有一处转义。这是存储型 XSS。审查确认的问题点：`:44` 弹幕内容、`:26` 标签、`:135` 头像 URL 与 alt、`:143` 用户名、`:149` 签名、`:178/180/184` 收藏夹名/视频标题/番剧名、`:94/98` title 属性注入、`:255/364` 视频标题、`:247/414` 词云 JSON 的 `</script>` 逃逸。另有 `:122` 的 `**粗体**` 只处理第一对、奇数个 `**` 导致后续全文变粗。

- [ ] **Step 1:** 阅读 `src/report.py` 全文（514 行），列出所有把外部数据插入 HTML 的位置。

- [ ] **Step 2:** 文件顶部（import 区后）加转义工具：

```python
import html as _html
from urllib.parse import urlparse

def esc(s):
    """HTML 转义用户可控文本（含引号，防属性注入）"""
    return _html.escape(str(s) if s is not None else "", quote=True)

def safe_url(url):
    """URL 白名单校验：仅允许 http/https，其余返回空串"""
    url = str(url) if url else ""
    if urlparse(url).scheme in ("http", "https"):
        return esc(url)
    return ""

def js_json(obj):
    """json.dumps 后转义 </，防 </script> 截断逃逸"""
    import json
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")
```

- [ ] **Step 3:** 所有用户可控文本插入点统一走 `esc()`；URL 属性（头像 src、链接 href）走 `safe_url()`；所有进入 `<script>` 块的 `json.dumps` 改走 `js_json()`（含 `:247` 词云、`:481/487/493` Chart.js 数据）。模板内部已安全拼接的静态字符串不要动。

- [ ] **Step 4:** 修复 AI 文本粗体渲染（约 `:121-124`）：先 `esc()` 再 `re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)` 替换现有的两次 `.replace("**", ...)` 写法。

- [ ] **Step 5:** 验证：

```bash
PYTHONPATH=src python -c "
from report import esc, safe_url, js_json
assert esc('<script>alert(1)</script>') == '&lt;script&gt;alert(1)&lt;/script&gt;'
assert esc('\"onmouseover=\"x') == '&quot;onmouseover=&quot;x'
assert safe_url('javascript:alert(1)') == ''
assert safe_url('https://i0.hdslb.com/a.jpg') == 'https://i0.hdslb.com/a.jpg'
assert '</script>' not in js_json({'w': 'x</script><script>alert(1)</script>'})
import re
safe = esc('**粗1** 普通 **粗2**')
out = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', safe)
assert out.count('<strong>') == 2 and '**' not in out, out
print('OK: 转义/URL/JSON/粗体全部通过')
"
```

另用 grep 自查：`grep -n '{name}\|{c}\|{sign}\|{title}' src/report.py` 剩余的未转义插值点应逐个确认是静态/已转义数据。

- [ ] **Step 6:** 提交：`fix: 报告HTML全量转义用户数据修复存储型XSS`

---

### Task 5: 修复 WBI 重签名残留旧 w_rid

**Files:**
- Modify: `src/api_client.py`（`_sign_wbi`，约第 54–65 行）

**背景：** `-403` 恢复路径用含旧 `w_rid` 的 params 重新签名，旧 `w_rid` 被拼进待签名串，新签名必然无效，恢复路径成为死代码。

- [ ] **Step 1:** 在 `_sign_wbi` 开头剔除残留字段：

```python
params = {k: v for k, v in params.items() if k not in ("w_rid", "wts")}
```

- [ ] **Step 2:** 验证：

```bash
PYTHONPATH=src python -c "
from api_client import BiliAPIClient
c = BiliAPIClient.__new__(BiliAPIClient)
c._wbi_key = ('' , '')  # 若 _sign_wbi 依赖密钥则注入测试值，以实际实现为准
# 核心断言：传入含 w_rid/wts 的 params，签名串中不得出现旧 w_rid
" 
```

（`_sign_wbi` 的确切签名与密钥注入方式以实际代码为准；断言要点：同一 params 加不同旧 `w_rid` 重签，两次 `w_rid` 结果应相同，证明旧值未参与签名。）

- [ ] **Step 3:** 提交：`fix: WBI重签名剔除残留w_rid/wts修复-403恢复死代码`

---

### Task 6: cookie 安全写入 + login 脚本保存 refresh_token

**Files:**
- Modify: `src/auth.py`（`save_cookie` 约 55–70 行、`load_cookie` 约 30–50 行）
- Modify: `login.py`（约 66–94 行）、`login_bg.py`（约 49–53 行）

**背景：** ①cookie.json 直接 `json.dump` 非原子，写入中断会留下截断文件且 `load_cookie` 无容错直接崩溃；文件权限默认 644 过宽（cookie=账号凭证）。②`login.py`/`login_bg.py` 扫码成功分支未提取 `data.refresh_token`，经这两条路径登录的用户自动刷新失效。

- [ ] **Step 1:** `save_cookie` 改原子写入 + 权限收紧：

```python
import tempfile

def save_cookie(client):
    # ... 既有构造 cookie_dict 的逻辑不变（含 _refresh_token 处理）...
    dir_ = os.path.dirname(COOKIE_PATH) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cookie_dict, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, COOKIE_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
```

- [ ] **Step 2:** `load_cookie` 容错：捕获 `json.JSONDecodeError`（与 `FileNotFoundError` 同级处理），打印"cookie 文件损坏，需重新登录"并返回 None。

- [ ] **Step 3:** `login.py` 与 `login_bg.py` 的 `code == 0` 成功分支，在 `save_cookie(client)` 前加（对齐 `src/auth.py:219-221` 主流程写法）：

```python
rt = result.get("data", {}).get("refresh_token", "")
if rt:
    client._refresh_token = rt
```

（变量名以两个脚本实际代码为准。）

- [ ] **Step 4:** 验证：

```bash
PYTHONPATH=src python -c "
import os, stat, json
from config import COOKIE_PATH
# save/load 往返 + 权限检查（用一个 mock client，以 auth.py 实际接口为准）
# 损坏文件容错：写入坏 JSON 后 load_cookie 应返回 None 而非抛异常
import auth
with open(COOKIE_PATH + '.bak', 'w') as f: pass  # 以实际测试逻辑为准
print('OK')
"
```

（实现后按实际接口写真实断言：①save 后 `stat.S_IMODE(os.stat(COOKIE_PATH).st_mode) == 0o600`；②写入 `'{broken'` 后 `load_cookie()` 返回 None。测试结束恢复/删除测试 cookie 文件，**不要动用户真实的 `data/cookie.json`**——测试时 monkeypatch `COOKIE_PATH` 到临时路径。）

- [ ] **Step 5:** 提交：`fix: cookie原子写入收紧权限并修复login脚本refresh_token丢失`

---

### Task 7: comment.py 防御性修复

**Files:**
- Modify: `src/comment.py`（约第 52、75、111 行）

**背景：** ①`member.get("level_info", {})` 在 `level_info` 为 `None` 时抛 `AttributeError`；`data["data"]` 为 `None` 同理。②`uid_map[crc] = uid` 在两评论用户 CRC32 碰撞时后到者静默覆盖先到者，造成误归属。

- [ ] **Step 1:** None 防御统一改为 `(member.get("level_info") or {}).get(...)` 形式；`data.get("data") or {}` 同理。

- [ ] **Step 2:** CRC 碰撞保留先见者并告警：

```python
if crc in uid_map and uid_map[crc] != uid:
    print(f"[评论] 警告: CRC32碰撞 {crc}，已归属UID {uid_map[crc]}，忽略 {uid}")
else:
    uid_map.setdefault(crc, uid)
```

- [ ] **Step 3:** 验证：`PYTHONPATH=src python -c` 构造含 `level_info: None` 的 mock member 走解析函数不抛异常（函数名以实际代码为准）。

- [ ] **Step 4:** 提交：`fix: 评论解析level_info空值防御与CRC碰撞保留先见者`

---

### Task 8: account_age_days 改名为 oldest_activity_days

**Files:**
- Modify: `src/profile_analyzer.py`（约第 206–212 行）
- Modify: `src/report.py`（消费该字段处）
- Modify: `src/llm_analyzer.py`（prompt 中引用该字段处）

**背景：** 该字段实为"采样到的最早一条动态/内容的年龄"（最多翻 10 页动态），对老用户远小于真实账号年龄。名不副实会误导 LLM 人格推断与报告读者。

- [ ] **Step 1:** 全文 grep `account_age_days`（含 `account_age` 变体），确认所有生产方与消费方。

- [ ] **Step 2:** 统一改名为 `oldest_activity_days`；LLM prompt 中的中文描述同步改为"最早动态距今（天）"或类似的准确表述；报告页面该指标的展示文案同步改为"最早动态"类表述（不要写成"账号年龄/注册时长"）。

- [ ] **Step 3:** 验证：`grep -rn "account_age" src/ quick_test.py` 应无残留；`PYTHONPATH=src python -c "from profile_analyzer import analyze_profile"` 导入不报错。

- [ ] **Step 4:** 提交：`fix: account_age_days改名oldest_activity_days消除语义误导`

---

## Self-Review 记录

- Spec 覆盖：路线图阶段 1 全部 9 项 → Task 1–8 全覆盖（cookie 权限/原子、login refresh_token 合并为 Task 6；comment 两项合并为 Task 7）。
- 冲突管理：Task 3/4/8 都改 report.py，**必须按 3→4→8 顺序执行**（Task 4 的转义改造会改动 Task 3 加的徽标行，Task 3 故意不做转义）。
- 类型一致性：Task 3 若改 `resolve_sender` 返回结构，调用方（main.py、quick_test.py）必须同任务内同步。

## 全部任务完成后（控制器执行）

- [ ] 跑 `python quick_test.py` 冒烟验证（需真实网络与 Cookie）
- [ ] 派发最终整体代码审查（base=main, head=HEAD）
- [ ] 合并回 main 或开下一阶段
