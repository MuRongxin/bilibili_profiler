"""
数据持久化模块（SQLite）

支持：
- 视频/弹幕/用户数据存储
- 基于 senders/users 缓存的断点续采
- 避免重复采集
"""
import sqlite3
import json
from contextlib import closing
from datetime import datetime

from config import DB_PATH


def get_db() -> sqlite3.Connection:
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化数据库表结构"""
    with closing(get_db()) as conn:
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

        # 全局 mid_hash→UID 映射表（跨视频复用，只增不删）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS global_uid_map (
                mid_hash TEXT PRIMARY KEY,
                uid INTEGER NOT NULL,
                source TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 1
            )
        ''')

        conn.commit()

        # 清理旧版本的 progress 表（断点续采已改为纯 senders/users 缓存机制）
        cursor.execute("DROP TABLE IF EXISTS progress")
        conn.commit()
    print("[Storage] 数据库初始化完成")


# ========== 视频数据 ==========

def save_video_info(bvid: str, video_info: dict):
    """保存视频信息"""
    with closing(get_db()) as conn:
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


def load_video_info(bvid: str) -> dict | None:
    """加载视频信息"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT video_info_json FROM videos WHERE bvid = ?", (bvid,))
        row = cursor.fetchone()
    if row:
        return json.loads(row["video_info_json"])
    return None


# ========== 发送者数据 ==========

def save_sender(bvid: str, mid_hash: str, uid: int | None, confidence: str,
                method: str, danmaku_count: int, contents: list[str],
                spam_level: str, spam_score: float):
    """保存发送者解析结果"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO senders
            (bvid, mid_hash, uid, confidence, method, danmaku_count, contents_json, spam_level, spam_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (bvid, mid_hash, uid, confidence, method, danmaku_count,
              json.dumps(contents, ensure_ascii=False), spam_level, spam_score))
        conn.commit()


def update_sender_spam(bvid: str, mid_hash: str, spam_level: str, spam_score: float):
    """回写发送者的刷屏检测结果（刷屏检测在 save_sender 之后运行，需单独 UPDATE）"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE senders SET spam_level = ?, spam_score = ?
            WHERE bvid = ? AND mid_hash = ?
        ''', (spam_level, spam_score, bvid, mid_hash))
        conn.commit()


def load_senders(bvid: str) -> list[dict]:
    """加载已解析的发送者"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT mid_hash, uid, confidence, method, danmaku_count, contents_json, spam_level, spam_score
            FROM senders WHERE bvid = ?
        ''', (bvid,))
        rows = cursor.fetchall()
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


# ========== 用户数据 ==========

def save_user_data(uid: int, name: str, level: int, user_data: dict, profile: dict):
    """保存用户深度数据和画像"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO users (uid, name, level, data_json, profile_json, collected_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (uid, name, level,
              json.dumps(user_data, ensure_ascii=False),
              json.dumps(profile, ensure_ascii=False),
              datetime.now().isoformat()))
        conn.commit()


def load_user_data(uid: int) -> tuple[dict, dict] | None:
    """加载用户数据和画像"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT data_json, profile_json FROM users WHERE uid = ?", (uid,))
        row = cursor.fetchone()
    if row:
        return json.loads(row["data_json"]), json.loads(row["profile_json"])
    return None


def has_user_data(uid: int) -> bool:
    """检查是否已采集过该用户"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM users WHERE uid = ?", (uid,))
        row = cursor.fetchone()
    return row is not None


# ========== 缓存清理 ==========

def clear_video_cache(bvid: str):
    """
    清除指定视频的缓存（供 --force 强制重采使用）

    - 删除该 bvid 的全部 senders 记录
    - users 表无 bvid 列，按 uid 关联：仅删除"该 bvid 的 senders 引用、
      且不再被其他 bvid 的 senders 引用"的用户数据，避免误删共享缓存
    - 删除 videos 表中该 bvid 的视频信息记录
    """
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT uid FROM senders WHERE bvid = ? AND uid IS NOT NULL", (bvid,))
        uids = [r["uid"] for r in cursor.fetchall()]

        cursor.execute("DELETE FROM senders WHERE bvid = ?", (bvid,))
        cursor.execute("DELETE FROM videos WHERE bvid = ?", (bvid,))

        for uid in uids:
            cursor.execute("SELECT 1 FROM senders WHERE uid = ? LIMIT 1", (uid,))
            if cursor.fetchone() is None:
                cursor.execute("DELETE FROM users WHERE uid = ?", (uid,))

        conn.commit()


# ========== 全局 mid_hash→UID 映射库（跨视频复用） ==========

def save_global_uid(mid_hash: str, uid: int, source: str):
    """
    upsert 全局映射：新条目 hit_count=1；重复命中 hit_count+1 并刷新 last_seen
    source: 评论区验证 / CRC32破解 / 充电名单 / 互动弹幕
    """
    now = datetime.now().isoformat()
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO global_uid_map (mid_hash, uid, source, first_seen, last_seen, hit_count)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(mid_hash) DO UPDATE SET
                uid=excluded.uid, source=excluded.source,
                last_seen=excluded.last_seen, hit_count=hit_count+1
        ''', (mid_hash, uid, source, now, now))
        conn.commit()


def load_global_uid_map() -> dict:
    """读取全局映射库：{mid_hash: {"uid": int, "source": str, "hit_count": int}}"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT mid_hash, uid, source, hit_count FROM global_uid_map")
        rows = cursor.fetchall()
    return {r["mid_hash"]: {"uid": r["uid"], "source": r["source"], "hit_count": r["hit_count"]}
            for r in rows}
