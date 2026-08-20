"""
分析完毕自动启动 Web 报告（run.py 与 quick_test.py 共用入口）

spec C：分析结束后探测本地服务→没有则分离启动 web.py→浏览器打开报告页；
任何一步失败都只打印 URL 提示手动访问，绝不影响主流程退出码。
"""
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser

from config import WEB_AUTOSTART, DATA_DIR


def maybe_launch_web(bvid: str):
    """分析结束后自动起 web.py 并用浏览器打开报告页。

    WEB_AUTOSTART=False 时只打印手动提示；已有服务在跑则跳过启动直接开页。
    """
    port = int(os.environ.get("PROFILER_PORT", "8000"))
    url = f"http://127.0.0.1:{port}/video/{bvid}"
    if not WEB_AUTOSTART:
        print(f"  运行 python web.py 后访问报告页: {url}")
        return

    # 1 秒超时探测：无服务时快速失败，不阻塞主流程收尾
    def _alive() -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1):
                return True
        except Exception:
            return False

    try:
        if not _alive():
            # 分离启动：start_new_session 脱离本进程生命周期，日志重定向到 data/web.log
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            log_path = os.path.join(DATA_DIR, "web.log")
            log_f = open(log_path, "a", encoding="utf-8")
            subprocess.Popen([sys.executable, os.path.join(root, "web.py")],
                             cwd=root, stdout=log_f, stderr=subprocess.STDOUT,
                             start_new_session=True)
            # 等服务就绪（最多约 5 秒）
            for _ in range(10):
                if _alive():
                    break
                time.sleep(0.5)
            if not _alive():
                print(f"  [Web] web.py 启动超时，日志见 {log_path}")
        if _alive():
            # 终端下拉起 GTK 系浏览器会打印 "Not loading module atk-bridge" 警告
            # （at-spi 无障碍桥未加载），无害但脏输出；GTK 读到该环境变量即跳过模块
            os.environ.setdefault("NO_AT_BRIDGE", "1")
            # webbrowser.open 在无图形环境返回 False/抛异常，降级打印 URL
            if webbrowser.open(url):
                print(f"  报告页已在浏览器打开: {url}")
                print("  停止后台 web 服务: python web.py --stop")
            else:
                print(f"  浏览器打开失败，请手动访问: {url}")
        else:
            print(f"  请手动运行 python web.py 后访问: {url}")
    except Exception as e:
        # 自动启动是锦上添花：任何异常都不影响分析结果与退出码
        print(f"  [Web] 自动启动失败（{e}），请手动运行 python web.py 后访问: {url}")
