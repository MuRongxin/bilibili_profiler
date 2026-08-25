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
