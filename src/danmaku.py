"""
弹幕采集与解析模块
"""
from collections import defaultdict
from lxml import etree
from typing import Optional

from api_client import BiliAPIClient
from config import VIDEO_INFO_URL, DANMAKU_XML_URL, MAX_ANALYZE_USERS


def get_video_info(bvid: str, client: BiliAPIClient) -> dict:
    """获取视频基础信息"""
    data = client.get(VIDEO_INFO_URL, params={"bvid": bvid})
    if data.get("code") != 0:
        raise Exception(f"获取视频信息失败: {data.get('message')}")
    return data["data"]


def get_cid_for_page(video_info: dict, page_index: int = 0) -> int:
    """获取指定分P的cid"""
    pages = video_info.get("pages", [])
    if not pages:
        return video_info.get("cid", 0)
    if 0 <= page_index < len(pages):
        return pages[page_index]["cid"]
    return pages[0]["cid"]


# 防御性XML解析器：禁用外部实体与网络访问，防XXE/实体膨胀攻击
_SAFE_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)


def parse_danmaku_xml(xml_bytes: bytes) -> list[dict]:
    """
    解析B站弹幕XML
    
    d标签属性: p="时间,模式,字号,颜色,时间戳,池,用户ID(hash),弹幕ID"
    """
    root = etree.fromstring(xml_bytes, parser=_SAFE_XML_PARSER)
    # 根标签必须是 <i>（B站弹幕XML格式）；良构的错误页（如<html>）会被静默解析为0条弹幕，
    # 这里抛出让上层把该页计入失败
    if root.tag != "i":
        raise etree.XMLSyntaxError(f"非弹幕XML（根标签<{root.tag}>）", 0, 0, 0)
    danmaku_list = []

    for d in root.findall(".//d"):
        p_attr = d.get("p", "")
        content = d.text or ""
        attrs = p_attr.split(",")
        if len(attrs) < 8:
            continue

        try:
            # 发送者标识固定为8位CRC32十六进制hash，统一按hex处理。
            # 注意：不能用isdigit()特判——约2.3%的CRC32 hash恰好全是十进制数字
            # （如"12345678"），误判为明文数字UID转码后会产生完全不同的值。
            mid_hash = attrs[6].lower()

            danmaku_list.append({
                "content": content,
                "time": float(attrs[0]),         # 视频内出现时间(秒)
                "mode": int(attrs[1]),            # 弹幕模式
                "fontsize": int(attrs[2]),        # 字号
                "color": f"#{int(attrs[3]):06x}", # 颜色
                "timestamp": int(attrs[4]),       # 发送时间戳
                "pool": int(attrs[5]),            # 弹幕池
                "mid_hash": mid_hash,             # 发送者标识（8位CRC32十六进制hash）
                "dmid": int(attrs[7]) if attrs[7].isdigit() else 0,
            })
        except (ValueError, IndexError):
            continue

    return danmaku_list


def fetch_danmaku(cid: int, client: BiliAPIClient) -> list[dict]:
    """获取单cid的弹幕列表"""
    resp = client.get_raw(DANMAKU_XML_URL, params={"oid": cid})
    resp.encoding = "utf-8"
    return parse_danmaku_xml(resp.content)


def fetch_all_danmaku(video_info: dict, client: BiliAPIClient) -> list[dict]:
    """获取视频所有分P的弹幕（单页失败只跳过该页，不中断整体采集）"""
    pages = video_info.get("pages", [])
    if not pages:
        pages = [{"cid": video_info.get("cid", 0), "page": 1}]

    all_danmaku = []
    failed_pages = []
    for idx, page in enumerate(pages):
        cid = page["cid"]
        try:
            dms = fetch_danmaku(cid, client)
        except Exception as e:
            # 降级而非中断：网络抖动或412错误页（非XML）等单页异常仅跳过该分P
            failed_pages.append(idx + 1)
            print(f"[Danmaku] 警告：分P {idx + 1} 弹幕采集失败，已跳过该页: {e}")
            continue
        for dm in dms:
            dm["page"] = idx + 1
        all_danmaku.extend(dms)

    if failed_pages:
        print(f"[Danmaku] 警告：共 {len(failed_pages)}/{len(pages)} 页采集失败"
              f"（失败分P: {failed_pages}），弹幕数据可能不完整")
    if len(failed_pages) == len(pages) and pages:
        print("[Danmaku] 【严重警告】所有分P弹幕均采集失败，返回空弹幕列表！")

    return all_danmaku


def group_by_sender(danmaku_list: list[dict]) -> dict[str, dict]:
    """
    按发送者聚合弹幕
    
    Returns:
        {mid_hash: {
            "mid_hash": str,
            "count": int,
            "contents": [str],
            "timestamps": [int],
            "video_times": [float],
            "colors": [str],
            "pages": [int],
        }}
    """
    groups = defaultdict(lambda: {
        "mid_hash": "",
        "count": 0,
        "contents": [],
        "timestamps": [],
        "video_times": [],
        "colors": [],
        "pages": set(),
    })

    for dm in danmaku_list:
        mh = dm["mid_hash"]
        g = groups[mh]
        g["mid_hash"] = mh
        g["count"] += 1
        g["contents"].append(dm["content"])
        g["timestamps"].append(dm["timestamp"])
        g["video_times"].append(dm["time"])
        g["colors"].append(dm["color"])
        g["pages"].add(dm.get("page", 1))

    # 将set转为list以便JSON序列化
    for g in groups.values():
        g["pages"] = sorted(g["pages"])

    return dict(groups)


def get_top_senders(sender_groups: dict[str, dict], max_users: int = MAX_ANALYZE_USERS) -> list[str]:
    """
    按弹幕数量降序，获取前N个发送者
    巨量数据时只分析活跃发送者
    """
    sorted_senders = sorted(
        sender_groups.keys(),
        key=lambda mh: sender_groups[mh]["count"],
        reverse=True
    )
    return sorted_senders[:max_users]


def collect_danmaku_data(bvid: str, client: BiliAPIClient) -> tuple[dict, list[dict], dict[str, dict]]:
    """
    采集视频完整弹幕数据
    
    Returns:
        (video_info, danmaku_list, sender_groups)
    """
    print(f"[Danmaku] 获取视频信息: {bvid}")
    video_info = get_video_info(bvid, client)
    title = video_info.get("title", "")
    print(f"[Danmaku] 视频: {title}")

    print(f"[Danmaku] 获取弹幕...")
    danmaku_list = fetch_all_danmaku(video_info, client)
    print(f"[Danmaku] 获取到 {len(danmaku_list)} 条弹幕")

    sender_groups = group_by_sender(danmaku_list)
    print(f"[Danmaku] 独立发送者: {len(sender_groups)} 人")

    return video_info, danmaku_list, sender_groups
