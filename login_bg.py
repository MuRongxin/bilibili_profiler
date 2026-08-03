#!/usr/bin/env python3
"""
B站扫码登录后台轮询（非交互式）
持续轮询二维码状态，直到登录成功或超时。
"""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from auth import get_qrcode, poll_qrcode, save_cookie, verify_cookie, load_cookie, COOKIE_PATH
from api_client import BiliAPIClient


def main():
    client = BiliAPIClient()
    
    # 检查已有cookie
    cookie_dict = load_cookie()
    if cookie_dict:
        client.update_cookies(cookie_dict)
        if verify_cookie(client):
            print("[Login] 已有有效Cookie")
            return

    # 生成二维码
    print("[Login] 获取二维码...")
    url, qrcode_key = get_qrcode()
    qr_path = os.path.join(os.path.dirname(COOKIE_PATH), "qrcode.png")
    
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
            save_cookie(client)
            print(f"[Login] Cookie已保存: {COOKIE_PATH}")
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
