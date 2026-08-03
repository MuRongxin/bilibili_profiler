# 阶段 2：网络层重构（风控感知 + 线程安全）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 把 `api_client.py` 重构为线程安全、风控感知的统一客户端，为阶段 4 并发采集与阶段 5 全量采集打底。

**Architecture:** 保持 `BiliAPIClient` 单一入口；限速/重试/降级全部收敛到 `_request_locked` + `get/post/get_raw` 三个出口；`auth.py` 的 cookie 刷新改走客户端。

**上游文档：** 路线图阶段 2；`docs/bilibili_api_reference.md` 第 1 章（WBI 规范、buvid、bili_ticket、风控）；审查报告 Important 项。

**测试约定：** 无 pytest，用 `PYTHONPATH=src .venv/bin/python -c "..."` 内联断言；不得发真实请求（mock session 或 `__new__` 绕过 __init__ 构造实例，除非任务明确允许真实请求验证）。每任务一个 commit。

**注意：** `src/config.py` 被 gitignore——新增配置常量的改动不会也不应提交，在 commit 中只提交其他文件，并在报告中说明。

**关键背景（控制器已读源码确认）：**
- `api_client.py` 全文 150 行：`_sign_wbi`（54-66）已剔除残留 w_rid/wts（阶段1）；`get()`（93-140）有 -412/-403 处理但退避短、最后一次 raise；`_sleep_if_needed`（85-91）无锁；`_get_wbi_key`（39-52）与 `_ensure_buvid3`（68-83）直接用 `session.get` 无限速；`get_raw`（142-144）无重试。
- `auth.py`：`_check_needs_refresh`（106-116）与 `_try_refresh_cookie`（128+）直接用 `client.session.get/post` 绕过限速；correspond 步骤需要 HTML 文本（非 JSON）。
- WBI 规范要点（调研实证）：过滤 value 中 `!'()*`；urlencode 百分号大写、空格 `%20`；`img_key/sub_key` 全站统一**每日更替**；签名失效返回 -352（评论 wbi 接口 -403）+ `v_voucher`。
- Python `urllib.parse.quote(s, safe='')` 默认即产生大写百分号、空格 `%20`，符合规范。

---

### Task 1: WBI 签名规范化 + 密钥每日刷新 + -352 处理

**Files:**
- Modify: `src/api_client.py`（`_sign_wbi` 54-66、`_get_wbi_key` 39-52、`get()` 的 -403 分支 114-118）

**Step 1:** `_sign_wbi` 按规范改造：

```python
from urllib.parse import quote

def _sign_wbi(self, params: dict) -> dict:
    """为 WBI 接口参数添加签名（规范：过滤 !'()*，urlencode 大写百分号，空格 %20）"""
    key = self._get_wbi_key()
    if not key:
        return params
    # 剔除残留的旧签名参数（-403/-352 恢复路径会传入旧值）
    params = {k: v for k, v in params.items() if k not in ("w_rid", "wts")}
    params["wts"] = int(time.time())
    items = []
    for k in sorted(params.keys()):
        # 规范要求过滤 value 中的 !'()* 字符后再编码
        v = "".join(ch for ch in str(params[k]) if ch not in "!'()*")
        items.append(f"{k}={quote(v, safe='')}")
    param_str = "&".join(items)
    params["w_rid"] = hashlib.md5((param_str + key).encode()).hexdigest()
    return params
```

**Step 2:** 密钥每日刷新：`__init__` 加 `self._wbi_key_date = None`；`_get_wbi_key` 中缓存命中前检查 `self._wbi_key_date == time.strftime("%Y-%m-%d")`，不一致则重新获取并更新日期。

**Step 3:** `get()` 中 -403 分支扩展为 `data.get("code") in (-352, -403)`：清 `_wbi_key`（和 `_wbi_key_date`）强制刷新重签，重签前加 `time.sleep(RETRY_BACKOFF)` 退避（当前无退避连续重发易加重风控）。

**Step 4:** 验证（`__new__` 构造实例，注入 `c._wbi_key = "a"*32`、`c._wbi_key_date` 为今天）：

```bash
PYTHONPATH=src .venv/bin/python -c "
from api_client import BiliAPIClient
c = BiliAPIClient.__new__(BiliAPIClient)
import time
c._wbi_key = 'a'*32; c._wbi_key_date = time.strftime('%Y-%m-%d')
p = c._sign_wbi({'mid': 123, 'foo': \"a!b'c(d)e*f g\"})
# 断言：w_rid 为32位hex；过滤与编码生效（手动复算同一签名串对比）
"
```

复算断言：手工构造期望签名串 `foo=a%20bcde%20f%20g`（!'()* 被过滤、空格 %20）+ `mid=123&wts=...` 排序拼接，md5 比对一致。

**Step 5:** 提交：`feat: WBI签名按最新规范改造并支持密钥每日刷新与-352恢复`

---

### Task 2: 线程安全限速器

**Files:**
- Modify: `src/api_client.py`（`__init__`、`_sleep_if_needed`、新增 `_request_locked`，`get/get_raw` 改走它）

**设计（控制器已定，照此实现）：**
- `__init__` 加 `self._lock = threading.RLock()`（RLock：`_get_wbi_key`/`_ensure_buvid3` 在持有锁的请求路径中可能嵌套发请求）。
- 新增：

```python
def _request_locked(self, method: str, url: str, **kwargs) -> requests.Response:
    """限速与请求发出原子化（线程安全）；冷却 sleep 在锁外，不阻塞其他线程"""
    with self._lock:
        self._sleep_if_needed(url)
        return self.session.request(method, url, timeout=15, **kwargs)
```

- `get()` 重试循环内改为 `resp = self._request_locked("GET", url, params=params, headers=merged_headers, **kwargs)`；`get_raw` 同理；`_get_wbi_key` 和 `_ensure_buvid3` 中的裸 `session.get` 也改走 `_request_locked`（这两个方法本身不加锁，靠 RLock 嵌套）。
- docstring 注明：`BiliAPIClient` 线程安全，限速为全局限速（所有线程共享同一速率）。

**验证：** 5 线程并发各发 3 个 mock 请求（monkeypatch `session.request` 记录时间戳并返回 mock），断言相邻请求间隔 ≥ REQUEST_DELAY。

**提交：** `feat: BiliAPIClient线程安全化限速与请求原子化`

---

### Task 3: 异常降级统一 + -412 长冷却 + get_raw 重试

**Files:**
- Modify: `src/api_client.py`（`get()` 93-140、`get_raw` 142-144）
- Modify: `src/config.py`（新增常量，gitignore 不提交）

**Step 1:** config.py 新增：`RISK_COOLDOWN = 600  # 触发-412风控后的长冷却秒数`

**Step 2:** `get()` 改造：
- `data.get("code") == -412` 分支：短退避改为长冷却 `wait = RISK_COOLDOWN + random.uniform(0, 60)`，打印中文警告；最后一次 attempt 不再浪费冷却直接 break/return `{"code": -412, ...}`。
- `resp.json()` 的 `ValueError`（含 `requests.exceptions.JSONDecodeError`）纳入可重试异常（与 Timeout/ConnectionError 同列）。
- 所有重试耗尽路径（网络异常、HTTP 5xx、HTTP 412）统一返回 `{"code": -1, "message": "..."}`，**不再 raise**——调用方契约是检查 `code != 0`。HTTP 412 耗尽返回 `{"code": -412, "message": "风控拦截"}`。

**Step 3:** `get_raw` 加重试（MAX_RETRY 次、指数退避）与 `resp.raise_for_status()`；耗尽后 raise 最后一个异常（调用方 danmaku 有/将有 try 包裹，见 Task 6）。

**Step 4:** 验证：mock session.request 依次返回 ①非 JSON 的 200 响应 ×3 → 断言返回 `{"code": -1}` 而非抛异常；②code=-412 的 JSON → 断言走长冷却分支（mock time.sleep 记录时长 ≥ RISK_COOLDOWN）；③ConnectionError ×3 → 返回 -1 不 raise。

**Step 5:** 提交：`feat: 请求异常统一降级返回与-412长冷却机制`

---

### Task 4: 启动初始化（buvid4/bili_ticket/UA 校验）

**Files:**
- Modify: `src/api_client.py`（`_ensure_buvid3` 扩展、新增 `_ensure_bili_ticket`、`get()` 调用点）
- Modify: `src/config.py`（新增常量，gitignore 不提交）

**Step 1:** config.py 新增：`BILI_TICKET_ENABLED = True  # 申请bili_ticket降低风控概率`

**Step 2:** 检查 config.py 的 `DEFAULT_HEADERS`：UA 必须是完整浏览器串且不含 `python`/`curl` 子串；必须含 `Referer: https://www.bilibili.com/`。不满足则修正（验证步骤断言）。

**Step 3:** `_ensure_buvid3` 扩展：finger/spi 返回的 `b_4` 也写入 cookie（buvid4）。

**Step 4:** 新增 `_ensure_bili_ticket`（调研规范）：

```python
def _ensure_bili_ticket(self):
    """申请 bili_ticket（3天有效），降低风控概率；失败静默降级"""
    if not BILI_TICKET_ENABLED or getattr(self, "_bili_ticket_ok", False):
        return
    self._bili_ticket_ok = True  # 每次会话只尝试一次
    try:
        ts = int(time.time())
        hexsign = hmac.new(b"XgwSnGZ1p", f"ts{ts}".encode(), hashlib.sha256).hexdigest()
        data = self.post(
            "https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket",
            params={"key_id": "ec02", "hexsign": hexsign, "context[ts]": ts, "csrf": ""},
        )
        ticket = (data.get("data") or {}).get("ticket", "")
        if ticket:
            self.session.cookies.set("bili_ticket", ticket, domain=".bilibili.com")
    except Exception:
        pass  # 非必需，失败不影响主流程
```

依赖 Task 5 的 `post()`——**本 Task 与 Task 5 顺序对调也可，若 post() 尚未存在，先临时用 `self.get(...)` 不行（这是 POST），则先实现 Task 5 的 post() 再回本 Task**。控制器建议执行顺序：Task 1→2→3→5→4→6。

**Step 5:** `get()` 中 `_ensure_buvid3()` 后加 `_ensure_bili_ticket()`。

**Step 6:** 验证：mock post 返回 ticket → 断言 cookie 写入且第二次调用不再发请求；断言 DEFAULT_HEADERS 的 UA 不含 'python' 且含 Referer。

**Step 7:** 提交：`feat: 启动初始化buvid4与bili_ticket降低风控暴露`

---

### Task 5: BiliAPIClient.post() + auth 刷新流程走客户端

**Files:**
- Modify: `src/api_client.py`（新增 `post()`）
- Modify: `src/auth.py`（`_check_needs_refresh` 106-116、`_try_refresh_cookie` 128+、主流程 verify 失败分支）

**Step 1:** `api_client.py` 新增 `post(url, data=None, params=None, **kwargs) -> dict`：与 `get()` 同款限速（走 `_request_locked("POST", ...)`）、网络异常重试、`resp.json()` 容错、耗尽返回 `{"code": -1}`。不做 WBI 签名（刷新接口不需要）。

**Step 2:** `auth.py` 改造：
- `_check_needs_refresh`：`client.session.get(...)` → `data = client.get(url)`，从返回 dict 取 `data.get("data", {}).get("refresh", False)`。
- `_try_refresh_cookie`：Step 2 correspond 页是 HTML（需要 `.text` 正则提取 refresh_csrf）→ 用 `client.get_raw(url, cookies=...)` 拿 Response（Task 3 后 get_raw 已带重试）；Step 3/5 两个 POST → `client.post(...)`。
- 主流程（约 185-215 行区域，读代码定位）：`verify_cookie` 返回 False 但存在 `_refresh_token` 时，先无条件尝试一次 `_try_refresh_cookie`，成功则重新 verify；失败才要求重新扫码（修复"refresh_token 明明可能有效却直接要求扫码"）。

**Step 3:** 验证：`import auth` 正常；mock client.get/post/get_raw 断言 `_check_needs_refresh` 与 `_try_refresh_cookie` 不再直接访问 `client.session.get/post`（可用 `unittest.mock.patch.object` 包装 session 断言未被调用，或代码审查级确认 + 行为断言）。

**Step 4:** 提交：`refactor: cookie刷新流程改走BiliAPIClient统一限速重试`

---

### Task 6: danmaku 采集异常隔离

**Files:**
- Modify: `src/danmaku.py`（`fetch_all_danmaku` 循环，约 88-93 行区域）

**背景：** `fetch_danmaku` 经 `get_raw`（Task 3 后耗尽会 raise）+ `etree.fromstring`，单分 P 失败（网络抖动/412 HTML 错误页）会炸掉整条流水线。

**Step 1:** `fetch_all_danmaku` 的分 P 循环内对每页 try/except：失败页打印中文警告并跳过（记录失败页数，循环结束后若全部失败则返回空并醒目警告）。`etree.fromstring` 建议加 `etree.XMLParser(resolve_entities=False, no_network=True)` 防 XXE。

**Step 2:** 验证：mock client.get_raw 第 2 页 raise、其余正常 → 断言返回正常页弹幕并打印警告；mock 返回 HTML 错误页 → XMLSyntaxError 被捕获跳过。

**Step 3:** 提交：`fix: 弹幕分页采集异常隔离单页失败不中断`

---

## Self-Review 记录

- 执行顺序：**1 → 2 → 3 → 5 → 4 → 6**（Task 4 依赖 Task 5 的 post()，计划中已注明）。
- Spec 覆盖：路线图阶段 2 全部项 → Task 1-6 覆盖（WBI 规范/密钥刷新=1；线程安全=2；JSON 容错/不 raise/-412 冷却/get_raw 重试=3；buvid/bili_ticket/UA/Referer=4；post()+auth 走客户端+verify 失败先刷新=5；danmaku 隔离=6）。
- config.py 常量：RISK_COOLDOWN（Task 3）、BILI_TICKET_ENABLED（Task 4）——gitignore 不提交。
- 已读源码确认的关键行号写在背景节，implementer 无需重新探索架构。

## 全部任务完成后（控制器执行）

- [ ] `python quick_test.py --top 3` 冒烟（真实网络，重点观察 WBI 接口、bili_ticket 是否正常工作）
- [ ] 整体终审（base=main）→ 合并
