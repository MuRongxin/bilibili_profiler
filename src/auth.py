"""
B站扫码登录 + Cookie自动刷新

核心改进（参考 bilibili-api）：
- 登录时保存 refresh_token，用于后续刷新
- cookie 过期时自动 RSA 刷新，无需重新扫码
"""
import json
import time
import os
import re
import tempfile
import uuid
import qrcode
import requests
from io import BytesIO
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256

from api_client import BiliAPIClient
from config import COOKIE_PATH, QRCODE_GEN_URL, QRCODE_POLL_URL, NAV_URL

# B站 cookie 刷新用的 RSA 公钥
REFRESH_PUBKEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----"""


def generate_qrcode_image(url: str, filepath: str = "qrcode.png") -> str:
    qr_ascii = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=2)
    qr_ascii.add_data(url)
    qr_ascii.make(fit=True)
    qr_ascii.print_ascii(invert=True, tty=False)

    qr_img = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
    qr_img.add_data(url)
    qr_img.make(fit=True)
    img = qr_img.make_image(fill_color="black", back_color="white")
    img.save(filepath)
    return filepath


def get_qrcode() -> tuple[str, str]:
    client = BiliAPIClient()
    data = client.get(QRCODE_GEN_URL)
    if data.get("code") != 0:
        raise Exception(f"获取二维码失败: {data}")
    return data["data"]["url"], data["data"]["qrcode_key"]


def poll_qrcode(qrcode_key: str, client: BiliAPIClient) -> dict:
    return client.get(QRCODE_POLL_URL, params={"qrcode_key": qrcode_key})


def save_cookie(client: BiliAPIClient) -> None:
    cookie_dict = client.get_cookies_dict()
    # 同时保存 refresh_token
    if hasattr(client, "_refresh_token"):
        cookie_dict["_refresh_token"] = client._refresh_token
    # 原子写入：先写临时文件再 os.replace，避免中断留下截断的 JSON
    dir_ = os.path.dirname(COOKIE_PATH) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cookie_dict, f, ensure_ascii=False, indent=2)
        # cookie 即账号凭证，收紧权限为仅当前用户可读写
        os.chmod(tmp, 0o600)
        os.replace(tmp, COOKIE_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print(f"[Auth] Cookie已保存到 {COOKIE_PATH}")


def load_cookie() -> dict | None:
    if not os.path.exists(COOKIE_PATH):
        return None
    try:
        with open(COOKIE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # cookie 文件损坏（如上次写入被中断或编码异常），降级为重新登录而非崩溃
        print(f"[Auth] 警告: Cookie文件损坏（{COOKIE_PATH}），请重新登录")
        return None
    if not isinstance(data, dict):
        # 合法 JSON 但不是 cookie dict（如被误写成数组/字符串），同样按损坏处理
        print(f"[Auth] 警告: Cookie文件格式异常（{COOKIE_PATH}），请重新登录")
        return None
    return data


def verify_cookie(client: BiliAPIClient) -> bool:
    try:
        data = client.get(NAV_URL)
        if data.get("code") == 0 and (data.get("data") or {}).get("isLogin"):
            uname = data["data"].get("uname", "未知")
            print(f"[Auth] Cookie有效，当前用户: {uname}")
            return True
    except Exception as e:
        print(f"[Auth] Cookie验证异常: {e}")
    return False


def _check_needs_refresh(client: BiliAPIClient) -> bool:
    """检查 cookie 是否需要刷新（还没完全过期但快过期）"""
    try:
        data = client.get(
            "https://passport.bilibili.com/x/passport-login/web/cookie/info",
        )
        # get() 降级返回 {"code": -1} 时 data 字段为 None，用 or {} 防御
        return (data.get("data") or {}).get("refresh", False)
    except Exception:
        return False


def _get_correspond_path() -> str:
    """RSA-OAEP 加密生成 correspondPath"""
    key = RSA.importKey(REFRESH_PUBKEY)
    cipher = PKCS1_OAEP.new(key, SHA256)
    ts = round(time.time() * 1000)
    encrypted = cipher.encrypt(f"refresh_{ts}".encode())
    return encrypted.hex()


def _try_refresh_cookie(client: BiliAPIClient) -> bool:
    """尝试刷新 cookie，返回是否成功"""
    refresh_token = getattr(client, "_refresh_token", None)
    if not refresh_token:
        return False

    try:
        print("[Auth] 尝试刷新Cookie...")

        # Step 1: 获取 correspondPath
        correspond_path = _get_correspond_path()

        # Step 2: 获取 refresh_csrf（该页面是 HTML，需要原始响应用正则提取；
        # get_raw 重试耗尽会 raise，这里接住降级为刷新失败）
        buvid = str(uuid.uuid1())
        try:
            resp = client.get_raw(
                f"https://www.bilibili.com/correspond/1/{correspond_path}",
                cookies={"buvid3": buvid},
            )
        except Exception as e:
            print(f"[Auth] 获取 refresh_csrf 请求失败: {e}")
            return False
        match = re.search(r'<div id="1-name">(.+?)</div>', resp.text)
        if not match:
            print("[Auth] refresh_csrf 提取失败")
            return False
        refresh_csrf = match.group(1)

        # Step 3: 执行刷新（requests.Session 会自动把响应 Set-Cookie 写入 cookie jar）
        data = client.post(
            "https://passport.bilibili.com/x/passport-login/web/cookie/refresh",
            data={
                "csrf": client.get_cookies_dict().get("bili_jct", ""),
                "refresh_csrf": refresh_csrf,
                "refresh_token": refresh_token,
                "source": "main_web",
            },
            cookies={"buvid3": str(uuid.uuid1())},
        )
        if data.get("code") != 0:
            print(f"[Auth] 刷新失败: {data.get('message', '')}")
            return False

        # Step 4: 更新 cookies（Step 3 的新 cookie 已自动写入 session，直接读取）
        new_cookies = client.get_cookies_dict()
        new_refresh_token = (data.get("data") or {}).get("refresh_token", "")

        # Step 5: 确认刷新
        client.post(
            "https://passport.bilibili.com/x/passport-login/web/confirm/refresh",
            data={
                "csrf": new_cookies.get("bili_jct", ""),
                "refresh_token": refresh_token,
            },
        )

        client._refresh_token = new_refresh_token or refresh_token
        save_cookie(client)
        print("[Auth] Cookie刷新成功!")
        return True

    except Exception as e:
        print(f"[Auth] Cookie刷新异常: {e}")
        return False


def login_by_qrcode() -> BiliAPIClient:
    """扫码登录，返回带登录态的客户端（支持自动刷新）"""
    client = BiliAPIClient()

    # 尝试加载已有cookie
    cookie_dict = load_cookie()
    if cookie_dict:
        refresh_token = cookie_dict.pop("_refresh_token", None)
        client.update_cookies(cookie_dict)
        if refresh_token:
            client._refresh_token = refresh_token

        if verify_cookie(client):
            return client

        # Cookie 验证失败但 refresh_token 可能仍有效：无条件先尝试一次刷新，
        # 成功后再重新验证；仍失败才走重新扫码
        if refresh_token and _try_refresh_cookie(client):
            if verify_cookie(client):
                return client

        print("[Auth] Cookie已过期，需要重新扫码登录...")

    # 扫码登录
    url, qrcode_key = get_qrcode()
    qr_path = os.path.join(os.path.dirname(COOKIE_PATH), "qrcode.png")
    generate_qrcode_image(url, qr_path)

    print("\n" + "=" * 50)
    print("请使用B站APP扫描二维码登录")
    print("=" * 50)

    max_wait = 180
    start_time = time.time()
    last_status = None

    while time.time() - start_time < max_wait:
        result = poll_qrcode(qrcode_key, client)
        # get() 降级返回 {"code": -1} 时 data 字段缺失或为 None，用 or {} 防御
        code = (result.get("data") or {}).get("code", -1)

        if code == 0:
            # 从扫码响应中提取 refresh_token
            refresh_token = (result.get("data") or {}).get("refresh_token", "")
            if refresh_token:
                client._refresh_token = refresh_token
            print("\n[Auth] 登录成功!")
            save_cookie(client)
            return client
        elif code == 86101:
            pass
        elif code == 86090:
            if last_status != 86090:
                print("\n[Auth] 已扫码，等待APP确认...")
                last_status = 86090
        elif code == 86038:
            print("\n[Auth] 二维码已过期，请重新运行程序")
            raise Exception("二维码过期")
        else:
            msg = (result.get("data") or {}).get("message", "未知状态")
            if last_status != code:
                print(f"\n[Auth] 登录状态: {msg} (code={code})")
                last_status = code

        time.sleep(2)

    raise Exception("登录超时（3分钟）")


def get_auth_client() -> BiliAPIClient:
    return login_by_qrcode()
