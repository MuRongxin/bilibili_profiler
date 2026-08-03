"""
数据持久化模块（SQLite）

支持：
- 视频/弹幕/用户数据存储
- 进度中断恢复
- 避免重复采集
"""
import sqlite3
import json
import os
from datetime import datetime

from config import DB_PATH


def get_db() -> sqlite3.Connection:
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化数据库表结构"""
    conn = get_db()
    cursor = conn.cursor()

    # 视频信息表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS videos (
            bvid TEXT PRIMARY KEY,
            title TEXT,
            aid INTEGER,
            cid INTEGER,
            duration INTEGER,
            view_count INTEGER,
            danmaku_count INTEGER,
            reply_count INTEGER,
            video_info_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 弹幕发送者表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS senders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bvid TEXT,
            mid_hash TEXT,
            uid INTEGER,
            confidence TEXT,
            method TEXT,
            danmaku_count INTEGER,
            contents_json TEXT,
            spam_level TEXT,
            spam_score REAL,
            UNIQUE(bvid, mid_hash)
        )
    ''')

    # 用户深度数据表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            uid INTEGER PRIMARY KEY,
            name TEXT,
            level INTEGER,
            data_json TEXT,
            profile_json TEXT,
            collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 进度表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS progress (
            bvid TEXT PRIMARY KEY,
            stage TEXT,
            processed_uids_json TEXT,
            total_uids INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()
    print("[Storage] 数据库初始化完成")


# ========== 视频数据 ==========

def save_video_info(bvid: str, video_info: dict):
    """保存视频信息"""
    conn = get_db()
    cursor = conn.cursor()
    stat = video_info.get("stat", {})
    cursor.execute('''
        INSERT OR REPLACE INTO videos (bvid, title, aid, cid, duration, view_count, danmaku_count, reply_count, video_info_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        bvid,
        video_info.get("title", ""),
        video_info.get("aid", 0),
        video_info.get("cid", 0),
        video_info.get("duration", 0),
        stat.get("view", 0),
        stat.get("danmaku", 0),
        stat.get("reply", 0),
        json.dumps(video_info, ensure_ascii=False)
    ))
    conn.commit()
    conn.close()


def load_video_info(bvid: str) -> dict | None:
    """加载视频信息"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT video_info_json FROM videos WHERE bvid = ?", (bvid,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return json.loads(row["video_info_json"])
    return None


# ========== 发送者数据 ==========

def save_sender(bvid: str, mid_hash: str, uid: int | None, confidence: str,
                method: str, danmaku_count: int, contents: list[str],
                spam_level: str, spam_score: float):
    """保存发送者解析结果"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO senders
        (bvid, mid_hash, uid, confidence, method, danmaku_count, contents_json, spam_level, spam_score)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (bvid, mid_hash, uid, confidence, method, danmaku_count,
          json.dumps(contents, ensure_ascii=False), spam_level, spam_score))
    conn.commit()
    conn.close()


def load_senders(bvid: str) -> list[dict]:
    """加载已解析的发送者"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT mid_hash, uid, confidence, method, danmaku_count, contents_json, spam_level, spam_score
        FROM senders WHERE bvid = ?
    ''', (bvid,))
    rows = cursor.fetchall()
    conn.close()
    return [{
        "mid_hash": r["mid_hash"],
        "uid": r["uid"],
        "confidence": r["confidence"],
        "method": r["method"],
        "danmaku_count": r["danmaku_count"],
        "contents": json.loads(r["contents_json"]),
        "spam_level": r["spam_level"],
        "spam_score": r["spam_score"],
    } for r in rows]


def get_resolved_uids(bvid: str) -> set[int]:
    """获取已解析出UID的发送者"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT uid FROM senders WHERE bvid = ? AND uid IS NOT NULL", (bvid,))
    rows = cursor.fetchall()
    conn.close()
    return {r["uid"] for r in rows}


# ========== 用户数据 ==========

def save_user_data(uid: int, name: str, level: int, user_data: dict, profile: dict):
    """保存用户深度数据和画像"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO users (uid, name, level, data_json, profile_json, collected_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (uid, name, level,
          json.dumps(user_data, ensure_ascii=False),
          json.dumps(profile, ensure_ascii=False),
          datetime.now().isoformat()))
    conn.commit()
    conn.close()


def load_user_data(uid: int) -> tuple[dict, dict] | None:
    """加载用户数据和画像"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT data_json, profile_json FROM users WHERE uid = ?", (uid,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return json.loads(row["data_json"]), json.loads(row["profile_json"])
    return None


def has_user_data(uid: int) -> bool:
    """检查是否已采集过该用户"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM users WHERE uid = ?", (uid,))
    row = cursor.fetchone()
    conn.close()
    return row is not None


# ========== 进度管理 ==========

def save_progress(bvid: str, stage: str, processed_uids: set[int], total_uids: int):
    """保存处理进度"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO progress (bvid, stage, processed_uids_json, total_uids, updated_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (bvid, stage, json.dumps(list(processed_uids)), total_uids, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def load_progress(bvid: str) -> tuple[str, set[int], int] | None:
    """加载处理进度"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT stage, processed_uids_json, total_uids FROM progress WHERE bvid = ?", (bvid,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row["stage"], set(json.loads(row["processed_uids_json"])), row["total_uids"]
    return None


def clear_progress(bvid: str):
    """清除进度（用于重新分析）"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM progress WHERE bvid = ?", (bvid,))
    conn.commit()
    conn.close()
