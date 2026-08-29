#!/usr/bin/env python3
"""
B站扫码登录后台轮询（非交互式）
持续轮询二维码状态，直到登录成功或超时。

用法:
    python login_bg.py          # 主号（data/cookie.json）
    python login_bg.py alt1     # 小号 alt1（data/cookies/alt1.json）
"""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

# 强制行缓冲：输出被重定向/管道时也能实时看到进度（默认块缓冲会长时间无输出）
sys.stdout.reconfigure(line_buffering=True)

from auth import (get_qrcode, poll_qrcode, save_cookie, verify_cookie, load_cookie,
                  account_cookie_path, COOKIE_PATH)
from api_client import BiliAPIClient


def main():
    # 位置参数即账号名：python login_bg.py alt1 → data/cookies/alt1.json
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        cookie_path = account_cookie_path(name) if name else COOKIE_PATH
    except ValueError as e:
        print(f"错误: {e}")
        sys.exit(1)

    client = BiliAPIClient()

    # 检查已有cookie
    cookie_dict = load_cookie(cookie_path)
    if cookie_dict:
        # _refresh_token 是本地保存的伪 cookie，需先弹出，避免注入 session 发给B站
        refresh_token = cookie_dict.pop("_refresh_token", None)
        client.update_cookies(cookie_dict)
        if refresh_token:
            client._refresh_token = refresh_token
        if verify_cookie(client):
            print("[Login] 已有有效Cookie")
            return

    # 生成二维码（路径按账号名区分，主号保持 data/qrcode.png，小号 qrcode_{name}.png）
    print("[Login] 获取二维码...")
    url, qrcode_key = get_qrcode()
    qr_name = f"qrcode_{name}.png" if name else "qrcode.png"
    qr_path = os.path.join(os.path.dirname(COOKIE_PATH), qr_name)
    
    from auth import generate_qrcode_image
    generate_qrcode_image(url, qr_path)
    
    print(f"[Login] 二维码已保存: {qr_path}")
    print(f"[Login] 备用链接: {url}")
    print("[Login] 请用B站APP扫描二维码并确认登录")
    print("[Login] 开始轮询，最长等待5分钟...")

    # 持续轮询
    max_wait = 300  # 5分钟
    start_time = time.time()
    last_status = None
    
    while time.time() - start_time < max_wait:
        result = poll_qrcode(qrcode_key, client)
        code = result.get("data", {}).get("code", -1)
        
        if code == 0:
            # 从扫码响应中提取 refresh_token（用于后续 cookie 自动刷新）
            refresh_token = result.get("data", {}).get("refresh_token", "")
            if refresh_token:
                client._refresh_token = refresh_token
            print("\n[Login] 登录成功!")
            save_cookie(client, cookie_path)
            print(f"[Login] Cookie已保存: {cookie_path}")
            # 登录成功后顺手删除临时二维码图片（避免残留过期二维码造成混淆）
            try:
                os.remove(qr_path)
            except OSError:
                pass
            return
        elif code == 86101:
            if last_status != 86101:
                print("[Login] 等待扫码...")
                last_status = 86101
        elif code == 86090:
            if last_status != 86090:
                print("[Login] 已扫码，等待APP确认...")
                last_status = 86090
        elif code == 86038:
            print("[Login] 二维码已过期")
            return
        else:
            msg = result.get("data", {}).get("message", "未知")
            if last_status != code:
                print(f"[Login] 状态: {msg} (code={code})")
                last_status = code
        
        time.sleep(3)
    
    print("[Login] 轮询超时（5分钟），登录失败")


if __name__ == "__main__":
    main()
