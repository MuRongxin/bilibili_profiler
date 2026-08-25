# 风控轮换重构（账号×IP 组合池）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 风控时不再直接 600s 长冷却，改为编排层换"新号+新IP"重试，长冷却降级为最后手段；IP 池支持外部 Clash 自动探测与内置 mihomo 核心（多订阅），任何一层故障都自动降级，保底单账号+单IP+长冷却也能跑完。

**Architecture:** `BiliAPIClient` 增加风控信号上报（`RiskControlError`/`ProxyConnError`，`raise_on_risk` 开关，默认 False 保持旧行为）；新增 `ComboPool`（鸭子类型模拟 client，透明包裹每个请求做组合轮换）+ `ClashCtl`（控制器封装）+ `ProxyCore`（内置 mihomo 生命周期）。采集函数零签名改动。

**Tech Stack:** 纯 Python3 stdlib + requests；无 pytest——验证用内联 heredoc 自测脚本 + `py_compile` + `quick_test.py` 冒烟（遵循 AGENTS.md：项目无单元测试框架）。

**关键设计取舍（相对 spec §4 的修正）：** ComboPool 鸭子类型模拟 `BiliAPIClient`（实现 `get/post/get_raw/update_cookies`），各采集函数签名不变、直接把原来的 `client` 实参换成 pool 即可。轮换粒度从"任务单元"细化为"单请求"，翻页游标天然保留在采集函数内，断点不丢。spec §4 在 Task 8 同步修正。

---

### Task 1: config.py 新增 IP 池配置常量

**Files:**
- Modify: `src/config.py`（追加到 `ADAPTIVE_THROTTLE_*` 常量块之后）

- [ ] **Step 1: 追加配置常量**

`src/config.py` 已有 `import os`（第4行）与 `RISK_COOLDOWN = 600`。在 `ADAPTIVE_THROTTLE_DECAY` 行之后追加：

```python
# ========== IP 池（外部 Clash / 内置 mihomo 核心） ==========
# 机场订阅链接列表（内置 mihomo 核心的节点来源；凭证，勿提交、勿打印）
SUB_URLS = [u.strip() for u in os.environ.get("SUB_URLS", "").split(",") if u.strip()]
# 覆盖内置 mihomo 二进制定位（默认依次找 vendor/mihomo、data/mihomo，都没有则自动下载）
MIHOMO_PATH = os.environ.get("MIHOMO_PATH", "")
# 外部 Clash/ShellCrash 控制器（CLASH_ENABLED=1 显式启用；未启用也会自动探测本机常见端口）
CLASH_ENABLED = os.environ.get("CLASH_ENABLED", "") == "1"
CLASH_API_URL = os.environ.get("CLASH_API_URL", "http://127.0.0.1:9090")
CLASH_SECRET = os.environ.get("CLASH_SECRET", "")
CLASH_GROUP = os.environ.get("CLASH_GROUP", "")            # 轮换节点组名（内置核心默认 profiler）
CLASH_PROXY_URL = os.environ.get("CLASH_PROXY_URL", "http://127.0.0.1:7890")
# 单任务单元允许的兜底冷却圈数（整圈账号全风控 → 长冷却为最后手段）
MAX_RISK_ROUNDS = 2
```

- [ ] **Step 2: 编译验证**

Run: `.venv/bin/python -m py_compile src/config.py && .venv/bin/python -c "import sys; sys.path.insert(0,'src'); import config; print(config.SUB_URLS, config.MAX_RISK_ROUNDS)"`
Expected: 输出 `[] 2`

- [ ] **Step 3: Commit**

```bash
git add src/config.py
git commit -m "feat: IP池配置常量（SUB_URLS多订阅/外部Clash/兜底圈数）"
```

---

### Task 2: api_client.py 风控信号化

**Files:**
- Modify: `src/api_client.py`（新增异常类、`raise_on_risk` 开关、`set_proxy`；改造 `get()`/`post()`/`get_raw()` 风控路径）

核心语义：`raise_on_risk=False`（默认）保持现状旧行为（600s 冷却重试）——登录等独立 client 与未迁移路径不受影响，每个中间 commit 都可用；`True`（ComboPool 成员）时风控短退避一次后抛 `RiskControlError` 上报编排层。`ProxyConnError`（代理连接失败，区别于目标站风控）任何模式都立即抛出。

- [ ] **Step 1: 写失败自测**

Run:

```bash
.venv/bin/python - <<'EOF'
import sys; sys.path.insert(0, "src")
from api_client import RiskControlError   # 尚不存在，应 ImportError
EOF
```

Expected: `ImportError: cannot import name 'RiskControlError'`

- [ ] **Step 2: 新增异常类**

`src/api_client.py` 在 `WBI_OE` 数组之后、`class BiliAPIClient` 之前插入：

```python
class RiskControlError(Exception):
    """风控拦截信号（-412 / HTTP412 / 重签后仍 -352/-403）：由编排层（ComboPool）接管轮换"""


class ProxyConnError(Exception):
    """代理连接失败（IP 池故障，区别于目标站风控）：ComboPool 据此切节点/摘代理转直连"""
```

- [ ] **Step 3: `__init__` 增加开关 + 新增 `set_proxy`**

`__init__` 末尾（`self._risk_apis = {...}` 之后）追加：

```python
        # 风控处理模式：False=旧行为（600s 长冷却后重试，登录等独立 client 路径）；
        # True=抛 RiskControlError 上报编排层（ComboPool 成员由池统一设置）
        self.raise_on_risk = False
```

类中新增方法（放在 `update_cookies` 前）：

```python
    def set_proxy(self, url: str | None):
        """设置/清除代理（None 或空串 = 直连）；带认证的代理地址直接内嵌 user:pass@host:port"""
        self.session.proxies = {"http": url, "https": url} if url else {}
```

- [ ] **Step 4: 改造 `get()` 的 -412 分支**

把 -412 分支（原 191-201 行）替换为：

```python
                if data.get("code") == -412:
                    self._penalize_throttle("触发风控-412")
                    if self.raise_on_risk:
                        # 编排层模式：一次短退避原地重试（防瞬时抖动），仍失败抛信号
                        if attempt == 0:
                            print("[API] 触发风控-412，短退避后原地重试一次...")
                            time.sleep(RETRY_BACKOFF)
                            continue
                        raise RiskControlError("-412 风控拦截")
                    # 旧行为：短退避无意义，改长冷却；最后一次不再浪费冷却，直接降级返回
                    if attempt < MAX_RETRY - 1:
                        wait = RISK_COOLDOWN + random.uniform(0, 60)
                        # 记录全局冷却截止时刻，其他线程在 _sleep_if_needed 中一起等待
                        self._risk_cooldown_until = time.time() + wait
                        print(f"[API] 触发风控-412，冷却 {wait:.0f} 秒后重试...")
                        time.sleep(wait)
                        continue
                    return {"code": -412, "message": "风控拦截"}
```

- [ ] **Step 5: 改造 `get()` 的 -352/-403 分支**

把该分支（原 203-223 行）替换为：

```python
                # 签名失效（一般接口 -352、评论 wbi 接口 -403，均伴随 v_voucher）：清缓存强制刷新密钥并重签
                if data.get("code") in (-352, -403):
                    # 末次 attempt 不再清缓存+退避+重签（重签也是白做）：
                    # 编排层模式抛信号；旧模式返回降级 dict 保留 -352/-403 语义
                    if attempt < MAX_RETRY - 1:
                        self._wbi_key = None
                        self._wbi_key_date = None
                        if attempt == 0:
                            # 首次：按真签名失效处理，短退避后重签重发
                            time.sleep(RETRY_BACKOFF)
                        else:
                            # 重签后仍 -352/-403：实为风控拦截（与签名无关，实测重签无效）
                            self._penalize_throttle(f"重签后仍 {data.get('code')}")
                            if self.raise_on_risk:
                                raise RiskControlError(f"重签后仍 {data['code']}，判定为风控")
                            # 旧行为：与 -412 同等处理，全局冷却后再做最后一次尝试
                            wait = RISK_COOLDOWN + random.uniform(0, 60)
                            self._risk_cooldown_until = time.time() + wait
                            print(f"[API] 重签后仍 {data.get('code')}，判定为风控，冷却 {wait:.0f} 秒后重试...")
                            time.sleep(wait)
                        params = self._sign_wbi(dict(params))
                        continue
                    if self.raise_on_risk:
                        raise RiskControlError(f"{data['code']} 重签重试耗尽")
                    return {"code": data["code"], "message": "WBI签名失效/风控拦截，重试已耗尽"}
```

- [ ] **Step 6: `get()` 增加 ProxyError 直通 + 改造 HTTP 412 分支**

在 `except (requests.Timeout, requests.ConnectionError, ValueError)` 之前插入（ProxyError 是 ConnectionError 子类，必须先捕获）：

```python
            except requests.exceptions.ProxyError as e:
                # 代理连接失败是 IP 池故障而非目标站风控：立即上报，不消耗重试
                raise ProxyConnError(str(e)) from e
```

HTTP 412 分支（原 236-246 行）替换为：

```python
                if status == 412:
                    self._penalize_throttle("触发风控HTTP412")
                    if self.raise_on_risk:
                        if attempt == 0:
                            print("[API] 触发风控HTTP412，短退避后原地重试一次...")
                            time.sleep(RETRY_BACKOFF)
                            continue
                        raise RiskControlError("HTTP 412 风控拦截")
                    # HTTP 412 与业务码 -412 同等处理：长冷却重试，耗尽后降级返回
                    if attempt < MAX_RETRY - 1:
                        wait = RISK_COOLDOWN + random.uniform(0, 60)
                        self._risk_cooldown_until = time.time() + wait
                        print(f"[API] 触发风控HTTP412，冷却 {wait:.0f} 秒后重试...")
                        time.sleep(wait)
                        continue
                    return {"code": -412, "message": "风控拦截"}
```

- [ ] **Step 7: `post()` 增加 ProxyError 直通与 412 信号**

`post()` 的 `except (requests.Timeout, requests.ConnectionError, ValueError)` 之前插入同 Step 6 的 ProxyError 块。`except requests.HTTPError` 内 `if attempt < MAX_RETRY - 1 and status >= 500:` 之前插入：

```python
                if status == 412 and self.raise_on_risk:
                    self._penalize_throttle("触发风控HTTP412(post)")
                    raise RiskControlError("HTTP 412 风控拦截(post)")
```

- [ ] **Step 8: `get_raw()` 增加 ProxyError 直通与 412 信号**

`get_raw()` 的 except 块整体替换为：

```python
            except requests.exceptions.ProxyError as e:
                # 代理连接失败：立即上报，不消耗重试
                raise ProxyConnError(str(e)) from e
            except requests.HTTPError as e:
                if (self.raise_on_risk and e.response is not None
                        and e.response.status_code == 412):
                    self._penalize_throttle("触发风控HTTP412(raw)")
                    raise RiskControlError("HTTP 412 风控拦截(raw)") from e
                last_exc = e
                if attempt < MAX_RETRY - 1:
                    wait = RETRY_BACKOFF * (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(wait)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                if attempt < MAX_RETRY - 1:
                    wait = RETRY_BACKOFF * (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(wait)
```

- [ ] **Step 9: 自测通过**

Run（耗时约 10s：限速 0.8-1.6s + 短退避 2s，无 600s 冷却）:

```bash
.venv/bin/python - <<'EOF'
import sys, time; sys.path.insert(0, "src")
import requests
from api_client import BiliAPIClient, RiskControlError, ProxyConnError

class FakeResp:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p

class FakeSession:
    def __init__(self, exc=None):
        self.headers = {}; self.cookies = requests.cookies.RequestsCookieJar()
        self.proxies = {}; self._exc = exc
    def request(self, *a, **k):
        if self._exc: raise self._exc
        return FakeResp({"code": -412, "message": "risk"})

# 1) raise_on_risk=True：-412 短退避一次后抛 RiskControlError，且无 600s 冷却
c = BiliAPIClient(session=FakeSession()); c.raise_on_risk = True
t0 = time.time()
try:
    c.get("https://api.bilibili.com/x/relation/followings")
    raise SystemExit("应当抛 RiskControlError")
except RiskControlError:
    pass
assert time.time() - t0 < 30, "不应进入长冷却"

# 2) 默认模式保持旧行为开关
assert BiliAPIClient(session=FakeSession()).raise_on_risk is False

# 3) ProxyError 立即抛 ProxyConnError（不重试）
c2 = BiliAPIClient(session=FakeSession(exc=requests.exceptions.ProxyError("dead proxy")))
t0 = time.time()
try:
    c2.get("https://api.bilibili.com/x/relation/followings")
    raise SystemExit("应当抛 ProxyConnError")
except ProxyConnError:
    pass
assert time.time() - t0 < 15, "代理故障不应退避重试"

# 4) set_proxy 设置/清除
c3 = BiliAPIClient(session=FakeSession())
c3.set_proxy("http://127.0.0.1:7890")
assert c3.session.proxies["https"] == "http://127.0.0.1:7890"
c3.set_proxy(None)
assert c3.session.proxies == {}
print("PASS")
EOF
```

Expected: `PASS`

- [ ] **Step 10: 编译 + Commit**

```bash
.venv/bin/python -m py_compile src/api_client.py
git add src/api_client.py
git commit -m "feat: 风控信号化（RiskControlError/ProxyConnError/raise_on_risk），编排层模式不再直接长冷却"
```

---

### Task 3: 新增 src/clash_ctl.py（Clash 控制器封装）

**Files:**
- Create: `src/clash_ctl.py`

- [ ] **Step 1: 写失败自测**

Run: `.venv/bin/python -c "import sys; sys.path.insert(0,'src'); import clash_ctl"`
Expected: `ModuleNotFoundError`

- [ ] **Step 2: 实现 clash_ctl.py**

```python
"""Clash/mihomo 外部控制器封装：列节点/切节点（换出口 IP）

所有失败静默降级（返回 False/[]/None），不抛给上层——IP 池不可用时由 ComboPool 走降级链。
注意：直连控制器（requests 裸调用），不走 BiliAPIClient（无 B 站限速语义）。
"""
import requests

# 组内伪节点/内置策略名，不进入轮换
_PSEUDO = {"DIRECT", "REJECT", "PASS", "GLOBAL", "COMPATIBLE"}


class ClashCtl:
    def __init__(self, api_url: str, secret: str = "", group: str = ""):
        self.api_url = api_url.rstrip("/")
        self.secret = secret
        self.group = group          # 为空时首次读组自动挑选第一个非 GLOBAL 的 Selector 组
        self._nodes: list[str] = []
        self._cursor = 0

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.secret}"} if self.secret else {}

    def available(self) -> bool:
        """控制器连通性自检"""
        try:
            r = requests.get(f"{self.api_url}/proxies", headers=self._headers(), timeout=3)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def _fetch_group(self) -> dict:
        """读节点组信息；group 为空时自动挑选第一个非 GLOBAL、含节点的 Selector 组。
        返回 {"name", "all", "now"}；请求异常向上抛（由调用方静默降级）。"""
        r = requests.get(f"{self.api_url}/proxies", headers=self._headers(), timeout=5)
        r.raise_for_status()
        proxies = (r.json() or {}).get("proxies") or {}
        if self.group:
            g = proxies.get(self.group) or {}
            return {"name": self.group, "all": g.get("all") or [], "now": g.get("now", "")}
        for name, g in proxies.items():
            if name == "GLOBAL":
                continue
            if (g or {}).get("type") == "Selector" and (g.get("all") or []):
                self.group = name
                return {"name": name, "all": g["all"], "now": g.get("now", "")}
        return {"name": "", "all": [], "now": ""}

    def list_nodes(self) -> list[str]:
        """组内可轮换节点（过滤伪节点）；失败返回 []"""
        try:
            g = self._fetch_group()
        except requests.RequestException:
            return []
        return [n for n in g["all"] if n and n.upper() not in _PSEUDO]

    def refresh_nodes(self) -> list[str]:
        """刷新并缓存节点列表"""
        self._nodes = self.list_nodes()
        return self._nodes

    def current_node(self) -> str | None:
        try:
            return self._fetch_group()["now"] or None
        except requests.RequestException:
            return None

    def pick_next_node(self) -> str | None:
        """组内轮询推进，跳过当前节点；节点为空返回 None"""
        if not self._nodes:
            self.refresh_nodes()
        if not self._nodes:
            return None
        cur = self.current_node()
        self._cursor = (self._cursor + 1) % len(self._nodes)
        if cur and len(self._nodes) > 1:
            for _ in range(len(self._nodes)):
                if self._nodes[self._cursor] != cur:
                    break
                self._cursor = (self._cursor + 1) % len(self._nodes)
        return self._nodes[self._cursor]

    def switch_node(self, name: str) -> bool:
        """切换节点组选择（换出口 IP）；失败返回 False"""
        if not name:
            return False
        try:
            g = self._fetch_group()
            if not g["name"]:
                return False
            r = requests.put(
                f"{self.api_url}/proxies/{requests.utils.quote(g['name'], safe='')}",
                headers=self._headers(), json={"name": name}, timeout=5)
            return r.status_code in (200, 204)
        except requests.RequestException:
            return False
```

- [ ] **Step 3: 自测通过（mock 控制器）**

Run:

```bash
.venv/bin/python - <<'EOF'
import sys; sys.path.insert(0, "src")
from unittest import mock
import clash_ctl
from clash_ctl import ClashCtl

FAKE = {"proxies": {
    "GLOBAL": {"type": "Selector", "all": ["node1", "node2"], "now": "node1"},
    "profiler": {"type": "Selector", "all": ["DIRECT", "node1", "node2", "node3"], "now": "node1"},
    "node1": {"type": "Shadowsocks"},
}}

class R:
    status_code = 200
    def json(self): return FAKE
    def raise_for_status(self): pass

with mock.patch.object(clash_ctl.requests, "get", return_value=R()), \
     mock.patch.object(clash_ctl.requests, "put", return_value=R()) as put:
    ctl = ClashCtl("http://127.0.0.1:9090", group="profiler")
    assert ctl.available() is True
    assert ctl.list_nodes() == ["node1", "node2", "node3"]   # DIRECT 被过滤
    assert ctl.current_node() == "node1"
    assert ctl.pick_next_node() == "node2"                   # 轮询推进且跳过当前
    assert ctl.switch_node("node2") is True
    assert "profiler" in put.call_args.args[0]

    # group 为空自动选组（跳过 GLOBAL）
    ctl2 = ClashCtl("http://127.0.0.1:9090")
    assert ctl2.list_nodes() == ["node1", "node2", "node3"] and ctl2.group == "profiler"

# 控制器不可达 → 静默降级
dead = ClashCtl("http://127.0.0.1:1")
assert dead.available() is False and dead.list_nodes() == [] and dead.pick_next_node() is None
assert dead.switch_node("x") is False
print("PASS")
EOF
```

Expected: `PASS`

- [ ] **Step 4: 编译 + Commit**

```bash
.venv/bin/python -m py_compile src/clash_ctl.py
git add src/clash_ctl.py
git commit -m "feat: Clash控制器封装（列节点/轮询/切节点，失败静默降级）"
```

---

### Task 4: 新增 src/proxy_core.py（内置 mihomo 核心）

**Files:**
- Create: `src/proxy_core.py`

职责：机场订阅 → 本地 127.0.0.1 随机端口混合代理 + 控制器。不解析订阅（`proxy-providers` 由核心自己拉取/解析）。任何一步失败静默降级返回 None。

- [ ] **Step 1: 写失败自测**

Run: `.venv/bin/python -c "import sys; sys.path.insert(0,'src'); import proxy_core"`
Expected: `ModuleNotFoundError`

- [ ] **Step 2: 实现 proxy_core.py**

```python
"""内置 mihomo 核心生命周期：机场订阅 → 本地代理端口 + 控制器

把 ShellCrash 的核心能力集成进程序：使用者无需安装任何梯子工具，
给 SUB_URLS 一个或多个机场订阅链接即可获得 IP 池。核心只监听 127.0.0.1
随机端口，系统与其它应用流量不经过它。订阅链接是凭证：不落日志、不进仓库。
"""
import atexit
import gzip
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import time

import requests

from clash_ctl import ClashCtl
from config import DATA_DIR, MIHOMO_PATH

_RUNTIME_DIR = os.path.join(DATA_DIR, "mihomo_runtime")
_RELEASES = "https://github.com/MetaCubeX/mihomo/releases/latest/download"


def _free_port() -> int:
    """取一个 127.0.0.1 空闲端口（避免与已有 Clash 实例冲突）"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _go_platform() -> tuple[str, str]:
    goos = {"linux": "linux", "darwin": "darwin"}.get(sys.platform, "")
    if sys.platform.startswith("win"):
        goos = "windows"
    goarch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64",
              "arm64": "arm64"}.get(platform.machine().lower(), "")
    return goos, goarch


def _download_binary(dest: str) -> bool:
    """从 GitHub Releases 下载 mihomo 二进制（.gz 解压）；失败返回 False"""
    goos, goarch = _go_platform()
    if not goos or not goarch:
        print(f"[ProxyCore] 不支持的平台 {sys.platform}/{platform.machine()}，请手动放置 mihomo 到 {dest}")
        return False
    base = f"mihomo-{goos}-{goarch}"
    suffix = ".exe" if goos == "windows" else ""
    # linux/amd64 优先 compatible 变体（老 CPU 无 v3 指令集也能跑）
    names = ([f"{base}-compatible{suffix}.gz", f"{base}{suffix}.gz"]
             if (goos, goarch) == ("linux", "amd64") else [f"{base}{suffix}.gz"])
    for name in names:
        url = f"{_RELEASES}/{name}"
        try:
            print(f"[ProxyCore] 下载 mihomo 核心: {url}")
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(gzip.decompress(r.content))
            os.chmod(dest, 0o755)
            return True
        except Exception as e:
            print(f"[ProxyCore] 下载失败（{name}）: {e}")
    return False


def ensure_binary(allow_download: bool = True) -> str | None:
    """定位 mihomo 二进制：MIHOMO_PATH → vendor/mihomo → data/mihomo → 自动下载"""
    candidates = [MIHOMO_PATH] if MIHOMO_PATH else []
    exe = "mihomo.exe" if sys.platform.startswith("win") else "mihomo"
    candidates += [os.path.join("vendor", exe), os.path.join(DATA_DIR, exe)]
    for p in candidates:
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    if not allow_download:
        return None
    dest = os.path.join(DATA_DIR, exe)
    return os.path.abspath(dest) if _download_binary(dest) else None


class ProxyCore:
    """内置 mihomo 核心：start() 成功返回就绪的 ClashCtl，失败返回 None（静默降级）"""

    def __init__(self, sub_urls: list[str], group: str = "profiler"):
        self.sub_urls = list(sub_urls)
        self.group = group
        self.mix_port = _free_port()
        self.api_port = _free_port()
        self.secret = secrets.token_hex(8)
        self._proc: subprocess.Popen | None = None

    def _gen_config(self) -> str:
        """生成最小 yaml（字符串模板，项目无 pyyaml 依赖）；每订阅一个 provider，
        select 组 use 引用全部 provider（多订阅节点汇入同一轮换组）"""
        providers = "\n".join(
            f'  sub{i}:\n'
            f'    type: http\n'
            f'    url: "{u}"\n'
            f'    interval: 86400\n'
            f'    health-check:\n'
            f'      enable: true\n'
            f'      url: "https://www.gstatic.com/generate_204"\n'
            f'      interval: 600'
            for i, u in enumerate(self.sub_urls))
        uses = ", ".join(f"sub{i}" for i in range(len(self.sub_urls)))
        return (
            f"mixed-port: {self.mix_port}\n"
            f"external-controller: 127.0.0.1:{self.api_port}\n"
            f'secret: "{self.secret}"\n'
            f"log-level: warning\n"
            f"proxy-providers:\n{providers}\n"
            f"proxy-groups:\n"
            f"  - name: {self.group}\n"
            f"    type: select\n"
            f"    use: [{uses}]\n"
            f"rules:\n"
            f"  - MATCH,{self.group}\n"
        )

    def start(self, allow_download: bool = True) -> ClashCtl | None:
        """拉起核心子进程并轮询就绪（核心需先拉订阅，最长等 60s）；失败返回 None"""
        if self._proc is not None:
            return None
        bin_path = ensure_binary(allow_download=allow_download)
        if not bin_path:
            print("[ProxyCore] 未找到 mihomo 二进制（可设 MIHOMO_PATH 或放置到 vendor/），禁用内置核心")
            return None
        os.makedirs(_RUNTIME_DIR, exist_ok=True)
        cfg_path = os.path.join(_RUNTIME_DIR, "config.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(self._gen_config())
        os.chmod(cfg_path, 0o600)   # 含订阅链接，收紧权限
        try:
            self._proc = subprocess.Popen(
                [bin_path, "-d", _RUNTIME_DIR, "-f", cfg_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            print(f"[ProxyCore] 核心启动失败: {e}")
            return None
        atexit.register(self.stop)
        ctl = ClashCtl(f"http://127.0.0.1:{self.api_port}", self.secret, self.group)
        for _ in range(60):
            if self._proc.poll() is not None:
                print(f"[ProxyCore] 核心进程退出（code={self._proc.returncode}），禁用内置核心")
                self._proc = None
                return None
            nodes = ctl.refresh_nodes() if ctl.available() else []
            if nodes:
                print(f"[ProxyCore] 内置核心就绪: {len(nodes)} 个节点（混合端口 127.0.0.1:{self.mix_port}）")
                return ctl
            time.sleep(1)
        print("[ProxyCore] 核心就绪超时（订阅拉取失败？），禁用内置核心")
        self.stop()
        return None

    def stop(self):
        """终止核心子进程（atexit 注册；幂等）"""
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
```

- [ ] **Step 3: 自测通过（纯本地，不下载、不拉核心）**

Run:

```bash
.venv/bin/python - <<'EOF'
import sys; sys.path.insert(0, "src")
from proxy_core import ProxyCore, _free_port, ensure_binary

core = ProxyCore(["https://example.com/sub1", "https://example.com/sub2"])
cfg = core._gen_config()
assert "proxy-providers:" in cfg and cfg.count("type: http") == 2
assert "name: profiler" in cfg and "use: [sub0, sub1]" in cfg
assert f"mixed-port: {core.mix_port}" in cfg and f"external-controller: 127.0.0.1:{core.api_port}" in cfg

p = _free_port()
assert 1024 < p < 65536

# 无二进制且不下载 → 静默降级 None（本机 vendor/data 均无 mihomo）
assert ensure_binary(allow_download=False) is None
assert core.start(allow_download=False) is None
core.stop()   # 幂等不抛
print("PASS")
EOF
```

Expected: `PASS`

- [ ] **Step 4: 编译 + Commit**

```bash
.venv/bin/python -m py_compile src/proxy_core.py
git add src/proxy_core.py
git commit -m "feat: 内置mihomo核心生命周期（多订阅→本地随机端口代理+控制器，零安装）"
```

---

### Task 5: 新增 src/combo_pool.py（账号×IP 组合池）

**Files:**
- Create: `src/combo_pool.py`

- [ ] **Step 1: 写失败自测**

Run: `.venv/bin/python -c "import sys; sys.path.insert(0,'src'); import combo_pool"`
Expected: `ModuleNotFoundError`

- [ ] **Step 2: 实现 combo_pool.py**

```python
"""账号×IP 组合池：风控时换"新号+新IP"重试，长冷却为最后手段

ComboPool 鸭子类型模拟 BiliAPIClient（get/post/get_raw/update_cookies），
各采集阶段/函数零签名改动：原来的 client 实参直接换成 pool 即可。
每个请求经 run() 包装——风控时 rotate()（下一账号+切节点）后原请求重试，
翻页游标保留在采集函数内部，断点天然不丢。

降级链（任何一层坏了退到下一层，流水线不中断）：
  账号×IP 组合轮换 → IP 池故障摘代理 → 账号轮转+直连 → 整圈风控 600s 长冷却
  → 连续 MAX_RISK_ROUNDS 圈仍失败抛 RiskControlError（阶段层按失败降级跳过本单元）
"""
import random
import time

from config import (CLASH_API_URL, CLASH_ENABLED, CLASH_GROUP, CLASH_PROXY_URL,
                    CLASH_SECRET, MAX_RISK_ROUNDS, NAV_URL, RISK_COOLDOWN, SUB_URLS)
from api_client import ProxyConnError, RiskControlError

_PROXY_FAIL_STRIP_THRESHOLD = 3   # 代理连续连接失败多少次后判定 IP 池不可用、摘代理转直连


class ComboPool:
    def __init__(self, accounts: list, clash=None, proxy_url: str | None = None):
        self._accounts = list(accounts)     # [(名字, BiliAPIClient)]，[0] 恒为主号
        self._clash = clash                 # ClashCtl 或 None（无 IP 池）
        self._proxy_url = proxy_url
        self._idx = 0
        self._risk_marks = [False] * len(self._accounts)
        self._rounds = 0
        self._proxy_fail_streak = 0
        for _, c in self._accounts:
            c.raise_on_risk = True          # 池成员风控改抛信号，由本池接管
            if proxy_url:
                c.set_proxy(proxy_url)

    # ---- 鸭子类型：模拟 BiliAPIClient ----
    def get(self, url, params=None, headers=None, **kw):
        return self.run(lambda c: c.get(url, params=params, headers=headers, **kw), desc=url[-40:])

    def post(self, url, data=None, params=None, **kw):
        return self.run(lambda c: c.post(url, data=data, params=params, **kw), desc=url[-40:])

    def get_raw(self, url, params=None, **kw):
        return self.run(lambda c: c.get_raw(url, params=params, **kw), desc=url[-40:])

    def update_cookies(self, cookies):
        # 登录路径不进池；仅为鸭子类型完整性防御
        self._accounts[self._idx][1].update_cookies(cookies)

    @property
    def current(self):
        return self._accounts[self._idx]

    def rotate(self, reason: str = ""):
        """换下一账号 + 切下一节点（切节点失败仅换号并记日志）"""
        self._idx = (self._idx + 1) % len(self._accounts)
        name = self._accounts[self._idx][0]
        if self._clash:
            nxt = self._clash.pick_next_node()
            if nxt and self._clash.switch_node(nxt):
                print(f"[Pool] {reason} → 换号[{name}] + 切节点[{nxt}]")
                return
            print(f"[Pool] {reason} → 换号[{name}]（切节点失败，保持当前 IP）")
        else:
            print(f"[Pool] {reason} → 换号[{name}]")

    def run(self, fn, desc: str = ""):
        """执行 fn(client)；风控→rotate 重试；整圈风控→长冷却；冷却 MAX_RISK_ROUNDS 圈仍败→抛"""
        while True:
            name, client = self._accounts[self._idx]
            try:
                result = fn(client)
                # 业务成功：清零风控圈与代理故障计数（一次霉运不污染后续单元）
                self._rounds = 0
                self._risk_marks = [False] * len(self._accounts)
                self._proxy_fail_streak = 0
                return result
            except ProxyConnError as e:
                if self._proxy_url is None:
                    raise   # 已转直连仍报代理错误属异常，直接上抛
                self._on_proxy_fault(e)
                # 代理故障不消耗风控圈：原地重试（已切节点或已转直连）
            except RiskControlError as e:
                self._risk_marks[self._idx] = True
                if all(self._risk_marks):
                    self._rounds += 1
                    if self._rounds >= MAX_RISK_ROUNDS:
                        raise RiskControlError(
                            f"{desc} 连续 {self._rounds} 圈全账号风控，放弃本单元") from e
                    wait = RISK_COOLDOWN + random.uniform(0, 60)
                    print(f"[Pool] 全部账号均触发风控，长冷却 {wait:.0f} 秒"
                          f"（最后手段，第 {self._rounds}/{MAX_RISK_ROUNDS} 圈）...")
                    time.sleep(wait)
                    self._risk_marks = [False] * len(self._accounts)
                self.rotate(f"风控({desc})")

    def _on_proxy_fault(self, e: Exception):
        """代理连接失败：先切节点重试；连续失败达阈值判定 IP 池不可用，摘代理转直连"""
        self._proxy_fail_streak += 1
        if self._proxy_fail_streak >= _PROXY_FAIL_STRIP_THRESHOLD:
            print(f"[Pool] 代理连续 {self._proxy_fail_streak} 次连接失败，判定 IP 池不可用，摘代理转直连: {e}")
            self._strip_proxy()
            return
        if self._clash:
            nxt = self._clash.pick_next_node()
            if nxt and self._clash.switch_node(nxt):
                print(f"[Pool] 代理连接失败，已切换节点 → {nxt}（第 {self._proxy_fail_streak} 次）")
                return
        print(f"[Pool] 代理连接失败（第 {self._proxy_fail_streak} 次，无法切节点）: {e}")

    def _strip_proxy(self):
        """摘掉池内全部账号的代理，转直连（IP 池故障降级）"""
        self._proxy_url = None
        self._clash = None
        for _, c in self._accounts:
            c.set_proxy(None)


def _discover_ip_pool():
    """IP 池来源自动发现：外部控制器（显式配置优先，再探测本机常见端口）→ SUB_URLS 内置核心 → 无"""
    from clash_ctl import ClashCtl
    candidates = [CLASH_API_URL] if CLASH_ENABLED else []
    candidates += ["http://127.0.0.1:9090", "http://127.0.0.1:9999", "http://127.0.0.1:9097"]
    seen = set()
    for api in candidates:
        if api in seen:
            continue
        seen.add(api)
        ctl = ClashCtl(api, CLASH_SECRET if api == CLASH_API_URL else "", CLASH_GROUP)
        if ctl.available() and ctl.refresh_nodes():
            print(f"[Pool] 发现外部 Clash 控制器 {api}（组 {ctl.group}，{len(ctl._nodes)} 节点）")
            return ctl, CLASH_PROXY_URL
    if SUB_URLS:
        from proxy_core import ProxyCore
        core = ProxyCore(SUB_URLS, group=CLASH_GROUP or "profiler")
        ctl = core.start()
        if ctl:
            return ctl, f"http://127.0.0.1:{core.mix_port}"
        print("[Pool] 内置核心启动失败，禁用 IP 池")
    return None, None


def _proxy_selfcheck(pool: ComboPool) -> bool:
    """启动自检：经代理发一次轻量请求（NAV）；失败由调用方摘代理转直连"""
    _, client = pool.current
    try:
        data = client.get(NAV_URL, timeout=8)
        return isinstance(data, dict) and "code" in data
    except Exception:
        return False


def build_pool(main_client) -> ComboPool:
    """登录完成后构建组合池：主号+小号池 + IP 池自动发现；代理自检失败自动摘代理降级"""
    from auth import load_extra_clients   # 延迟导入避免循环依赖
    accounts = [("主号", main_client)] + load_extra_clients()
    if len(accounts) > 1:
        names = "、".join(n for n, _ in accounts[1:])
        print(f"[Pool] 小号池: {len(accounts) - 1} 个可用（{names}），采集将按组合轮转分摊")

    clash, proxy_url = _discover_ip_pool()
    pool = ComboPool(accounts, clash=clash, proxy_url=proxy_url)
    if proxy_url and not _proxy_selfcheck(pool):
        print("[Pool] 代理自检失败，摘代理转直连（仅账号轮转）")
        pool._strip_proxy()
    return pool
```

注意：`NAV_URL` 在 `config.py` 已存在（api_client.py 有 import 先例），无需新增。

- [ ] **Step 3: 自测通过（fake 客户端 + fake 控制器）**

Run:

```bash
.venv/bin/python - <<'EOF'
import sys; sys.path.insert(0, "src")
from unittest import mock
import combo_pool
from combo_pool import ComboPool
from api_client import RiskControlError, ProxyConnError

class FakeClient:
    def __init__(self, risks=0):
        self.risks = risks; self.raise_on_risk = False; self.proxy = None
    def set_proxy(self, url): self.proxy = url
    def get(self, url, **kw):
        if self.risks > 0:
            self.risks -= 1
            raise RiskControlError("-412")
        return {"code": 0}

class AlwaysRisk(FakeClient):
    def get(self, url, **k): raise RiskControlError("-412")

class AlwaysProxyDead(FakeClient):
    def get(self, url, **k): raise ProxyConnError("boom")

class FakeClash:
    def __init__(self): self.switched = []
    def pick_next_node(self): return "nodeX"
    def switch_node(self, n): self.switched.append(n); return True

# 1) a 风控一次 → rotate 换号+切节点 → b 成功
a, b = FakeClient(risks=1), FakeClient()
clash = FakeClash()
pool = ComboPool([("主号", a), ("alt1", b)], clash=clash, proxy_url="http://127.0.0.1:7890")
assert a.raise_on_risk and b.raise_on_risk
assert a.proxy == "http://127.0.0.1:7890"
calls = []
r = pool.run(lambda c: (calls.append(c), c.get("x"))[1])
assert r == {"code": 0} and calls == [a, b] and clash.switched == ["nodeX"]

# 2) 全账号风控 → 长冷却（mock sleep）→ MAX_RISK_ROUNDS=1 时直接抛
p2 = ComboPool([("a", AlwaysRisk()), ("b", AlwaysRisk())])
with mock.patch.object(combo_pool.time, "sleep"), \
     mock.patch.object(combo_pool, "MAX_RISK_ROUNDS", 1):
    try:
        p2.run(lambda c: c.get("x"), desc="t")
        raise SystemExit("应当抛 RiskControlError")
    except RiskControlError:
        pass

# 3) 代理连续故障 → 摘代理转直连；已直连仍 ProxyConnError 则上抛不死循环
p3 = ComboPool([("a", AlwaysProxyDead())], proxy_url="http://127.0.0.1:7890")
with mock.patch.object(combo_pool.time, "sleep"):
    try:
        p3.run(lambda c: c.get("x"))
        raise SystemExit("应当抛 ProxyConnError")
    except ProxyConnError:
        pass
assert p3._proxy_url is None and p3._accounts[0][1].proxy is None

# 4) 无 IP 池（clash=None）：风控仅换号
c1, c2 = FakeClient(risks=1), FakeClient()
p4 = ComboPool([("a", c1), ("b", c2)])
assert p4.run(lambda c: c.get("x")) == {"code": 0}
print("PASS")
EOF
```

Expected: `PASS`

- [ ] **Step 4: 编译 + Commit**

```bash
.venv/bin/python -m py_compile src/combo_pool.py
git add src/combo_pool.py
git commit -m "feat: 账号×IP组合池（鸭子类型透明接管请求，风控换号+切节点，长冷却兜底，IP池故障降级直连）"
```

---

### Task 6: main.py / quick_test.py 接线

**Files:**
- Modify: `src/main.py`（imports、`run_analysis` 建池、`phase_collect_users` 简化、`main()` 风控兜底）
- Modify: `quick_test.py`（建池并全程用 pool）

鸭子类型收益：`phase_danmaku`/`phase_comment`/`phase_resolve` 及 `danmaku*.py`/`comment.py`/`uid_resolver.py`/`user_collector.py` **零改动**——调用点把 `client` 实参换成 `pool` 即可。

- [ ] **Step 1: main.py imports**

把 `from auth import get_auth_client, load_extra_clients` 改为：

```python
from auth import get_auth_client
from combo_pool import build_pool
```

- [ ] **Step 2: run_analysis 建池，替换小号池块**

把原块（小号池注释 + `extra_clients = load_extra_clients()` + if 打印，约 585-590 行）整体替换为：

```python
    # 账号×IP 组合池：主号+小号轮转，风控换"新号+新IP"重试，长冷却兜底；
    # IP 池自动发现（外部控制器探测 → SUB_URLS 内置核心），故障自动降级直连
    pool = build_pool(client)
```

随后各阶段调用点把 `client` 换成 `pool`：

- `phase_danmaku(bvid, client)` → `phase_danmaku(bvid, pool)`
- `phase_comment(video_info, client)` → `phase_comment(video_info, pool)`
- `phase_resolve(bvid, sender_groups, comment_uid_map, client, ...)` → 第三个位置实参换 `pool`
- `phase_collect_users(resolved, client, max_users=max_users, force=force, extra_clients=extra_clients)` → `phase_collect_users(resolved, pool, max_users=max_users, force=force)`

- [ ] **Step 3: phase_collect_users 简化（删除手工轮换）**

函数签名改为：

```python
def phase_collect_users(resolved: dict, pool, max_users: int | None = None, force: bool = False):
```

docstring 中账号池段落替换为：

```python
    组合池（ComboPool）：每个请求由池透明接管——风控自动换"新号+新IP"重试，
    长冷却为池内兜底；兜底耗尽抛 RiskControlError，本 uid 按失败跳过（流水线不中断）。
```

删除"账号池轮转状态"注释起的 `pool = [...]` / `rr = 0` 两行，以及 per-uid 循环里的整个 `tried`/while 选号块（含 `[账号:...]` 打印与风控换号分支），替换为：

```python
        # 组合池接管风控轮换与兜底冷却；兜底耗尽抛 RiskControlError，按失败跳过本 uid
        try:
            data = collect_user_data(uid, pool)
        except Exception as e:
            data = {"error": str(e)}
```

（其后的 `"error" not in data` 落库/失败分支保持不变。）

- [ ] **Step 4: main() 单跑风控兜底**

`main()` 末尾调用 `run_analysis(...)` 处包一层（先 Read `src/main.py:773-811` 确认现有调用形态，保持其余参数不变）：

```python
    try:
        run_analysis(args.bvid, force=args.force, max_users=args.max_users)
    except RiskControlError as e:
        print(f"[Main] 风控兜底耗尽（{e}），本视频分析终止")
        raise SystemExit(1)
```

同时在文件头部 import 区加 `from api_client import RiskControlError`（若无）。批量模式 `run_batch` 已有 `except Exception` 逐视频兜底，RiskControlError 天然被接住继续下一视频，无需改动。

- [ ] **Step 5: quick_test.py 建池**

import 区加：

```python
from combo_pool import build_pool
```

`client = get_auth_client()` 之后插入：

```python
    # 账号×IP 组合池（同主流程：风控换号+切节点，故障自动降级）
    pool = build_pool(client)
```

后续所有 `client` 实参替换为 `pool`：`collect_danmaku_data(bvid, pool)`、`_merge_history_danmaku(video_info, danmaku_list, pool)`、`collect_comment_data(..., pool)`、`resolve_sender(..., pool)`、`collect_user_data(uid, pool)`。其中 `collect_user_data(uid, pool)` 外包一层（兜底耗尽跳过该用户，对齐"失败降级不中断"）：

```python
        try:
            user_data = collect_user_data(uid, pool)
        except Exception as e:
            print(f"  ❌ 用户数据采集失败: {e}")
            continue
        if "error" in user_data:
            ...
```

- [ ] **Step 6: 编译 + 残留检查**

Run:

```bash
.venv/bin/python -m py_compile src/main.py quick_test.py
grep -n "extra_clients\|load_extra_clients" src/main.py quick_test.py || echo "残留检查 OK"
```

Expected: 编译无输出；`残留检查 OK`

- [ ] **Step 7: Commit**

```bash
git add src/main.py quick_test.py
git commit -m "feat: 主流程/冒烟接入组合池（风控轮换透明接管，phase_collect_users 删除手工选号）"
```

---

### Task 7: web.py 手动分析 job 接线

**Files:**
- Modify: `web.py`（import、`_run_analysis_job` 建池与两处调用）

- [ ] **Step 1: web.py 改动**

import 区（约 48-49 行 `from uid_resolver import ...` 附近）加：

```python
from combo_pool import build_pool
```

`_run_analysis_job` 中 `client = _get_client()` 成功之后（约 188 行 `return` 分支之后）插入：

```python
    # 账号×IP 组合池（同主流程：风控换号+切节点，故障自动降级）
    pool = build_pool(client)
```

两处调用把 `client` 换成 `pool`：

- `resolve_sender(mid_hash, stats["contents"], plain_uid_map, client, method_map=method_map)` → 第四实参换 `pool`
- `user_data = collect_user_data(uid, client)` → `collect_user_data(uid, pool)`

说明：per-mid_hash 的 `try/except Exception` 已存在（约 206 行起），池兜底耗尽的 `RiskControlError` 会被记为 errors 并继续下一个 mid_hash，无需额外处理。

- [ ] **Step 2: 编译 + Commit**

```bash
.venv/bin/python -m py_compile web.py
git add web.py
git commit -m "feat: web手动分析job接入组合池"
```

---

### Task 8: 文档同步与收尾验证

**Files:**
- Modify: `docs/superpowers/specs/2026-08-25-risk-rotation-ip-pool-design.md`（§4 修正为鸭子类型手法）
- Modify: `AGENTS.md`、`README.md`、`.gitignore`

- [ ] **Step 1: spec §4 修正**

把 spec 中"### 4. 各采集阶段接入（任务单元粒度，断点不丢）"整节替换为：

```markdown
### 4. 各采集阶段接入（鸭子类型透明接管，零签名改动）

实现手法（相对初稿的修正）：`ComboPool` 鸭子类型模拟 `BiliAPIClient`
（`get/post/get_raw/update_cookies`），各采集函数**签名不变**，调用点把
`client` 实参换成 `pool` 即可。每个请求经 `pool.run()` 包装，风控时换
"新号+新IP"后**原请求**重试——轮换粒度为单请求，比"任务单元"更细，
翻页游标/日期循环天然保留在采集函数内，断点不丢、已采部分不重跑。

- 阶段1 弹幕 / 历史弹幕 / 互动弹幕：调用点换 pool，无函数改动；
- 阶段3 评论 / 充电名单：同上；
- 阶段4 UID 解析（resolve_all_senders / web 端 resolve_sender）：同上；
- 阶段5 用户采集 `phase_collect_users`：删除手工选号分支，`collect_user_data(uid, pool)`，
  池兜底耗尽抛 `RiskControlError` → 该 uid 按失败跳过；
- `web.py` 手动分析 job：建池后同样换 pool。
```

- [ ] **Step 2: AGENTS.md 更新**

代码结构块 `├── api_client.py ...` 行之后插入三行：

```
├── clash_ctl.py         # Clash/mihomo 控制器封装（列节点/切节点换出口 IP，失败静默降级）
├── proxy_core.py        # 内置 mihomo 核心生命周期（SUB_URLS 多订阅→127.0.0.1 随机端口代理+控制器，零安装）
├── combo_pool.py        # 账号×IP 组合池（鸭子类型模拟 BiliAPIClient；风控换"新号+新IP"重试，长冷却兜底，IP 池故障摘代理降级直连）
```

两处描述同步：

- 概述中"阶段5支持主号+小号池按 uid 轮转分摊，风控号不剔除、仅换号重试"（user_collector.py 行）改为"采集全程走账号×IP 组合池（combo_pool，鸭子类型透明接管：风控换号+切节点重试，长冷却兜底，IP 池故障自动降级直连）"；
- 安全注意事项追加一条：`- 机场订阅链接（config.py SUB_URLS / 环境变量）与 data/mihomo_runtime/config.yaml 含凭证，均不入库、不打印；内置核心二进制在 data/ 或 vendor/（gitignore）。`

- [ ] **Step 3: README.md 更新**

在小号池段落之后追加：

```markdown
可选 IP 池（风控时换"新号+新IP"继续采集，长冷却仅作最后手段；不配也能正常运行）：
- 已有运行中的 Clash/ShellCrash：程序自动探测本机控制器（9090/9999/9097），零配置直接用；ShellCrash 建议「模式设置 → 流量劫持范围 → 4 纯净模式」，其它应用流量不受影响。
- 没有梯子工具：在 config.py 或环境变量填 `SUB_URLS`（支持多个机场订阅，逗号分隔），程序自动下载/拉起内置 mihomo 核心（只监听 127.0.0.1 随机端口，不影响其它应用）。
```

- [ ] **Step 4: .gitignore 追加**

先 Read `.gitignore`，然后追加：

```
# 内置 mihomo 核心（二进制 + 运行时配置含订阅凭证）
vendor/mihomo*
data/mihomo*
```

- [ ] **Step 5: 全量编译 + 冒烟**

Run:

```bash
.venv/bin/python -m py_compile src/*.py web.py run.py quick_test.py login.py login_bg.py && echo "编译 OK"
.venv/bin/python quick_test.py --top 1
```

Expected: 编译 OK；quick_test 端到端跑通（需有效 Cookie 与真实网络；无代理环境验证降级路径——日志应出现组合池构建信息，且无代理时自动直连）。

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/specs/2026-08-25-risk-rotation-ip-pool-design.md AGENTS.md README.md .gitignore
git commit -m "docs: 组合池/IP池文档同步（AGENTS/README/spec §4 鸭子类型修正）+ gitignore 内置核心"
```

---

## Self-Review 记录

- **Spec 覆盖**：风控信号化(T2)、ClashCtl(T3)、ProxyCore(T4)、ComboPool+降级链+兜底(T5)、各阶段接入(T6/T7)、配置(T1)、文档(T8)——spec 各节均有对应任务；spec §4 手法修正已在 T8 同步。
- **Placeholder 扫描**：无 TBD/TODO；所有代码步骤含完整代码。
- **类型一致性**：`RiskControlError`/`ProxyConnError`（T2 定义，T5/T6/T7 使用）；`ClashCtl(api_url, secret, group)`（T3 定义，T4/T5 使用）；`ProxyCore(sub_urls, group).start() -> ClashCtl|None`、`core.mix_port`（T4 定义，T5 使用）；`ComboPool(accounts, clash, proxy_url)`/`build_pool(main_client)`（T5 定义，T6/T7 使用）；`MAX_RISK_ROUNDS`/`RISK_COOLDOWN`/`NAV_URL`/`SUB_URLS`/`CLASH_*`（T1/config 已有）。
- **已知取舍**：`raise_on_risk=False` 旧冷却路径保留给登录等独立 client（spec 的"删除冷却循环"落地为"编排层接管"，旧路径作为非池 client 的保底，进程内行为可回归）。
