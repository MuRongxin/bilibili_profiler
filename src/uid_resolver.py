"""
UID解析引擎：mid_hash MITM 反查 + 明文交叉验证 + 碰撞消歧

B站弹幕中的用户标识历史：
- 2021年前：使用 CRC32(UID) 作为 midHash，可反向破解
- 2021年后：新用户UID过长（16位随机），数学上不可解
  → 解决方案：MITM 反查覆盖全部 ≤10 位 UID（秒级返回全部候选），
    再经评论等明文来源交叉验证 / 候选交集消歧 / 存在性验证压制置信度
"""
import zlib
from typing import Optional, Tuple

from api_client import BiliAPIClient
from config import MITM_MAX_UID, USER_CARD_URL
from crc_rainbow import lookup

# 解析方法标识（入库/缓存推断均引用这些常量，勿散落硬编码字符串）
METHOD_COMMENT_VERIFY = "评论区验证"
METHOD_CRC32_CRACK = "CRC32破解"
METHOD_CRC32_COLLISION = "CRC32碰撞"
METHOD_UNKNOWN = "未知"


def calc_crc32(uid: int) -> str:
    """计算UID的标准CRC32（B站使用的算法）"""
    return format(zlib.crc32(str(uid).encode()) & 0xFFFFFFFF, "08x")


def crack_crc32(crc32_hash: str, max_search: int = MITM_MAX_UID) -> list:
    """
    MITM 反查 mid_hash，返回全部候选 UID（升序，每个都经 zlib 校验）

    ≤10 位空间每个 hash 平均约 2.3 个候选，消歧（评论交集/存在性验证/
    置信度压制）由调用方 resolve_sender 负责。16 位随机长 UID 数学上不可解。

    Args:
        crc32_hash: 弹幕 mid_hash（8位hex）
        max_search: UID 覆盖上限（语义由 MITM_MAX_UID 承接，保留参数名兼容旧调用）

    Returns:
        候选 UID 列表（可能为空=未命中）
    """
    return lookup(crc32_hash, max_uid=max_search)


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
    max_search: int = MITM_MAX_UID,
    method_map: dict | None = None,
) -> Tuple[Optional[int], str, str, Optional[dict], bool, list]:
    """
    解析发送者UID，综合多种方法

    优先级：
    1. 明文交叉验证（评论/充电名单/互动弹幕/全局库合并映射，100%准确）
    2. MITM 反查 + 消歧（候选∩明文UID集合唯一→按明文验证；否则存在性验证）
    3. 标记为未知

    Args:
        mid_hash: 弹幕中的用户标识
        danmaku_contents: 该发送者的所有弹幕内容
        comment_uid_map: 明文源合并映射 crc32->UID（评论优先，见 main.phase_resolve）
        client: API客户端
        max_search: UID 覆盖上限（MITM_MAX_UID）
        method_map: 伴随映射 mid_hash->来源方法名（充电名单/互动弹幕等），
                    缺省时命中一律记"评论区验证"（向后兼容）

    Returns:
        (uid, confidence, method, user_info, collision_risk, candidates)
        candidates: MITM 全部候选（未走 MITM 路径时为空列表），供报告/全局库判断
    """
    uid = None
    method = METHOD_UNKNOWN
    confidence = "无"
    user_info = None
    candidates: list = []

    # === 方法1：明文交叉验证（合并映射，来源由 method_map 标注） ===
    uid = cross_verify_with_comments(mid_hash, comment_uid_map)
    if uid:
        exists, user_info = verify_uid_exists(uid, client)
        if exists:
            method = (method_map or {}).get(mid_hash, METHOD_COMMENT_VERIFY)
            confidence = "高" if len(danmaku_contents) >= 2 else "中"
            return uid, confidence, method, user_info, False, candidates
        uid = None  # 理论上不会发生，但做防御

    # === 方法2：MITM 反查 + 碰撞消歧 ===
    candidates = crack_crc32(mid_hash, max_search=max_search)
    if not candidates:
        return None, confidence, method, user_info, False, candidates

    # 2a. 候选 ∩ 明文 UID 集合唯一 → 等同明文验证（最可靠的消歧）
    plain_uids = set(comment_uid_map.values())
    inter = [u for u in candidates if u in plain_uids]
    if inter:
        for u in inter:
            exists, user_info = verify_uid_exists(u, client)
            if exists:
                method = (method_map or {}).get(mid_hash, METHOD_COMMENT_VERIFY)
                confidence = "高" if len(danmaku_contents) >= 2 else "中"
                return u, confidence, method, user_info, False, candidates
        # 交集候选全部注销 → 继续走 2b 对剩余候选验证
        candidates = [u for u in candidates if u not in inter]

    # 2b. 逐个存在性验证
    if len(candidates) > 1:
        print(f"[Resolver] 注意: hash {mid_hash} 有 {len(candidates)} 个碰撞候选，逐个验证存在性")
    existing = []
    for u in candidates:
        exists, info = verify_uid_exists(u, client)
        if exists:
            existing.append((u, info))

    if len(existing) == 1:
        # 恰好 1 个存在 → 方法 CRC32破解，置信度上限"中"（沿用压置信度规则）
        uid, user_info = existing[0]
        method = METHOD_CRC32_CRACK
        confidence = "中" if len(danmaku_contents) >= 2 else "低"  # 单条弹幕碰撞风险更高
        return uid, confidence, method, user_info, True, candidates
    if len(existing) > 1:
        # 多个存在 → 取最小 UID（注册更早、更可能活跃），置信度"低"
        existing.sort(key=lambda x: x[0])
        uid, user_info = existing[0]
        method = METHOD_CRC32_CRACK
        confidence = "低"
        print(f"[Resolver] 警告: hash {mid_hash} 有 {len(existing)} 个候选同时存在，"
              f"取最小 UID:{uid}（误识别风险高）")
        return uid, confidence, method, user_info, True, candidates

    # 候选全部不存在 → 碰撞假阳性
    return None, "无", METHOD_CRC32_COLLISION, None, False, candidates


def resolve_all_senders(
    sender_groups: dict[str, dict],
    comment_uid_map: dict[str, int],
    client: BiliAPIClient,
    max_search: int = MITM_MAX_UID,
    method_map: dict | None = None,
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
            "candidates": [int],  # MITM 全部候选（未走 MITM 路径时为空列表）
        }}
    """
    results = {}
    total = len(sender_groups)
    resolved = 0

    print(f"[Resolver] 开始解析 {total} 个发送者...")

    for idx, (mid_hash, group) in enumerate(sender_groups.items(), 1):
        contents = group["contents"]
        uid, confidence, method, user_info, collision_risk, candidates = resolve_sender(
            mid_hash, contents, comment_uid_map, client, max_search, method_map
        )

        results[mid_hash] = {
            "uid": uid,
            "confidence": confidence,
            "method": method,
            "user_info": user_info or {},
            "danmaku_count": group["count"],
            "contents": contents,
            "collision_risk": collision_risk,
            "candidates": candidates,
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
