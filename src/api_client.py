"""
基础HTTP请求封装：带限速、重试、Cookie管理、WBI签名
"""
import time
import random
import hashlib
import hmac
import threading
import requests
from urllib.parse import quote
from config import (DEFAULT_HEADERS, REQUEST_DELAY, REQUEST_DELAY_LONG, MAX_RETRY,
                    RETRY_BACKOFF, RISK_COOLDOWN, NAV_URL, BILI_TICKET_ENABLED,
                    ADAPTIVE_THROTTLE_FACTOR, ADAPTIVE_THROTTLE_MAX, ADAPTIVE_THROTTLE_DECAY)


# WBI 密钥混淆数组（来自 biliscope）
WBI_OE = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45,
          35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38,
          41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60,
          51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
          20, 34, 44, 52]


class BiliAPIClient:
    """B站API客户端，统一处理请求、限速、重试、WBI签名、buvid3/buvid4/bili_ticket

    线程安全：限速与请求发出通过同一把 RLock 原子化，多线程并发调用时
    限速为全局限速（所有线程共享同一速率，整体请求频率不会超过配置上限）。
    注意：重试退避 sleep（-352/-403 等待、指数退避）在锁外执行，不阻塞其他线程；
    但 -412 风控冷却通过全局共享时间戳 _risk_cooldown_until 在锁内等待，
    所有线程一起暂停（全局冷却的预期语义，避免多线程各自命中 -412 加重风控）。
    """

    def __init__(self, session: requests.Session = None):
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self._last_request_time = 0
        # -412 风控全局冷却截止时刻（时间戳），所有线程共享；0 表示无冷却
        self._risk_cooldown_until = 0.0
        # 自适应降速倍率：触发风控时 ×ADAPTIVE_THROTTLE_FACTOR（上限 ADAPTIVE_THROTTLE_MAX），
        # 业务成功请求缓慢衰减回 1.0；进程内共享（多线程读写 float 靠 GIL 即可，启发式允许竞态）
        self._throttle = 1.0
        # RLock：_get_wbi_key/_ensure_buvid3 在持有锁的请求路径中可能嵌套发请求
        self._lock = threading.RLock()
        self._wbi_key = None
        self._wbi_key_date = None  # WBI 密钥缓存日期，img_key/sub_key 全站统一、每日更替
        self._buvid3 = None
        # spi 获取失败置 True，本进程内不再每次 get 重试（对齐 _bili_ticket_ok 每进程一次模式）
        self._buvid3_failed = False
        self._risk_apis = {
            "relation/followings", "relation/followers",
            "space/wbi/arc/search", "polymer/web-dynamic",
            "space/wbi/acc/info",
        }

    def _is_risk_api(self, url: str) -> bool:
        return any(r in url for r in self._risk_apis)

    def _is_wbi_api(self, url: str) -> bool:
        return "/wbi/" in url

    def _get_wbi_key(self) -> str:
        """获取并缓存 WBI mixin key（img_key/sub_key 全站统一、每日更替，跨日期自动刷新）"""
        today = time.strftime("%Y-%m-%d")
        if self._wbi_key and self._wbi_key_date == today:
            return self._wbi_key
        try:
            resp = self._request_locked("GET", NAV_URL, timeout=10)
            data = resp.json().get("data", {}).get("wbi_img", {})
            img = data.get("img_url", "").split("/")[-1].split(".")[0]
            sub = data.get("sub_url", "").split("/")[-1].split(".")[0]
            val = img + sub
            self._wbi_key = "".join(val[i] for i in WBI_OE)[:32]
            self._wbi_key_date = today
        except Exception:
            self._wbi_key = ""
        return self._wbi_key

    def _sign_wbi(self, params: dict) -> dict:
        """为 WBI 接口参数添加签名（规范：过滤 !'()*，urlencode 大写百分号，空格 %20）"""
        key = self._get_wbi_key()
        if not key:
            return params
        # 剔除残留的旧签名参数，避免旧 w_rid/wts 混入签名串导致重签无效（-352/-403 恢复路径会传入旧值）
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

    def _ensure_buvid3(self):
        """获取并缓存 buvid3/buvid4 设备指纹，减少风控；失败置标志，本进程不再重试"""
        if self._buvid3 or self._buvid3_failed:
            return self._buvid3 or ""
        try:
            resp = self._request_locked(
                "GET",
                "https://api.bilibili.com/x/frontend/finger/spi",
                timeout=10,
            )
            data = resp.json().get("data", {})
            self._buvid3 = data.get("b_3", "")
            if self._buvid3:
                self.session.cookies.set("buvid3", self._buvid3, domain=".bilibili.com")
            # spi 同时返回 b_4（buvid4），一并写入 cookie
            buvid4 = data.get("b_4", "")
            if buvid4:
                self.session.cookies.set("buvid4", buvid4, domain=".bilibili.com")
        except Exception:
            pass
        if not self._buvid3:
            # 异常或响应缺 b_3 均视为失败：本进程内后续 get 不再重复打 spi
            self._buvid3_failed = True
        return self._buvid3 or ""

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

    def _penalize_throttle(self, what: str):
        """触发风控：上调请求间隔倍率（撞一次墙就老实一点）"""
        old = self._throttle
        self._throttle = min(self._throttle * ADAPTIVE_THROTTLE_FACTOR, ADAPTIVE_THROTTLE_MAX)
        if self._throttle > old:
            print(f"[API] {what}，自适应降速: 请求间隔倍率 {old:.2f} → {self._throttle:.2f}")

    def _reward_throttle(self):
        """业务成功请求：倍率缓慢衰减回 1.0（约40次成功回落一半）"""
        self._throttle = max(1.0, self._throttle * ADAPTIVE_THROTTLE_DECAY)

    def _sleep_if_needed(self, url: str):
        # -412 全局冷却：本方法在 _request_locked 的锁内执行，
        # 冷却等待会阻塞其他线程拿锁，即所有请求一起暂停（全局冷却的预期语义）
        remaining = self._risk_cooldown_until - time.time()
        if remaining > 0:
            if remaining > 1:
                print(f"[API] 风控冷却中，等待 {remaining:.0f} 秒...")
            time.sleep(remaining)
        # 区间内随机取间隔（消除固定节奏特征），再乘自适应倍率
        lo, hi = REQUEST_DELAY_LONG if self._is_risk_api(url) else REQUEST_DELAY
        delay = random.uniform(lo, hi) * self._throttle
        elapsed = time.time() - self._last_request_time
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request_time = time.time()

    def _request_locked(self, method: str, url: str, **kwargs) -> requests.Response:
        """限速与请求发出原子化（线程安全）；全局冷却等待在锁内的 _sleep_if_needed 中执行"""
        with self._lock:
            self._sleep_if_needed(url)
            # setdefault：调用方显式传 timeout（如 _get_wbi_key 的 timeout=10）时保留，避免关键字冲突
            kwargs.setdefault("timeout", 15)
            return self.session.request(method, url, **kwargs)

    def get(self, url: str, params: dict = None, headers: dict = None, **kwargs) -> dict:
        merged_headers = {**(headers or {})}
        params = dict(params or {})

        # buvid3/bili_ticket 反爬
        self._ensure_buvid3()
        self._ensure_bili_ticket()

        # WBI 签名
        if self._is_wbi_api(url):
            params = self._sign_wbi(params)

        for attempt in range(MAX_RETRY):
            try:
                resp = self._request_locked("GET", url, params=params, headers=merged_headers, **kwargs)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") == -412:
                    # 风控拦截：短退避无意义，改长冷却；最后一次不再浪费冷却，直接降级返回
                    if attempt < MAX_RETRY - 1:
                        wait = RISK_COOLDOWN + random.uniform(0, 60)
                        # 记录全局冷却截止时刻，其他线程在 _sleep_if_needed 中一起等待
                        self._risk_cooldown_until = time.time() + wait
                        self._penalize_throttle("触发风控-412")
                        print(f"[API] 触发风控-412，冷却 {wait:.0f} 秒后重试...")
                        time.sleep(wait)
                        continue
                    return {"code": -412, "message": "风控拦截"}
                # 签名失效（一般接口 -352、评论 wbi 接口 -403，均伴随 v_voucher）：清缓存强制刷新密钥并重签
                if data.get("code") in (-352, -403):
                    # 末次 attempt 不再清缓存+退避+重签（重签也是白做），
                    # 直接返回降级 dict 且保留 -352/-403 语义供调用方判断签名失效
                    if attempt < MAX_RETRY - 1:
                        self._wbi_key = None
                        self._wbi_key_date = None
                        if attempt == 0:
                            # 首次：按真签名失效处理，短退避后重签重发
                            time.sleep(RETRY_BACKOFF)
                        else:
                            # 重签后仍 -352/-403：实为风控拦截（与签名无关，实测重签无效），
                            # 与 -412 同等处理：全局冷却后再做最后一次尝试
                            wait = RISK_COOLDOWN + random.uniform(0, 60)
                            # 记录全局冷却截止时刻，其他线程在 _sleep_if_needed 中一起等待
                            self._risk_cooldown_until = time.time() + wait
                            self._penalize_throttle(f"重签后仍 {data.get('code')}")
                            print(f"[API] 重签后仍 {data.get('code')}，判定为风控，冷却 {wait:.0f} 秒后重试...")
                            time.sleep(wait)
                        params = self._sign_wbi(dict(params))
                        continue
                    return {"code": data["code"], "message": "WBI签名失效/风控拦截，重试已耗尽"}
                # 业务成功（非风控码）：自适应倍率缓慢回落
                self._reward_throttle()
                return data
            except (requests.Timeout, requests.ConnectionError, ValueError) as e:
                # ValueError 含 resp.json() 解析失败（风控 HTML 错误页等非 JSON 响应）
                if attempt < MAX_RETRY - 1:
                    wait = RETRY_BACKOFF * (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(wait)
                else:
                    return {"code": -1, "message": f"请求异常: {e}"}
            except requests.HTTPError as e:
                status = e.response.status_code if e.response else 0
                if status == 412:
                    # HTTP 412 与业务码 -412 同等处理：长冷却重试，耗尽后降级返回
                    if attempt < MAX_RETRY - 1:
                        wait = RISK_COOLDOWN + random.uniform(0, 60)
                        # 记录全局冷却截止时刻，其他线程在 _sleep_if_needed 中一起等待
                        self._risk_cooldown_until = time.time() + wait
                        self._penalize_throttle("触发风控HTTP412")
                        print(f"[API] 触发风控HTTP412，冷却 {wait:.0f} 秒后重试...")
                        time.sleep(wait)
                        continue
                    return {"code": -412, "message": "风控拦截"}
                if attempt < MAX_RETRY - 1 and status >= 500:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    time.sleep(wait)
                else:
                    return {"code": -1, "message": f"HTTP错误 {status}"}
        return {"code": -1, "message": "重试次数耗尽"}

    def post(self, url: str, data: dict = None, params: dict = None, **kwargs) -> dict:
        """POST 请求（限速+重试），返回解析后的 JSON dict

        与 get() 同款限速/指数退避重试，耗尽降级返回 {"code": -1, ...} 不 raise。
        不走 WBI 签名，也不调用 _ensure_buvid3（cookie 刷新等接口不需要，
        且避免在 buvid3 获取路径中嵌套 POST 造成递归）。
        """
        for attempt in range(MAX_RETRY):
            try:
                resp = self._request_locked("POST", url, data=data, params=params, **kwargs)
                resp.raise_for_status()
                data = resp.json()
                # POST 成功：自适应倍率缓慢回落（post 不做风控码处理，沿用原降级语义）
                self._reward_throttle()
                return data
            except (requests.Timeout, requests.ConnectionError, ValueError) as e:
                # ValueError 含 resp.json() 解析失败（非 JSON 响应）
                if attempt < MAX_RETRY - 1:
                    wait = RETRY_BACKOFF * (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(wait)
                else:
                    return {"code": -1, "message": f"请求异常: {e}"}
            except requests.HTTPError as e:
                status = e.response.status_code if e.response else 0
                if attempt < MAX_RETRY - 1 and status >= 500:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    time.sleep(wait)
                else:
                    return {"code": -1, "message": f"HTTP错误 {status}"}
        return {"code": -1, "message": "重试次数耗尽"}

    def get_raw(self, url: str, params: dict = None, **kwargs) -> requests.Response:
        """带重试的原始响应请求（弹幕 XML 等非 JSON 接口）

        重试 MAX_RETRY 次（指数退避+抖动），耗尽后 raise 最后一个异常，由调用方兜底。
        """
        last_exc = None
        for attempt in range(MAX_RETRY):
            try:
                resp = self._request_locked("GET", url, params=params, **kwargs)
                resp.raise_for_status()
                # 原始响应成功：自适应倍率缓慢回落
                self._reward_throttle()
                return resp
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
                last_exc = e
                if attempt < MAX_RETRY - 1:
                    wait = RETRY_BACKOFF * (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(wait)
        raise last_exc

    def update_cookies(self, cookies: dict):
        self.session.cookies.update(cookies)

    def get_cookies_dict(self) -> dict:
        return dict(self.session.cookies)
