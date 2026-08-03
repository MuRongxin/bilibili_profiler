"""
历史弹幕采集模块（弹幕池历史快照）

实时弹幕池（XML 接口）只保留最近几千条，历史弹幕接口可按日拉取历史快照：
- history/index：查询某月哪些日期有弹幕（需登录）
- history/seg.so：拉取某日期的弹幕池快照 protobuf（需 SESSDATA）

【实测语义 2026-08-03 验证】seg.so 并非"该日发送的弹幕"，而是"截至该日期的
最新 1000 条弹幕池快照"（每日上限 1000，弹幕 ctime 可早于请求日期；相邻日
快照可能大量重叠或完全不重叠）。逐日遍历 + 按 dmid 去重可逼近全量历史弹幕，
但热门日期（如发布初期）快照间的弹幕滚动仍可能丢失。
另：历史接口的 midHash 省略前导零（需 zfill(8)）；weight/pool 字段不下发（恒 0）。

protobuf 结构（DmSegMobileReply）：
    repeated DanmakuElem elems = 1;
    DanmakuElem: id=1(varint), progress=2(varint毫秒), mode=3, fontsize=4,
                 color=5, midHash=6(string), content=7(string), ctime=8(varint),
                 weight=9, pool=11, idStr=12(string)
为免引入 protobuf 依赖，wire 格式手写解析。
"""
import time
from typing import Optional

from api_client import BiliAPIClient
from config import (
    DANMAKU_HISTORY_INDEX_URL,
    DANMAKU_HISTORY_SEG_URL,
    HISTORY_DANMAKU_ENABLED,
    HISTORY_MAX_MONTHS,
    HISTORY_MAX_DAYS,
)


# ========== protobuf wire 手写解析 ==========

def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """读取 varint，返回 (值, 新位置)"""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("varint 越界")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint 过长")


def _skip_field(data: bytes, pos: int, wire_type: int) -> int:
    """跳过未知字段，返回新位置"""
    if wire_type == 0:      # varint
        _, pos = _read_varint(data, pos)
        return pos
    if wire_type == 1:      # 64-bit
        return pos + 8
    if wire_type == 2:      # length-delimited
        length, pos = _read_varint(data, pos)
        return pos + length
    if wire_type == 5:      # 32-bit
        return pos + 4
    raise ValueError(f"不支持的 wire type: {wire_type}")


def _parse_elem(data: bytes) -> dict:
    """解析单个 DanmakuElem 嵌套消息，输出与 danmaku.parse_danmaku_xml 同构的 dict"""
    fields = {}
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        field_no, wire_type = tag >> 3, tag & 0x07
        if wire_type == 0:
            value, pos = _read_varint(data, pos)
            fields[field_no] = value
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            fields[field_no] = data[pos:pos + length]
            pos += length
        else:
            pos = _skip_field(data, pos, wire_type)

    def _s(field_no: int) -> str:
        raw = fields.get(field_no, b"")
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else ""

    id_str = _s(12)
    return {
        "content": _s(7),                                     # 弹幕内容
        "time": fields.get(2, 0) / 1000.0,                    # 视频内出现时间(秒)，progress 为毫秒
        "mode": fields.get(3, 1),                             # 弹幕模式
        "fontsize": fields.get(4, 25),                        # 字号
        "color": f"#{fields.get(5, 0xffffff):06x}",           # 颜色
        "timestamp": fields.get(8, 0),                        # 发送时间戳(ctime)
        "pool": fields.get(11, 0),                            # 弹幕池
        # midHash 是 CRC32 hex 字符串，但历史接口省略前导零（实测约 6% 为 6~7 位），
        # 左补零对齐实时池 XML 的 8 位格式，否则与 CRC32 反查结果对不上
        "mid_hash": _s(6).lower().zfill(8),                   # 发送者标识（8位CRC32十六进制hash）
        "dmid": int(id_str) if id_str.isdigit() else fields.get(1, 0),
        "weight": fields.get(9, 0),                           # 弹幕权重（历史接口特有）
    }


def parse_danmaku_proto(data: bytes) -> list[dict]:
    """
    解析 DmSegMobileReply protobuf 字节流（历史弹幕 seg.so 响应）

    顶层结构只有 field 1（elems，length-delimited 嵌套消息）重复出现。
    单条 elem 解析失败跳过不中断。
    """
    danmaku_list = []
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _read_varint(data, pos)
            field_no, wire_type = tag >> 3, tag & 0x07
            if field_no == 1 and wire_type == 2:
                length, pos = _read_varint(data, pos)
                elem_bytes = data[pos:pos + length]
                pos += length
                try:
                    danmaku_list.append(_parse_elem(elem_bytes))
                except Exception:
                    # 单条弹幕损坏不中断整日解析
                    continue
            else:
                pos = _skip_field(data, pos, wire_type)
        except (ValueError, IndexError):
            # 顶层流损坏：已解析的部分仍然可用，直接返回
            break
    return danmaku_list


# ========== 历史弹幕采集 ==========

def _month_range(start_ym: tuple[int, int], end_ym: tuple[int, int]) -> list[str]:
    """生成 [start, end] 闭区间的 'YYYY-MM' 月份列表"""
    months = []
    y, m = start_ym
    while (y, m) <= end_ym:
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return months


def _fetch_month_dates(cid: int, month: str, client: BiliAPIClient) -> list[str]:
    """查询某月有弹幕的日期列表；失败返回空列表（降级不中断）"""
    data = client.get(DANMAKU_HISTORY_INDEX_URL, params={"type": 1, "oid": cid, "month": month})
    if data.get("code") != 0:
        print(f"[历史弹幕] 警告：{month} 月份索引获取失败（{data.get('message')}），跳过该月")
        return []
    return data.get("data") or []


def _fetch_day_danmaku(cid: int, date: str, client: BiliAPIClient) -> list[dict]:
    """拉取并解析某日期的弹幕池快照（截至该日的最新1000条，非"该日发送的弹幕"）"""
    resp = client.get_raw(DANMAKU_HISTORY_SEG_URL, params={"type": 1, "oid": cid, "date": date})
    return parse_danmaku_proto(resp.content)


def fetch_history_danmaku(cid: int, client: BiliAPIClient, pubdate: Optional[int] = None) -> list[dict]:
    """
    采集视频历史弹幕（弹幕池快照逐日遍历）

    从 pubdate 月份起到当前月逐月查询日期索引，再逐日拉取 seg.so。
    返回的是原始快照合并结果（相邻日快照可能重叠），调用方需按 dmid 去重。
    受 HISTORY_MAX_MONTHS / HISTORY_MAX_DAYS 限制；单月/单日失败打印警告跳过，不中断。

    Returns:
        与 danmaku.parse_danmaku_xml 同构的弹幕 dict 列表（另含 weight 字段，实测恒为 0）
    """
    if not HISTORY_DANMAKU_ENABLED:
        return []

    now = time.localtime()
    end_ym = (now.tm_year, now.tm_mon)
    if pubdate:
        start = time.localtime(pubdate)
        start_ym = (start.tm_year, start.tm_mon)
    else:
        # 无发布时间时只从当月回溯 HISTORY_MAX_MONTHS 个月
        y, m = end_ym
        m -= HISTORY_MAX_MONTHS - 1
        while m <= 0:
            y, m = y - 1, m + 12
        start_ym = (y, m)

    months = _month_range(start_ym, end_ym)
    if len(months) > HISTORY_MAX_MONTHS:
        # 超上限时保留最近的月份（近期弹幕对画像价值更高）
        months = months[-HISTORY_MAX_MONTHS:]
        print(f"[历史弹幕] 时间跨度超限，仅回溯最近 {HISTORY_MAX_MONTHS} 个月（{months[0]} 起）")

    all_danmaku = []
    fetched_days = 0
    for month in months:
        if fetched_days >= HISTORY_MAX_DAYS:
            print(f"[历史弹幕] 已达天数上限 {HISTORY_MAX_DAYS}，停止回溯更早月份")
            break
        dates = _fetch_month_dates(cid, month, client)
        if not dates:
            continue
        month_count = 0
        for date in dates:
            if fetched_days >= HISTORY_MAX_DAYS:
                break
            try:
                dms = _fetch_day_danmaku(cid, date, client)
            except Exception as e:
                # 降级而非中断：单日失败仅跳过该日
                print(f"[历史弹幕] 警告：{date} 弹幕采集失败，已跳过: {e}")
                continue
            fetched_days += 1
            month_count += len(dms)
            all_danmaku.extend(dms)
        print(f"[历史弹幕] {month}: {len(dates)} 天有弹幕，累计 {len(all_danmaku)} 条")

    print(f"[历史弹幕] 共采集 {fetched_days} 天，{len(all_danmaku)} 条历史弹幕")
    return all_danmaku
