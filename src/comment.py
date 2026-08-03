"""
评论采集模块：提取评论区明文UID，用于mid_hash交叉验证
"""
from api_client import BiliAPIClient
from config import COMMENT_MAIN_URL, COMMENT_REPLY_URL, MAX_COMMENT_PAGES


def fetch_comments(oid: int, client: BiliAPIClient, max_pages: int = MAX_COMMENT_PAGES) -> list[dict]:
    """
    获取视频评论列表（主评论+子评论）
    
    Args:
        oid: 视频aid
        max_pages: 最大翻页数
    
    Returns:
        评论列表，每条包含uid、uname、level、content等
    """
    all_comments = []
    next_page = 0

    for page in range(1, max_pages + 1):
        data = client.get(COMMENT_MAIN_URL, params={
            "type": 1,
            "oid": oid,
            "mode": 3,       # 按热度排序
            "next": next_page,
            "ps": 20,
        })

        if data.get("code") != 0:
            print(f"[Comment] 获取评论失败: {data.get('message')}")
            break

        # 防御 data["data"] 为 None（风控/空结果时 API 会返回 data: null）
        page_data = data.get("data") or {}
        replies = page_data.get("replies") or []
        if not replies:
            break

        for r in replies:
            if not r or not r.get("member"):
                continue

            member = r["member"]
            mid = member.get("mid", 0)
            if mid == 0:
                continue

            all_comments.append({
                "uid": mid,
                "uname": member.get("uname", ""),
                "sign": member.get("sign", ""),
                "level": (member.get("level_info") or {}).get("current_level", 0),
                "avatar": member.get("avatar", ""),
                "content": r.get("content", {}).get("message", ""),
                "like": r.get("like", 0),
                "reply_count": r.get("rcount", 0),
                "ctime": r.get("ctime", 0),
                "is_sub": False,
            })

            # 子评论
            sub_replies = r.get("replies", []) or []
            for sub in sub_replies:
                if not sub or not sub.get("member"):
                    continue
                sub_member = sub["member"]
                sub_mid = sub_member.get("mid", 0)
                if sub_mid == 0:
                    continue

                all_comments.append({
                    "uid": sub_mid,
                    "uname": sub_member.get("uname", ""),
                    "sign": sub_member.get("sign", ""),
                    "level": (sub_member.get("level_info") or {}).get("current_level", 0),
                    "avatar": sub_member.get("avatar", ""),
                    "content": sub.get("content", {}).get("message", ""),
                    "like": sub.get("like", 0),
                    "reply_count": 0,
                    "ctime": sub.get("ctime", 0),
                    "is_sub": True,
                })

        cursor = page_data.get("cursor") or {}
        next_page = cursor.get("next", 0)
        if cursor.get("is_end", False) or not next_page:
            break

    return all_comments


def build_comment_uid_map(comments: list[dict]) -> dict[str, int]:
    """
    构建 CRC32(mid) -> mid 映射表，用于弹幕mid_hash交叉验证
    
    Returns:
        {crc32_hash: uid} 映射字典
    """
    import zlib
    uid_map = {}
    seen_uids = set()

    for c in comments:
        uid = c["uid"]
        if uid in seen_uids:
            continue
        seen_uids.add(uid)

        # 计算该UID的CRC32（B站使用的标准CRC32）
        crc = format(zlib.crc32(str(uid).encode()) & 0xFFFFFFFF, "08x")
        # CRC32碰撞时保留先见者并告警，避免弹幕发送者被误归属到后到的用户
        if crc in uid_map and uid_map[crc] != uid:
            print(f"[评论] 警告: CRC32碰撞 {crc}，已归属UID {uid_map[crc]}，忽略 {uid}")
        else:
            uid_map.setdefault(crc, uid)

    return uid_map


def collect_comment_data(aid: int, client: BiliAPIClient) -> tuple[list[dict], dict[str, int]]:
    """
    采集评论数据并构建UID映射
    
    Returns:
        (comments_list, crc32_to_uid_map)
    """
    print(f"[Comment] 获取评论区 (AID:{aid})...")
    comments = fetch_comments(aid, client)
    uid_map = build_comment_uid_map(comments)
    print(f"[Comment] 获取到 {len(comments)} 条评论，提取 {len(uid_map)} 个独立用户UID")
    return comments, uid_map
