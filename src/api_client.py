"""
基础HTTP请求封装：带限速、重试、Cookie管理、WBI签名
"""
import time
import random
import hashlib
import threading
import requests
from urllib.parse import quote
from config import DEFAULT_HEADERS, REQUEST_DELAY, REQUEST_DELAY_LONG, MAX_RETRY, RETRY_BACKOFF, NAV_URL


# WBI 密钥混淆数组（来自 biliscope）
WBI_OE = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45,
          35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38,
          41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60,
          51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
          20, 34, 44, 52]


class BiliAPIClient:
    """B站API客户端，统一处理请求、限速、重试、WBI签名、buvid3

    线程安全：限速与请求发出通过同一把 RLock 原子化，多线程并发调用时
    限速为全局限速（所有线程共享同一速率，整体请求频率不会超过配置上限）。
    注意：冷却/退避 sleep（重试退避、-412/-352 等待）在锁外执行，不阻塞其他线程。
    """

    def __init__(self, session: requests.Session = None):
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self._last_request_time = 0
        # RLock：_get_wbi_key/_ensure_buvid3 在持有锁的请求路径中可能嵌套发请求
        self._lock = threading.RLock()
        self._wbi_key = None
        self._wbi_key_date = None  # WBI 密钥缓存日期，img_key/sub_key 全站统一、每日更替
        self._buvid3 = None
        self._risk_apis = {
            "relation/followings", "relation/followers",
            "space/wbi/arc/search", "polymer/web-dynamic",
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
        """获取并缓存 buvid3 设备指纹，减少风控"""
        if self._buvid3:
            return self._buvid3
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
        except Exception:
            pass
        return self._buvid3 or ""

    def _sleep_if_needed(self, url: str):
        delay = REQUEST_DELAY_LONG if self._is_risk_api(url) else REQUEST_DELAY
        delay += random.uniform(0.1, 0.4)
        elapsed = time.time() - self._last_request_time
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request_time = time.time()

    def _request_locked(self, method: str, url: str, **kwargs) -> requests.Response:
        """限速与请求发出原子化（线程安全）；冷却 sleep 在锁外，不阻塞其他线程"""
        with self._lock:
            self._sleep_if_needed(url)
            # setdefault：调用方显式传 timeout（如 _get_wbi_key 的 timeout=10）时保留，避免关键字冲突
            kwargs.setdefault("timeout", 15)
            return self.session.request(method, url, **kwargs)

    def get(self, url: str, params: dict = None, headers: dict = None, **kwargs) -> dict:
        merged_headers = {**(headers or {})}
        params = dict(params or {})

        # buvid3 反爬
        self._ensure_buvid3()

        # WBI 签名
        if self._is_wbi_api(url):
            params = self._sign_wbi(params)

        for attempt in range(MAX_RETRY):
            try:
                resp = self._request_locked("GET", url, params=params, headers=merged_headers, **kwargs)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") == -412:
                    wait = RETRY_BACKOFF * (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(wait)
                    continue
                # 签名失效（一般接口 -352、评论 wbi 接口 -403，均伴随 v_voucher）：清缓存强制刷新密钥并重签
                if data.get("code") in (-352, -403):
                    self._wbi_key = None
                    self._wbi_key_date = None
                    # 退避后再重发，避免连续重发同一无效请求加重风控
                    time.sleep(RETRY_BACKOFF)
                    params = self._sign_wbi(dict(params))
                    continue
                return data
            except (requests.Timeout, requests.ConnectionError):
                if attempt < MAX_RETRY - 1:
                    wait = RETRY_BACKOFF * (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(wait)
                else:
                    raise
            except requests.HTTPError as e:
                status = e.response.status_code if e.response else 0
                if status == 412:
                    if attempt < MAX_RETRY - 1:
                        wait = RETRY_BACKOFF * (2 ** attempt) + random.uniform(0, 2)
                        time.sleep(wait)
                        continue
                    else:
                        raise
                if attempt < MAX_RETRY - 1 and status >= 500:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    time.sleep(wait)
                else:
                    raise
        return {"code": -1, "message": "Max retry exceeded"}

    def get_raw(self, url: str, params: dict = None, **kwargs) -> requests.Response:
        return self._request_locked("GET", url, params=params, **kwargs)

    def update_cookies(self, cookies: dict):
        self.session.cookies.update(cookies)

    def get_cookies_dict(self) -> dict:
        return dict(self.session.cookies)
