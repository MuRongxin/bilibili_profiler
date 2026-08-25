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
import socket
import subprocess
import sys
import threading
import time

import requests

from clash_ctl import ClashCtl
from config import BASE_DIR, DATA_DIR, MIHOMO_PATH

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
        tmp = dest + ".tmp"
        try:
            print(f"[ProxyCore] 下载 mihomo 核心: {url}")
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            data = gzip.decompress(r.content)   # 先解压再落盘，避免截断/0字节残留
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(data)
            os.chmod(tmp, 0o755)
            os.replace(tmp, dest)   # 原子替换：不存在半成品 dest
            return True
        except Exception as e:
            print(f"[ProxyCore] 下载失败（{name}）: {e}")
            if os.path.exists(tmp):
                os.remove(tmp)
    return False


def ensure_binary(allow_download: bool = True) -> str | None:
    """定位 mihomo 二进制：MIHOMO_PATH → vendor/mihomo → data/mihomo → 自动下载"""
    candidates = [MIHOMO_PATH] if MIHOMO_PATH else []
    exe = "mihomo.exe" if sys.platform.startswith("win") else "mihomo"
    candidates += [os.path.join(BASE_DIR, "vendor", exe), os.path.join(DATA_DIR, exe)]
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
        self._raw_sub_urls = list(sub_urls)   # 原始入参，供 get_core 单例做参数比对
        # 剔除会生成非法 yaml 的链接（含双引号/反斜杠/换行；双引号标量中 \ 是转义符），
        # 避免静默失效；警告只打印序号，凭证不落日志
        self.sub_urls = []
        for i, u in enumerate(sub_urls):
            if '"' in u or "\\" in u or "\n" in u or "\r" in u:
                print(f"[ProxyCore] 警告：第 {i} 条订阅链接含非法字符（引号/反斜杠/换行），已剔除")
            else:
                self.sub_urls.append(u)
        # 组名同样要进 yaml：含引号/反斜杠/换行会生成非法配置或与 self.group 不一致的组名，
        # 回退默认组名（\ 结尾会生成未终止字符串；\n/\t 字面两字符会被 yaml 解析成转义）
        if not group:
            group = "profiler"   # 未配置组名，直接用默认（非异常，不打警告）
        elif '"' in group or "\\" in group or "\n" in group or "\r" in group:
            print("[ProxyCore] 警告：节点组名含非法字符（引号/反斜杠/换行），回退默认组名 profiler")
            group = "profiler"
        self.group = group
        self.mix_port = _free_port()
        self.api_port = _free_port()
        while self.api_port == self.mix_port:   # 理论上可撞，守卫一下
            self.api_port = _free_port()
        self.secret = secrets.token_hex(8)
        self._proc: subprocess.Popen | None = None
        self._start_lock = threading.Lock()   # start() 串行化：并发建池不会拉起多个核心
        self._atexit_registered = False       # atexit.register 只注册一次，避免叠加调用

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
            # 显式禁 LAN + 只绑回环：把"只监听 127.0.0.1"从依赖上游默认值变成显式承诺
            f"allow-lan: false\n"
            f"bind-address: 127.0.0.1\n"
            f"external-controller: 127.0.0.1:{self.api_port}\n"
            f'secret: "{self.secret}"\n'
            f"log-level: warning\n"
            f"proxy-providers:\n{providers}\n"
            f"proxy-groups:\n"
            f'  - name: "{self.group}"\n'
            f"    type: select\n"
            f"    use: [{uses}]\n"
            f"rules:\n"
            f'  - "MATCH,{self.group}"\n'
        )

    def start(self, allow_download: bool = True) -> ClashCtl | None:
        """拉起核心子进程并轮询就绪（核心需先拉订阅，最长等 60s）；失败返回 None。

        幂等：已启动且存活（poll() 为 None）直接复用返回既有 ClashCtl，不重复拉起
        （web.py 重新生成 job 会反复 build_pool，进程内只允许一个内置核心）。
        整段在锁内：并发调用者阻塞等待首个调用拉起完成后走复用分支。
        """
        with self._start_lock:
            if self._proc is not None:
                if self._proc.poll() is None:
                    print(f"[ProxyCore] 内置核心已在运行（混合端口 127.0.0.1:{self.mix_port}），复用")
                    return ClashCtl(f"http://127.0.0.1:{self.api_port}", self.secret, self.group)
                self._proc = None       # 进程已死，按重新拉起处理（崩溃自愈）
            if not self.sub_urls:
                print("[ProxyCore] 无可用订阅链接（全部被剔除或未配置），禁用内置核心")
                return None
            bin_path = ensure_binary(allow_download=allow_download)
            if not bin_path:
                print("[ProxyCore] 未找到 mihomo 二进制（可设 MIHOMO_PATH 或放置到 vendor/），禁用内置核心")
                return None
            try:
                os.makedirs(_RUNTIME_DIR, exist_ok=True)
                cfg_path = os.path.join(_RUNTIME_DIR, "config.yaml")
                with open(cfg_path, "w", encoding="utf-8") as f:
                    f.write(self._gen_config())
                os.chmod(cfg_path, 0o600)   # 含订阅链接，收紧权限
            except OSError as e:
                print(f"[ProxyCore] 配置写入失败: {e}，禁用内置核心")
                return None
            try:
                self._proc = subprocess.Popen(
                    [bin_path, "-d", _RUNTIME_DIR, "-f", cfg_path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                print(f"[ProxyCore] 核心启动失败: {e}")
                return None
            if not self._atexit_registered:
                atexit.register(self.stop)
                self._atexit_registered = True
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


_CORE_INSTANCE: ProxyCore | None = None
_CORE_LOCK = threading.Lock()


def get_core(sub_urls: list[str], group: str = "profiler") -> ProxyCore:
    """模块级单例入口：同一进程只持有一个内置核心实例（配合 start() 幂等复用，
    web.py 多次重新生成 job 反复 build_pool 不会累积拉起多个 mihomo 子进程）。
    参数与既有实例不同则打警告并复用旧实例（同一进程内配置一般不会变，重建反而泄漏旧核心）。
    """
    global _CORE_INSTANCE
    with _CORE_LOCK:
        if _CORE_INSTANCE is None:
            _CORE_INSTANCE = ProxyCore(sub_urls, group=group)
        elif _CORE_INSTANCE._raw_sub_urls != list(sub_urls) or _CORE_INSTANCE.group != group:
            print("[ProxyCore] 警告：内置核心已按先前参数创建，本次参数不同，复用既有实例")
    return _CORE_INSTANCE
