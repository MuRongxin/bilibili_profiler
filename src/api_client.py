"""
基础HTTP请求封装：带限速、重试、Cookie管理、WBI签名
"""
import time
import random
import hashlib
import requests
from config import DEFAULT_HEADERS, REQUEST_DELAY, REQUEST_DELAY_LONG, MAX_RETRY, RETRY_BACKOFF, NAV_URL


# WBI 密钥混淆数组（来自 biliscope）
WBI_OE = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45,
          35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38,
          41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60,
          51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
          20, 34, 44, 52]


class BiliAPIClient:
    """B站API客户端，统一处理请求、限速、重试、WBI签名、buvid3"""

    def __init__(self, session: requests.Session = None):
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self._last_request_time = 0
        self._wbi_key = None
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
        """获取并缓存 WBI mixin key"""
        if self._wbi_key:
            return self._wbi_key
        try:
            resp = self.session.get(NAV_URL, timeout=10)
            data = resp.json().get("data", {}).get("wbi_img", {})
            img = data.get("img_url", "").split("/")[-1].split(".")[0]
            sub = data.get("sub_url", "").split("/")[-1].split(".")[0]
            val = img + sub
            self._wbi_key = "".join(val[i] for i in WBI_OE)[:32]
        except Exception:
            self._wbi_key = ""
        return self._wbi_key

    def _sign_wbi(self, params: dict) -> dict:
        """为 WBI 接口参数添加签名"""
        key = self._get_wbi_key()
        if not key:
            return params
        # 剔除残留的旧签名参数，避免旧 w_rid/wts 混入签名串导致重签无效（-403 恢复路径会传入旧值）
        params = {k: v for k, v in params.items() if k not in ("w_rid", "wts")}
        params["wts"] = int(time.time())
        keys = sorted(params.keys())
        param_str = "&".join(f"{k}={params[k]}" for k in keys)
        sign = hashlib.md5((param_str + key).encode()).hexdigest()
        params["w_rid"] = sign
        return params

    def _ensure_buvid3(self):
        """获取并缓存 buvid3 设备指纹，减少风控"""
        if self._buvid3:
            return self._buvid3
        try:
            resp = self.session.get(
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

    def get(self, url: str, params: dict = None, headers: dict = None, **kwargs) -> dict:
        merged_headers = {**(headers or {})}
        params = dict(params or {})

        # buvid3 反爬
        self._ensure_buvid3()

        # WBI 签名
        if self._is_wbi_api(url):
            params = self._sign_wbi(params)

        for attempt in range(MAX_RETRY):
            self._sleep_if_needed(url)
            try:
                resp = self.session.get(url, params=params, headers=merged_headers, timeout=15, **kwargs)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") == -412:
                    wait = RETRY_BACKOFF * (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(wait)
                    continue
                # WBI key 过期时重新获取
                if data.get("code") == -403:
                    self._wbi_key = None
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
        self._sleep_if_needed(url)
        return self.session.get(url, params=params, timeout=15, **kwargs)

    def update_cookies(self, cookies: dict):
        self.session.cookies.update(cookies)

    def get_cookies_dict(self) -> dict:
        return dict(self.session.cookies)
