"""
用户深度数据采集模块（四维度）

维度1: 用户主页信息
维度2: 互动内容足迹
维度3: 社交关系网络
维度4: 行为模式分析
"""
import time
from datetime import datetime
from api_client import BiliAPIClient
from config import (
    USER_CARD_URL, USER_SPACE_URL, USER_VIDEOS_URL,
    USER_DYNAMICS_URL, USER_FOLLOWINGS_URL, USER_FOLLOWERS_URL,
    USER_FAV_FOLDERS_URL, USER_FAV_CONTENTS_URL, USER_BANGUMI_URL,
    MAX_VIDEO_PAGES, MAX_DYNAMIC_PAGES, MAX_FOLLOWING_PAGES,
    MAX_FOLLOWER_PAGES, MAX_FAV_CONTENTS, MAX_UP_SAMPLE
)


def _safe_int(v, default=0):
    """B站数值字段可能返回 '--' 等字符串，强转失败时降级为默认值"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ========== 维度1：用户主页信息 ==========

def get_user_card(uid: int, client: BiliAPIClient) -> dict:
    """获取用户空间卡信息（最基础的信息）"""
    data = client.get(USER_CARD_URL, params={"mid": uid})
    if data.get("code") != 0:
        return {"error": data.get("message", "获取失败")}

    card = data.get("data", {}).get("card", {})
    vip = card.get("vip", {})
    official = card.get("official_verify", {})

    return {
        "uid": uid,
        "name": card.get("name", ""),
        "face": card.get("face", ""),
        "sign": card.get("sign", ""),
        "sex": card.get("sex", ""),
        "level": card.get("level_info", {}).get("current_level", 0),
        "vip_type": vip.get("type", 0),        # 1:月度大会员, 2:年度大会员
        "vip_status": vip.get("status", 0),    # 1:有效
        "vip_due_date": vip.get("due_date", 0),
        "official_type": official.get("type", -1),
        "official_title": official.get("desc", ""),
        "follower": card.get("fans", 0),
        "following": card.get("attention", 0),
        "like_num": data.get("data", {}).get("like_num", 0),
        "archive_count": card.get("archive_count", 0),
        "article_count": card.get("article_count", 0),
        # ip_location removed (B站已下线该字段)
    }


def get_user_space_info(uid: int, client: BiliAPIClient) -> dict:
    """获取用户空间详细信息（含硬币数、直播等）"""
    data = client.get(USER_SPACE_URL, params={"mid": uid})
    if data.get("code") != 0:
        return {}

    info = data.get("data", {})
    live = info.get("live_room") or {}

    return {
        "coins": info.get("coins", 0),
        "is_senior_member": info.get("is_senior_member", 0),  # 硬核会员
        "live_room_id": live.get("roomid", 0),
        "live_status": live.get("liveStatus", 0),
        "live_title": live.get("title", ""),
        "live_url": live.get("url", ""),
        "school": (info.get("school") or {}).get("name", ""),
        "profession": (info.get("profession") or {}).get("name", ""),
        "tags": info.get("tags", []),
    }


# ========== 维度2：互动内容足迹 ==========

def get_user_videos(uid: int, client: BiliAPIClient, max_pages: int = MAX_VIDEO_PAGES) -> list[dict]:
    """获取用户投稿视频列表"""
    all_videos = []
    for page in range(1, max_pages + 1):
        data = client.get(USER_VIDEOS_URL, params={
            "mid": uid, "ps": 30, "pn": page, "order": "pubdate"
        })
        if data.get("code") != 0:
            break

        vlist = data.get("data", {}).get("list", {}).get("vlist", [])
        if not vlist:
            break

        for v in vlist:
            all_videos.append({
                "bvid": v.get("bvid", ""),
                "title": v.get("title", ""),
                "description": v.get("description", ""),
                "play": _safe_int(v.get("play", 0)),
                "comment": _safe_int(v.get("comment", 0)),
                "created": _safe_int(v.get("created", 0)),
                "length": v.get("length", ""),
                "typeid": v.get("typeid", 0),
                "tag": v.get("tag", ""),
            })

        total = data.get("data", {}).get("page", {}).get("count", 0)
        if len(all_videos) >= total:
            break

    return all_videos


def get_user_dynamics(uid: int, client: BiliAPIClient, max_pages: int = MAX_DYNAMIC_PAGES) -> list[dict]:
    """获取用户动态列表"""
    all_dynamics = []
    offset = ""

    for _ in range(max_pages):
        params = {"host_mid": uid}
        if offset:
            params["offset"] = offset

        data = client.get(USER_DYNAMICS_URL, params=params)
        if data.get("code") != 0:
            break

        items = data.get("data", {}).get("items", [])
        if not items:
            break

        for item in items:
            modules = item.get("modules", {})
            author = modules.get("module_author", {})
            dynamic = modules.get("module_dynamic", {})
            desc = dynamic.get("desc", {})
            major = dynamic.get("major", {})

            content = desc.get("text", "") if desc else ""
            dyn_type = item.get("type", "")

            # 提取图片
            images = []
            if major and major.get("draw"):
                for img in major["draw"].get("items", []):
                    images.append(img.get("src", ""))

            # 提取视频信息
            video_info = None
            if major and major.get("archive"):
                archive = major["archive"]
                video_info = {
                    "title": archive.get("title", ""),
                    "bvid": archive.get("bvid", ""),
                    "play": _safe_int(archive.get("stat", {}).get("view", 0)),
                }

            stat = modules.get("module_stat", {})
            all_dynamics.append({
                "id": item.get("id_str", ""),
                "type": dyn_type,
                "content": content[:500],
                "images": images[:4],
                "timestamp": author.get("pub_ts", 0),
                "like": _safe_int(stat.get("like", {}).get("count", 0)),
                "comment": _safe_int(stat.get("comment", {}).get("count", 0)),
                "repost": _safe_int(stat.get("forward", {}).get("count", 0)),
                "video_info": video_info,
            })

        offset = data.get("data", {}).get("offset", "")
        if not data.get("data", {}).get("has_more", False):
            break

    return all_dynamics


def get_favorite_folders(uid: int, client: BiliAPIClient) -> list[dict]:
    """获取用户创建的收藏夹"""
    data = client.get(USER_FAV_FOLDERS_URL, params={"up_mid": uid})
    if data.get("code") != 0:
        return []

    folders = []
    data_obj = data.get("data") or {}
    for f in data_obj.get("list", []):
        folders.append({
            "id": f.get("id", 0),
            "title": f.get("title", ""),
            "media_count": _safe_int(f.get("media_count", 0)),
            "attr": f.get("attr", 0),  # 0:公开, 1:私密
        })
    return folders


def get_favorite_contents(media_id: int, client: BiliAPIClient, max_items: int = MAX_FAV_CONTENTS) -> list[dict]:
    """获取收藏夹内容"""
    data = client.get(USER_FAV_CONTENTS_URL, params={
        "media_id": media_id, "ps": max_items, "pn": 1
    })
    if data.get("code") != 0:
        return []

    items = []
    for item in data.get("data", {}).get("medias") or []:
        items.append({
            "id": item.get("id", 0),
            "title": item.get("title", ""),
            "upper": item.get("upper", {}).get("name", ""),
            "type": item.get("type", 0),
            "bvid": item.get("bvid", ""),
            "play": _safe_int(item.get("cnt_info", {}).get("play", 0)),
        })
    return items


# ========== 维度3：社交关系网络 ==========

def get_followings(uid: int, client: BiliAPIClient, max_pages: int = MAX_FOLLOWING_PAGES) -> list[dict]:
    """获取用户关注列表"""
    all_followings = []
    for page in range(1, max_pages + 1):
        data = client.get(USER_FOLLOWINGS_URL, params={
            "vmid": uid, "ps": 20, "pn": page
        })
        if data.get("code") != 0:
            break

        flist = data.get("data", {}).get("list", [])
        if not flist:
            break

        for f in flist:
            all_followings.append({
                "uid": f.get("mid", 0),
                "name": f.get("uname", ""),
                "sign": f.get("sign", ""),
                "official_type": f.get("official", {}).get("type", -1),
                "vip_type": f.get("vip", {}).get("type", 0),
                "face": f.get("face", ""),
            })

        total = data.get("data", {}).get("total", 0)
        if len(all_followings) >= total:
            break

    return all_followings


def get_followers(uid: int, client: BiliAPIClient, max_pages: int = MAX_FOLLOWER_PAGES) -> dict:
    """获取用户粉丝列表（采样）"""
    all_followers = []
    for page in range(1, max_pages + 1):
        data = client.get(USER_FOLLOWERS_URL, params={
            "vmid": uid, "ps": 20, "pn": page
        })
        if data.get("code") != 0:
            break

        flist = data.get("data", {}).get("list", [])
        if not flist:
            break

        for f in flist:
            all_followers.append({
                "uid": f.get("mid", 0),
                "name": f.get("uname", ""),
                "sign": f.get("sign", ""),
                "official_type": f.get("official", {}).get("type", -1),
                "vip_type": f.get("vip", {}).get("type", 0),
            })

        total = data.get("data", {}).get("total", 0)
        if len(all_followers) >= total:
            break

    return {"total": len(all_followers), "sample": all_followers}


# ========== 维度4：行为模式相关 ==========

def get_bangumi_list(uid: int, client: BiliAPIClient, btype: int = 1) -> list[dict]:
    """
    获取用户追番/追剧列表
    btype: 1=番剧, 2=追剧
    """
    try:
        # 新版API endpoint
        data = client.get("https://api.bilibili.com/x/space/bangumi/follow/list", params={
            "vmid": uid, "type": btype, "pn": 1, "ps": 15
        })
    except Exception:
        return []
    if data.get("code") != 0:
        return []

    items = []
    for item in data.get("data", {}).get("list", []):
        items.append({
            "title": item.get("title", ""),
            "season_id": item.get("season_id", 0),
            "total": item.get("total", 0),
            "new_ep": item.get("new_ep", {}).get("index_show", ""),
            "cover": item.get("cover", ""),
            "is_finish": item.get("is_finish", 0),
        })
    return items


def analyze_activity_pattern(timestamps: list[int]) -> dict:
    """分析活跃时间模式"""
    if not timestamps:
        return {}

    hours = {}
    days = {}
    day_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    for ts in timestamps:
        try:
            ts_int = int(ts)
            dt = datetime.fromtimestamp(ts_int)
            h = dt.hour
            d = day_names[dt.weekday()]
            hours[h] = hours.get(h, 0) + 1
            days[d] = days.get(d, 0) + 1
        except (ValueError, OSError, TypeError):
            continue

    peak_hour = max(hours.items(), key=lambda x: x[1])[0] if hours else None
    peak_day = max(days.items(), key=lambda x: x[1])[0] if days else None

    # 判断活跃类型
    activity_type = "未知"
    if peak_hour is not None:
        if 6 <= peak_hour < 12:
            activity_type = "早起型"
        elif 12 <= peak_hour < 18:
            activity_type = "午间活跃"
        elif 18 <= peak_hour < 24:
            activity_type = "晚间活跃"
        else:
            activity_type = "夜猫子"

    return {
        "peak_hour": peak_hour,
        "peak_day": peak_day,
        "activity_type": activity_type,
        "hour_distribution": dict(sorted(hours.items())),
        "day_distribution": days,
    }


# ========== 统一采集接口 ==========

def collect_user_data(uid: int, client: BiliAPIClient) -> dict:
    """
    采集用户完整深度数据（四维度）
    
    Returns:
        包含所有维度的完整数据dict
    """
    print(f"  [Collect] UID:{uid} 开始采集...")

    # 维度1：主页信息
    card = get_user_card(uid, client)
    if "error" in card:
        return {"uid": uid, "error": card["error"]}

    space = get_user_space_info(uid, client)
    user_data = {**card, **space}

    # 追番
    try:
        user_data["bangumi"] = get_bangumi_list(uid, client, btype=1)
    except Exception:
        user_data["bangumi"] = []
    try:
        user_data["dramas"] = get_bangumi_list(uid, client, btype=2)
    except Exception:
        user_data["dramas"] = []

    # 收藏夹
    try:
        folders = get_favorite_folders(uid, client)
        user_data["favorite_folders"] = folders
    except Exception:
        folders = []
        user_data["favorite_folders"] = []
    if folders:
        user_data["favorite_contents"] = get_favorite_contents(folders[0]["id"], client)
    else:
        user_data["favorite_contents"] = []

    # 维度2：互动足迹
    try:
        user_data["videos"] = get_user_videos(uid, client)
    except Exception:
        user_data["videos"] = []
    try:
        user_data["dynamics"] = get_user_dynamics(uid, client)
    except Exception:
        user_data["dynamics"] = []

    # 维度3：社交网络
    try:
        user_data["followings"] = get_followings(uid, client)
    except Exception:
        user_data["followings"] = []
    try:
        user_data["followers"] = get_followers(uid, client)
    except Exception:
        user_data["followers"] = []

    # UP主关注偏好分析
    try:
        from up_analyzer import summarize_followings
        user_data["following_summary"] = summarize_followings(
            user_data["followings"], client, sample_size=MAX_UP_SAMPLE
        )
    except Exception:
        user_data["following_summary"] = {}

    # 维度4：行为模式（综合动态 + 视频投稿时间）
    dynamic_timestamps = [d["timestamp"] for d in user_data["dynamics"] if d.get("timestamp")]
    video_timestamps = [v["created"] for v in user_data.get("videos", []) if v.get("created")]
    all_timestamps = dynamic_timestamps + video_timestamps
    user_data["activity_pattern"] = analyze_activity_pattern(all_timestamps)

    # 注册时间推断（从UID位数和第一个动态时间推断）
    if dynamic_timestamps:
        user_data["first_seen"] = min(dynamic_timestamps)

    print(f"  [Collect] UID:{uid} {user_data.get('name','')} Lv.{user_data.get('level',0)} 采集完成")
    return user_data
