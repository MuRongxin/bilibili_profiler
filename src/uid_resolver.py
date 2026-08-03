"""
UID解析引擎：mid_hash破解 + 评论区交叉验证

B站弹幕中的用户标识历史：
- 2021年前：使用 CRC32(UID) 作为 midHash，可反向破解
- 2021年后：新用户UID过长（>10位），CRC32碰撞空间太大
  → 解决方案：从评论区获取明文UID，计算CRC32后与弹幕mid_hash比对
"""
import zlib
from typing import Optional, Tuple

from api_client import BiliAPIClient
from config import CRC32_MAX_SEARCH, USER_CARD_URL

# 解析方法标识（入库/缓存推断均引用这些常量，勿散落硬编码字符串）
METHOD_COMMENT_VERIFY = "评论区验证"
METHOD_CRC32_CRACK = "CRC32破解"
METHOD_CRC32_COLLISION = "CRC32碰撞"
METHOD_UNKNOWN = "未知"


def calc_crc32(uid: int) -> str:
    """计算UID的标准CRC32（B站使用的算法）"""
    return format(zlib.crc32(str(uid).encode()) & 0xFFFFFFFF, "08x")


def crack_crc32(crc32_hash: str, max_search: int = CRC32_MAX_SEARCH) -> Optional[int]:
    """
    CRC32反向搜索破解UID
    
    算法来自 bilibili_api.utils.utils.crack_uid（esterTion/BiliBili_crc2mid）
    搜索范围扩展到 max_search（默认5000万）
    
    Returns:
        破解出的UID，或None
    """
    __CRCPOLYNOMIAL = 0xEDB88320
    __crctable = [0] * 256
    __index = [0] * 4

    for i in range(256):
        crcreg = i
        for _ in range(8):
            crcreg = (__CRCPOLYNOMIAL ^ (crcreg >> 1)) if (crcreg & 1) else (crcreg >> 1)
        __crctable[i] = crcreg

    def _crc32(input_):
        crcstart = 0xFFFFFFFF
        for ch in str(input_):
            idx = (crcstart ^ ord(ch)) & 0xFF
            crcstart = (crcstart >> 8) ^ __crctable[idx]
        return crcstart

    def _crc32lastindex(input_):
        crcstart = 0xFFFFFFFF
        li = 0
        for ch in str(input_):
            li = (crcstart ^ ord(ch)) & 0xFF
            crcstart = (crcstart >> 8) ^ __crctable[li]
        return li

    def _getcrcindex(t):
        for i in range(256):
            if __crctable[i] >> 24 == t:
                return i
        return -1

    def _deep_check(i, index):
        hash_ = _crc32(i)
        r = ""
        for p in [2, 1, 0]:
            tc = hash_ & 0xFF ^ index[p]
            if not (57 >= tc >= 48):
                return False, ""
            r += chr(tc)
            hash_ = __crctable[index[p]] ^ (hash_ >> 8)
        return True, r

    ht = int(crc32_hash, 16) ^ 0xFFFFFFFF
    for j in range(3, -1, -1):
        __index[3 - j] = _getcrcindex(ht >> (j * 8))
        if __index[3 - j] == -1:
            return None
        ht ^= __crctable[__index[3 - j]] >> ((3 - j) * 8)

    for i in range(max_search):
        if _crc32lastindex(i) == __index[3]:
            ok, sfx = _deep_check(i, __index)
            if ok:
                return int(str(i) + sfx)

    return None


def verify_uid_exists(uid: int, client: BiliAPIClient) -> Tuple[bool, dict]:
    """
    通过API验证UID是否存在
    
    Returns:
        (是否存在, 用户基础信息dict)
    """
    try:
        data = client.get(USER_CARD_URL, params={"mid": uid})
        if data.get("code") != 0:
            return False, {}
        card = data.get("data", {}).get("card", {})
        if not card or not card.get("mid"):
            return False, {}
        return True, {
            "uid": uid,
            "name": card.get("name", ""),
            "face": card.get("face", ""),
            "sign": card.get("sign", ""),
            "level": card.get("level_info", {}).get("current_level", 0),
            "sex": card.get("sex", ""),
            "vip_type": card.get("vip", {}).get("type", 0),
            "vip_status": card.get("vip", {}).get("status", 0),
            "official_type": card.get("official_verify", {}).get("type", -1),
            "follower": card.get("fans", 0),
            "following": card.get("attention", 0),
        }
    except Exception as e:
        return False, {}


def cross_verify_with_comments(mid_hash: str, comment_uid_map: dict[str, int]) -> Optional[int]:
    """
    用评论区UID映射交叉验证mid_hash
    
    原理：评论区能拿到明文UID，计算其CRC32，看是否与弹幕mid_hash匹配
    
    Returns:
        匹配到的UID，或None
    """
    return comment_uid_map.get(mid_hash)


def resolve_sender(
    mid_hash: str,
    danmaku_contents: list[str],
    comment_uid_map: dict[str, int],
    client: BiliAPIClient,
    max_search: int = CRC32_MAX_SEARCH
) -> Tuple[Optional[int], str, str, Optional[dict], bool]:
    """
    解析发送者UID，综合多种方法
    
    优先级：
    1. 评论区交叉验证（最可靠，100%准确）
    2. CRC32反向破解（老用户，需API验证，存在碰撞误识别风险）
    3. 标记为未知
    
    Args:
        mid_hash: 弹幕中的用户标识
        danmaku_contents: 该发送者的所有弹幕内容
        comment_uid_map: 评论区CRC32->UID映射
        client: API客户端
        max_search: CRC32搜索上限
    
    Returns:
        (uid, confidence, method, user_info, collision_risk)
        uid: 解析出的UID，失败为None
        confidence: 置信度（高/中/低/无）；暴力破解路径上限为"中"
        method: 解析方法（评论区验证/CRC32破解/CRC32碰撞/未知）
        user_info: API验证返回的用户信息
        collision_risk: 是否有CRC32碰撞误识别风险（暴力破解路径为True）
    """
    uid = None
    method = METHOD_UNKNOWN
    confidence = "无"
    user_info = None

    # === 方法1：评论区交叉验证 ===
    uid = cross_verify_with_comments(mid_hash, comment_uid_map)
    if uid:
        exists, user_info = verify_uid_exists(uid, client)
        if exists:
            method = METHOD_COMMENT_VERIFY
            # 弹幕越多，对该用户的置信度越高
            if len(danmaku_contents) >= 5:
                confidence = "高"
            elif len(danmaku_contents) >= 2:
                confidence = "高"  # 评论区验证本身就很可靠
            else:
                confidence = "中"
            return uid, confidence, method, user_info, False
        else:
            uid = None  # 理论上不会发生，但做防御

    # === 方法2：CRC32反向破解 ===
    cracked_uid = crack_crc32(mid_hash, max_search=max_search)
    if cracked_uid:
        exists, user_info = verify_uid_exists(cracked_uid, client)
        if exists:
            uid = cracked_uid
            method = METHOD_CRC32_CRACK
            # 暴力破解存在碰撞风险（实测 calc_crc32(1) 会被破解成其他真实存在的UID，
            # verify_uid_exists 无法拦截），因此置信度上限压到"中"，不得为"高"
            confidence = "中" if len(danmaku_contents) >= 2 else "低"  # 单条弹幕碰撞风险更高
            return uid, confidence, method, user_info, True
        else:
            # CRC32破解出了数字，但API返回不存在 → 碰撞假阳性
            method = METHOD_CRC32_COLLISION
            confidence = "无"
            uid = None

    return uid, confidence, method, user_info, False


def resolve_all_senders(
    sender_groups: dict[str, dict],
    comment_uid_map: dict[str, int],
    client: BiliAPIClient,
    max_search: int = CRC32_MAX_SEARCH
) -> dict[str, dict]:
    """
    批量解析所有发送者UID
    
    Returns:
        {mid_hash: {
            "uid": int or None,
            "confidence": str,
            "method": str,
            "user_info": dict,
            "danmaku_count": int,
            "contents": [str],
            "collision_risk": bool,
        }}
    """
    results = {}
    total = len(sender_groups)
    resolved = 0

    print(f"[Resolver] 开始解析 {total} 个发送者...")

    for idx, (mid_hash, group) in enumerate(sender_groups.items(), 1):
        contents = group["contents"]
        uid, confidence, method, user_info, collision_risk = resolve_sender(
            mid_hash, contents, comment_uid_map, client, max_search
        )

        results[mid_hash] = {
            "uid": uid,
            "confidence": confidence,
            "method": method,
            "user_info": user_info or {},
            "danmaku_count": group["count"],
            "contents": contents,
            "collision_risk": collision_risk,
            "timestamps": group["timestamps"],
            "video_times": group["video_times"],
            "colors": group["colors"],
            "pages": group["pages"],
        }

        if uid:
            resolved += 1
            name = user_info.get("name", "?") if user_info else "?"
            risk_note = " ⚠️可能误识别" if collision_risk else ""
            print(f"  [{idx}/{total}] ✅ {mid_hash} → UID:{uid} ({name}) 方法:{method} 置信度:{confidence}{risk_note}")
        else:
            if idx % 10 == 0 or idx == total:
                print(f"  [{idx}/{total}] 进度... 已解析:{resolved}")

    print(f"[Resolver] 解析完成: {resolved}/{total} 个发送者成功识别")
    return results
