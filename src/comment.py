"""
评论采集模块：提取评论区明文UID，用于mid_hash交叉验证

主评论走新接口 /x/v2/reply/wbi/main（wbi签名 + pagination_str 游标翻页），
失败时降级回旧接口 /x/v2/reply/main（next 游标）；子评论在主评论 rcount
超过内嵌预览数时调 /x/v2/reply/reply 按 pn 补采。每条评论提取 IP 属地
（reply_control.location）。
"""
import json
import zlib

from api_client import BiliAPIClient
from config import COMMENT_MAIN_WBI_URL, COMMENT_MAIN_URL, COMMENT_REPLY_URL
from config import MAX_COMMENT_PAGES, COMMENT_REPLY_MAX_PAGES


def _parse_comment(r: dict, is_sub: bool) -> dict | None:
    """解析单条评论（主/子结构相同），无有效成员信息时返回 None"""
    if not r or not r.get("member"):
        return None

    member = r["member"]
    # member.mid 实测为 str（如 '888465'），统一归一化为 int：SQLite 缓存路径的
    # uid 是 int，str/int 混杂会导致属地注入（uid in location_map）与跨 run
    # 去重（seen_uids）静默失效；转换失败的脏数据按无有效成员处理
    try:
        mid = int(member.get("mid") or 0)
    except (TypeError, ValueError):
        return None
    if mid == 0:
        return None

    return {
        "uid": mid,
        "rpid": r.get("rpid", 0),
        "uname": member.get("uname", ""),
        "sign": member.get("sign", ""),
        "level": (member.get("level_info") or {}).get("current_level", 0),
        "avatar": member.get("avatar", ""),
        "content": (r.get("content") or {}).get("message", ""),
        "like": r.get("like", 0),
        "reply_count": 0 if is_sub else r.get("rcount", 0),
        "ctime": r.get("ctime", 0),
        "is_sub": is_sub,
        "location": (r.get("reply_control") or {}).get("location", ""),
    }


def _fetch_sub_replies(oid: int, root_rpid, rcount: int, preview: list[dict],
                       client: BiliAPIClient) -> list[dict]:
    """补采子评论：rcount 超过内嵌预览数时按 pn 翻页拉取全量子评论。

    补采成功（拿到比预览更多的子评论）时以补采结果替换预览（reply/reply 返回
    全量通常含预览条目，替换避免重复）；但 rcount 超过补采上限被截断时，按热度
    排序的预览条目可能不在已采前 N 页内，需把缺失的预览条目追加回去。
    补采失败/无新增时保留预览，降级不中断。
    """
    if rcount <= len(preview):
        return preview

    fetched = []
    for pn in range(1, COMMENT_REPLY_MAX_PAGES + 1):
        data = client.get(COMMENT_REPLY_URL, params={
            "type": 1,
            "oid": oid,
            "root": root_rpid,
            "pn": pn,
            "ps": 20,
        })
        if data.get("code") != 0:
            print(f"[Comment] 子评论补采失败 (root={root_rpid} pn={pn}): {data.get('message')}，保留预览")
            break

        # 防御 data["data"] 为 None（风控/空结果时 API 会返回 data: null）
        page_data = data.get("data") or {}
        replies = page_data.get("replies") or []
        for sub in replies:
            c = _parse_comment(sub, is_sub=True)
            if c:
                fetched.append(c)

        # data.page.count 为子评论总数，翻够页数或本页为空即终止
        total = (page_data.get("page") or {}).get("count", 0)
        if not replies or pn * 20 >= total:
            break

    if len(fetched) <= len(preview):
        return preview

    # 截断边界：rcount 超过补采上限时，按热度排序的预览条目可能不在已采
    # 结果内，把缺失的预览条目（按 rpid 判重）追加回去，避免丢其 UID/属地
    fetched_rpids = {c["rpid"] for c in fetched}
    for c in preview:
        if c["rpid"] not in fetched_rpids:
            fetched.append(c)
    return fetched


def _collect_page(replies: list, oid: int, client: BiliAPIClient) -> list[dict]:
    """解析一页主评论及其子评论（含补采）"""
    comments = []
    for r in replies:
        main = _parse_comment(r, is_sub=False)
        if not main:
            continue
        comments.append(main)

        # 内嵌预览子评论
        preview = []
        for sub in r.get("replies") or []:
            c = _parse_comment(sub, is_sub=True)
            if c:
                preview.append(c)

        comments.extend(_fetch_sub_replies(oid, r.get("rpid"), main["reply_count"], preview, client))
    return comments


def _fetch_comments_wbi(oid: int, client: BiliAPIClient, max_pages: int) -> list[dict] | None:
    """wbi/main 游标翻页采集主评论。首页即失败返回 None（调用方降级旧接口）"""
    all_comments = []
    offset = ""
    seen_rpids = set()  # 已采评论的 rpid：该接口的 next_offset 可能连续多页相同但内容不同（实测），
                        # 只有"整页无新 rpid"才是真的重复页

    for page in range(1, max_pages + 1):
        data = client.get(COMMENT_MAIN_WBI_URL, params={
            "oid": oid,
            "type": 1,
            "mode": 3,       # 按热度排序
            # pagination_str 为 JSON 字符串参数，client.get 的 params 会 urlencode
            "pagination_str": json.dumps({"offset": offset}),
        })

        if data.get("code") != 0:
            print(f"[Comment] wbi/main 获取评论失败 (第{page}页): {data.get('message')}")
            if page == 1:
                return None  # 首页即失败（签名/风控等），整体降级旧接口
            break            # 中途失败保留已采部分

        # 防御 data["data"] 为 None（风控/空结果时 API 会返回 data: null）
        page_data = data.get("data") or {}
        replies = page_data.get("replies") or []
        if not replies:
            break

        # 真重复页检测：整页 rpid 都已见过才终止（next_offset 重复不代表内容重复，实测确认）
        new_replies = [r for r in replies if r.get("rpid") not in seen_rpids]
        if not new_replies:
            print(f"[Comment] wbi/main 第{page}页无新评论（真重复页），终止翻页")
            break
        seen_rpids.update(r.get("rpid") for r in new_replies)

        # 翻页：cursor.pagination_reply.next_offset 为不透明游标字符串，is_end 终止
        cursor = page_data.get("cursor") or {}
        next_offset = (cursor.get("pagination_reply") or {}).get("next_offset")

        all_comments.extend(_collect_page(new_replies, oid, client))

        if cursor.get("is_end", False) or not next_offset:
            break
        offset = next_offset

    return all_comments


def _fetch_comments_legacy(oid: int, client: BiliAPIClient, max_pages: int) -> list[dict]:
    """旧接口 /x/v2/reply/main（next 游标）采集，作为 wbi/main 失败时的降级路径"""
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

        all_comments.extend(_collect_page(replies, oid, client))

        cursor = page_data.get("cursor") or {}
        next_page = cursor.get("next", 0)
        if cursor.get("is_end", False) or not next_page:
            break

    return all_comments


def fetch_comments(oid: int, client: BiliAPIClient, max_pages: int = MAX_COMMENT_PAGES) -> list[dict]:
    """
    获取视频评论列表（主评论+子评论）

    优先走 wbi/main 游标接口；首页即失败时降级回旧 /x/v2/reply/main 接口。

    Args:
        oid: 视频aid
        max_pages: 最大翻页数

    Returns:
        评论列表，每条包含uid、uname、level、content、location等
    """
    comments = _fetch_comments_wbi(oid, client, max_pages)
    if comments is None:
        print("[Comment] wbi/main 接口不可用，降级为旧版 /x/v2/reply/main")
        comments = _fetch_comments_legacy(oid, client, max_pages)
    return comments


def build_comment_uid_map(comments: list[dict]) -> dict[str, int]:
    """
    构建 CRC32(mid) -> mid 映射表，用于弹幕mid_hash交叉验证

    Returns:
        {crc32_hash: uid} 映射字典
    """
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


def build_comment_location_map(comments: list[dict]) -> dict[int, str]:
    """
    构建 uid -> IP属地 映射表（如 "IP属地：江苏"），供画像地域维度使用

    同一用户多条评论属地不一致时保留先见者（与 build_comment_uid_map 同策略）。
    """
    location_map = {}
    for c in comments:
        uid = c["uid"]
        location = c.get("location", "")
        if location and uid not in location_map:
            location_map[uid] = location
    return location_map


def collect_comment_data(aid: int, client: BiliAPIClient) -> tuple[list[dict], dict[str, int], dict[int, str]]:
    """
    采集评论数据并构建UID映射与IP属地映射

    Returns:
        (comments_list, crc32_to_uid_map, uid_to_location_map)
    """
    print(f"[Comment] 获取评论区 (AID:{aid})...")
    comments = fetch_comments(aid, client)
    uid_map = build_comment_uid_map(comments)
    location_map = build_comment_location_map(comments)
    sub_count = sum(1 for c in comments if c.get("is_sub"))
    print(f"[Comment] 获取到 {len(comments)} 条评论（含子评论 {sub_count} 条），"
          f"提取 {len(uid_map)} 个独立用户UID，{len(location_map)} 个IP属地")
    return comments, uid_map, location_map
