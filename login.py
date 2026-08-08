#!/usr/bin/env python3
"""
B站扫码登录工具（交互式）

用法:
    python login.py
    
步骤:
    1. 程序生成二维码
    2. 用B站APP扫描二维码
    3. 在APP上点击确认登录
    4. 回到终端按 Enter 键
    5. 程序验证并保存 Cookie
"""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from auth import get_qrcode, poll_qrcode, save_cookie, verify_cookie, load_cookie, COOKIE_PATH
from api_client import BiliAPIClient


def main():
    print("=" * 50)
    print("  B站扫码登录工具")
    print("=" * 50)

    # 检查已有cookie
    client = BiliAPIClient()
    cookie_dict = load_cookie()
    if cookie_dict:
        # _refresh_token 是本地保存的伪 cookie，需先弹出，避免注入 session 发给B站
        refresh_token = cookie_dict.pop("_refresh_token", None)
        client.update_cookies(cookie_dict)
        if refresh_token:
            client._refresh_token = refresh_token
        if verify_cookie(client):
            print("\n[✓] 已有有效Cookie，无需重新登录")
            print(f"    Cookie文件: {COOKIE_PATH}")
            return
        print("\n[!] 已有Cookie已过期，重新登录...")

    # 生成二维码
    print("\n[1] 正在获取登录二维码...")
    url, qrcode_key = get_qrcode()
    qr_path = os.path.join(os.path.dirname(COOKIE_PATH), "qrcode.png")
    
    from auth import generate_qrcode_image
    generate_qrcode_image(url, qr_path)

    print("\n" + "=" * 50)
    print("  请使用B站APP扫描上方二维码")
    print("=" * 50)
    print(f"  二维码图片: {qr_path}")
    print(f"  备用链接: {url}")
    print("\n  步骤:")
    print("    1. 打开B站APP")
    print("    2. 点击右上角【扫描】图标")
    print("    3. 扫描上方二维码（或打开图片扫描）")
    print("    4. 在APP上点击【确认登录】")
    print("=" * 50)

    # 等待用户确认
    input("\n[2] 完成APP确认后，请按 Enter 键继续...")

    # 验证登录
    print("\n[3] 正在验证登录状态...")
    result = poll_qrcode(qrcode_key, client)
    code = result.get("data", {}).get("code", -1)

    if code == 0:
        # 从扫码响应中提取 refresh_token（用于后续 cookie 自动刷新）
        refresh_token = result.get("data", {}).get("refresh_token", "")
        if refresh_token:
            client._refresh_token = refresh_token
        print("[✓] 登录成功!")
        save_cookie(client)
        print(f"\n  Cookie已保存到: {COOKIE_PATH}")
        print("  现在可以运行: python run.py BVxxxxxxxx")
        return
    elif code == 86090:
        print("[!] 已扫码但未确认，请先在APP上点击确认")
    elif code == 86101:
        print("[!] 尚未扫码，请先扫码")
    elif code == 86038:
        print("[✗] 二维码已过期，请重新运行本程序")
    else:
        print(f"[✗] 登录失败 (code={code}): {result.get('data', {}).get('message', '未知错误')}")

    # 如果还没成功，再试一次验证
    if code != 0:
        print("\n[4] 再次尝试验证...")
        time.sleep(2)
        result = poll_qrcode(qrcode_key, client)
        code = result.get("data", {}).get("code", -1)
        if code == 0:
            # 从扫码响应中提取 refresh_token（用于后续 cookie 自动刷新）
            refresh_token = result.get("data", {}).get("refresh_token", "")
            if refresh_token:
                client._refresh_token = refresh_token
            print("[✓] 登录成功!")
            save_cookie(client)
            print(f"\n  Cookie已保存到: {COOKIE_PATH}")
            print("  现在可以运行: python run.py BVxxxxxxxx")
        else:
            print("[✗] 登录最终失败，请重新运行本程序")


if __name__ == "__main__":
    main()
