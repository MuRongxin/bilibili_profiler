"""Clash/mihomo 外部控制器封装：列节点/切节点（换出口 IP）

所有失败静默降级（返回 False/[]/None），不抛给上层——IP 池不可用时由 ComboPool 走降级链。
注意：直连控制器（requests 裸调用），不走 BiliAPIClient（无 B 站限速语义）。
"""
import requests

# 组内伪节点/内置策略名，不进入轮换
_PSEUDO = {"DIRECT", "REJECT", "PASS", "GLOBAL", "COMPATIBLE"}


class ClashCtl:
    def __init__(self, api_url: str, secret: str = "", group: str = ""):
        self.api_url = api_url.rstrip("/")
        self.secret = secret
        self.group = group          # 为空时首次读组自动挑选第一个非 GLOBAL 的 Selector 组
        self._nodes: list[str] = []
        self._cursor = 0

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.secret}"} if self.secret else {}

    def available(self) -> bool:
        """控制器连通性自检"""
        try:
            r = requests.get(f"{self.api_url}/proxies", headers=self._headers(), timeout=3)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def _fetch_group(self) -> dict:
        """读节点组信息；group 为空时自动挑选第一个非 GLOBAL、含节点的 Selector 组。
        返回 {"name", "all", "now"}；请求异常向上抛（由调用方静默降级）。"""
        r = requests.get(f"{self.api_url}/proxies", headers=self._headers(), timeout=5)
        r.raise_for_status()
        proxies = (r.json() or {}).get("proxies") or {}
        if self.group:
            g = proxies.get(self.group) or {}
            return {"name": self.group, "all": g.get("all") or [], "now": g.get("now", "")}
        for name, g in proxies.items():
            if name == "GLOBAL":
                continue
            if (g or {}).get("type") == "Selector" and (g.get("all") or []):
                self.group = name
                return {"name": name, "all": g["all"], "now": g.get("now", "")}
        return {"name": "", "all": [], "now": ""}

    def list_nodes(self) -> list[str]:
        """组内可轮换节点（过滤伪节点）；失败返回 []"""
        try:
            g = self._fetch_group()
        except requests.RequestException:
            return []
        return [n for n in g["all"] if n and n.upper() not in _PSEUDO]

    def refresh_nodes(self) -> list[str]:
        """刷新并缓存节点列表"""
        self._nodes = self.list_nodes()
        return self._nodes

    def current_node(self) -> str | None:
        try:
            return self._fetch_group()["now"] or None
        except requests.RequestException:
            return None

    def pick_next_node(self) -> str | None:
        """组内轮询推进，跳过当前节点；节点为空返回 None"""
        if not self._nodes:
            self.refresh_nodes()
        if not self._nodes:
            return None
        cur = self.current_node()
        self._cursor = (self._cursor + 1) % len(self._nodes)
        if cur and len(self._nodes) > 1:
            for _ in range(len(self._nodes)):
                if self._nodes[self._cursor] != cur:
                    break
                self._cursor = (self._cursor + 1) % len(self._nodes)
        return self._nodes[self._cursor]

    def switch_node(self, name: str) -> bool:
        """切换节点组选择（换出口 IP）；失败返回 False"""
        if not name:
            return False
        try:
            g = self._fetch_group()
            if not g["name"]:
                return False
            r = requests.put(
                f"{self.api_url}/proxies/{requests.utils.quote(g['name'], safe='')}",
                headers=self._headers(), json={"name": name}, timeout=5)
            return r.status_code in (200, 204)
        except requests.RequestException:
            return False
