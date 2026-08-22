"""
数据持久化模块（SQLite）

支持：
- 视频/弹幕/用户数据存储
- 基于 senders/users 缓存的断点续采
- 避免重复采集
"""
import sqlite3
import json
import time
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

        # LLM 结果缓存表（问题弹幕判定 + 重点深掘，跨运行复用省 token）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS llm_cache (
                cache_key   TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
        ''')

        # 全量弹幕表（Web 弹幕浏览器数据源；mode/color/pool/dmid 入库支撑弹幕属性统计）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS danmaku (
                bvid TEXT NOT NULL,
                mid_hash TEXT NOT NULL,
                content TEXT NOT NULL,
                time REAL NOT NULL,        -- 视频内出现时间(秒)
                timestamp INTEGER NOT NULL, -- 发送时间戳
                mode INTEGER NOT NULL DEFAULT 1,   -- 弹幕模式（1-3滚动/4底部/5顶部）
                color TEXT NOT NULL DEFAULT '',    -- 弹幕颜色（#rrggbb）
                pool INTEGER NOT NULL DEFAULT 0,   -- 弹幕池
                dmid INTEGER NOT NULL DEFAULT 0    -- 弹幕ID（历史合并去重键）
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_danmaku_bvid ON danmaku(bvid)")

        # 旧库迁移：danmaku 表补 mode/color/pool/dmid 列（弹幕属性统计：滚动占比/颜色分布/顶底弹幕）
        dm_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(danmaku)").fetchall()}
        if "mode" not in dm_cols:
            cursor.execute("ALTER TABLE danmaku ADD COLUMN mode INTEGER NOT NULL DEFAULT 1")
        if "color" not in dm_cols:
            cursor.execute("ALTER TABLE danmaku ADD COLUMN color TEXT NOT NULL DEFAULT ''")
        if "pool" not in dm_cols:
            cursor.execute("ALTER TABLE danmaku ADD COLUMN pool INTEGER NOT NULL DEFAULT 0")
        if "dmid" not in dm_cols:
            cursor.execute("ALTER TABLE danmaku ADD COLUMN dmid INTEGER NOT NULL DEFAULT 0")

        # 评论表（跨视频足迹 + 高回复评论页数据源；reply_count 只对主评论有意义，
        # root_rpid 记录子评论所属主评论的 rpid，供「高回复评论」页关联争议主楼与回复）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bvid TEXT NOT NULL,
                uid INTEGER NOT NULL,
                uname TEXT NOT NULL DEFAULT '',  -- 评论者昵称（高回复评论树直接显示用户名用）
                rpid INTEGER NOT NULL DEFAULT 0,
                content TEXT NOT NULL,
                ctime INTEGER NOT NULL DEFAULT 0,   -- 评论发送时间戳
                like INTEGER NOT NULL DEFAULT 0,
                is_sub INTEGER NOT NULL DEFAULT 0,  -- 1=子评论 0=主评论
                reply_count INTEGER NOT NULL DEFAULT 0,  -- 主评论的回复总数（rcount）
                root_rpid INTEGER NOT NULL DEFAULT 0,    -- 子评论所属主评论 rpid
                parent_rpid INTEGER NOT NULL DEFAULT 0,  -- 子评论的直接父级 rpid（回复树缩进用；0=直接回复主楼）
                problem TEXT NOT NULL DEFAULT '',        -- LLM 问题评论类别（空=正常/未判定）
                UNIQUE(bvid, rpid, uid)
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_comments_bvid ON comments(bvid)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_comments_uid ON comments(uid)")

        # 旧库迁移：comments 表补 reply_count / root_rpid 列（高回复评论功能）
        comment_cols = {r["name"] for r in cursor.execute("PRAGMA table_info(comments)").fetchall()}
        if "reply_count" not in comment_cols:
            cursor.execute("ALTER TABLE comments ADD COLUMN reply_count INTEGER NOT NULL DEFAULT 0")
        if "root_rpid" not in comment_cols:
            cursor.execute("ALTER TABLE comments ADD COLUMN root_rpid INTEGER NOT NULL DEFAULT 0")
        # 旧库迁移：comments 表补 parent_rpid（回复树缩进）与 problem（LLM 问题评论标注）列
        if "parent_rpid" not in comment_cols:
            cursor.execute("ALTER TABLE comments ADD COLUMN parent_rpid INTEGER NOT NULL DEFAULT 0")
        if "problem" not in comment_cols:
            cursor.execute("ALTER TABLE comments ADD COLUMN problem TEXT NOT NULL DEFAULT ''")
        # 旧库迁移：comments 表补 uname（高回复评论树直接显示用户名）
        if "uname" not in comment_cols:
            cursor.execute("ALTER TABLE comments ADD COLUMN uname TEXT NOT NULL DEFAULT ''")

        # 误报标记表（P2-a）：人工标注 LLM 误判的问题弹幕/评论。
        # kind: dm=问题弹幕（target=弹幕内容，判定按内容去重故同内容同源同罪）
        #       cmt=问题评论（target=rpid 字符串）
        # 展示层加载后从聚合中扣除，可再点撤销；llm_cache 不动，标记跨重跑保留
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS false_positive (
                bvid TEXT NOT NULL,
                kind TEXT NOT NULL,
                target TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (bvid, kind, target)
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
    """回写发送者的刷屏检测结果（仅 UPDATE 已存在的行；未落库的行不受影响）"""
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


# ========== 全量弹幕（Web 弹幕浏览器数据源） ==========

def save_danmaku(bvid: str, danmaku_list: list[dict]):
    """阶段2弹幕合并后批量落库：先删该 bvid 旧行再插入，幂等可重跑。

    存 bvid/mid_hash/content/time/timestamp/mode/color/pool/dmid 九列；
    Web API 直接 SQL 查询，无 load_danmaku。
    """
    rows = [(bvid, dm["mid_hash"], dm["content"], dm["time"], dm["timestamp"],
             dm.get("mode", 1), dm.get("color", ""), dm.get("pool", 0), dm.get("dmid", 0))
            for dm in danmaku_list]
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM danmaku WHERE bvid = ?", (bvid,))
        cursor.executemany(
            "INSERT INTO danmaku (bvid, mid_hash, content, time, timestamp, mode, color, pool, dmid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows)
        conn.commit()


def save_comments(bvid: str, comments: list[dict]):
    """阶段3评论采集后批量落库（跨视频足迹数据源）。

    UNIQUE(bvid, rpid, uid) + INSERT OR IGNORE 幂等去重：同一次分析重复落库、
    --force 重采（清缓存后重采同一批评论）都不会产生重复行。
    只存基础列；sign/avatar/location 等仅用于当次运行的内存流程，不入库。
    problem 列由 LLM 问题评论检测后经 update_comment_problems 回写。
    uname 在冲突时回填（旧行无昵称时补上），其余列冲突不覆盖（首采快照为准）。
    """
    rows = [(bvid, c["uid"], c.get("uname", ""), c.get("rpid", 0), c.get("content", ""),
             c.get("ctime", 0), c.get("like", 0), 1 if c.get("is_sub") else 0,
             c.get("reply_count", 0), c.get("root_rpid", 0), c.get("parent_rpid", 0))
            for c in comments if c.get("uid") and c.get("content")]
    if not rows:
        return
    with closing(get_db()) as conn:
        conn.executemany(
            "INSERT INTO comments "
            "(bvid, uid, uname, rpid, content, ctime, like, is_sub, reply_count, root_rpid, parent_rpid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(bvid, rpid, uid) DO UPDATE SET "
            "uname = CASE WHEN excluded.uname != '' THEN excluded.uname ELSE comments.uname END",
            rows)
        conn.commit()


def update_comment_problems(bvid: str, verdicts: dict):
    """回写 LLM 问题评论判定：verdicts = {rpid: 类别}；先清该 bvid 旧标注再逐条 UPDATE。

    幂等可重跑；rpid 在库中不存在（评论被删/未落库）时 UPDATE 命中 0 行，静默跳过。
    """
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE comments SET problem = '' WHERE bvid = ?", (bvid,))
        for rpid, category in verdicts.items():
            cursor.execute("UPDATE comments SET problem = ? WHERE bvid = ? AND rpid = ?",
                           (category, bvid, rpid))
        conn.commit()


# ========== 误报标记（P2-a：人工纠偏 LLM 判定，展示层扣除聚合，可撤销） ==========

def toggle_false_positive(bvid: str, kind: str, target: str) -> bool:
    """切换误报标记，返回切换后的状态（True=已标记）。kind: dm=弹幕内容 / cmt=评论rpid"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT 1 FROM false_positive WHERE bvid = ? AND kind = ? AND target = ?",
            (bvid, kind, target)).fetchone()
        if row:
            cursor.execute(
                "DELETE FROM false_positive WHERE bvid = ? AND kind = ? AND target = ?",
                (bvid, kind, target))
            marked = False
        else:
            cursor.execute(
                "INSERT INTO false_positive (bvid, kind, target, created_at) VALUES (?, ?, ?, ?)",
                (bvid, kind, target, int(time.time())))
            marked = True
        conn.commit()
    return marked


def load_false_positives(bvid: str) -> set[tuple[str, str]]:
    """加载该视频的全部误报标记：{(kind, target)}"""
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT kind, target FROM false_positive WHERE bvid = ?", (bvid,)).fetchall()
    return {(r["kind"], r["target"]) for r in rows}


# ========== 缓存清理 ==========

def clear_video_cache(bvid: str):
    """
    清除指定视频的缓存（供 --force 强制重采使用）

    - 删除该 bvid 的全部 senders 记录
    - users 表无 bvid 列，按 uid 关联：仅删除"该 bvid 的 senders 引用、
      且不再被其他 bvid 的 senders 引用"的用户数据，避免误删共享缓存
    - 删除该 bvid 的全部 danmaku 弹幕行
    - 删除该 bvid 的全部 comments 评论行（跨视频足迹数据源）
    - 删除 videos 表中该 bvid 的视频信息记录
    - 删除 llm_cache 中该 bvid 的问题弹幕判定缓存（cringe:{bvid}:*），深掘缓存（deep:*）保留
    """
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT uid FROM senders WHERE bvid = ? AND uid IS NOT NULL", (bvid,))
        uids = [r["uid"] for r in cursor.fetchall()]

        cursor.execute("DELETE FROM senders WHERE bvid = ?", (bvid,))
        cursor.execute("DELETE FROM videos WHERE bvid = ?", (bvid,))
        cursor.execute("DELETE FROM danmaku WHERE bvid = ?", (bvid,))
        cursor.execute("DELETE FROM comments WHERE bvid = ?", (bvid,))
        # 该视频的问题弹幕/问题评论判定缓存一并清除（key 前缀 cringe:/cmt: + {bvid}:）；
        # 深掘缓存 key 为 deep:{uid}:...，按用户跨视频复用，不清
        cursor.execute("DELETE FROM llm_cache WHERE cache_key LIKE ?", (f"cringe:{bvid}:%",))
        cursor.execute("DELETE FROM llm_cache WHERE cache_key LIKE ?", (f"cmt:{bvid}:%",))

        for uid in uids:
            cursor.execute("SELECT 1 FROM senders WHERE uid = ? LIMIT 1", (uid,))
            if cursor.fetchone() is None:
                cursor.execute("DELETE FROM users WHERE uid = ?", (uid,))

        conn.commit()


def delete_video_data(bvid: str) -> dict:
    """彻底删除指定视频的全部分析数据（Web 报告页「删除」按钮，spec 9）。

    与 clear_video_cache 的区别——共享缓存一并清除（用户明确选择）：
    - users 画像行无条件删除（即使仍被其他视频的 senders 引用）
    - llm_cache 深掘缓存 deep:{uid}:* 删除
    - global_uid_map 中该视频涉及的 mid_hash 条目删除
    返回各项删除行数 dict。"""
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT uid, mid_hash FROM senders WHERE bvid = ?", (bvid,))
        rows = cursor.fetchall()
        uids = {r["uid"] for r in rows if r["uid"] is not None}
        mid_hashes = {r["mid_hash"] for r in rows}

        counts = {}
        counts["senders"] = cursor.execute("DELETE FROM senders WHERE bvid = ?", (bvid,)).rowcount
        counts["danmaku"] = cursor.execute("DELETE FROM danmaku WHERE bvid = ?", (bvid,)).rowcount
        counts["comments"] = cursor.execute("DELETE FROM comments WHERE bvid = ?", (bvid,)).rowcount
        counts["false_positive"] = cursor.execute(
            "DELETE FROM false_positive WHERE bvid = ?", (bvid,)).rowcount
        counts["videos"] = cursor.execute("DELETE FROM videos WHERE bvid = ?", (bvid,)).rowcount
        counts["cringe_cache"] = cursor.execute(
            "DELETE FROM llm_cache WHERE cache_key LIKE ?", (f"cringe:{bvid}:%",)).rowcount
        counts["cmt_cache"] = cursor.execute(
            "DELETE FROM llm_cache WHERE cache_key LIKE ?", (f"cmt:{bvid}:%",)).rowcount
        counts["deep_cache"] = 0
        for uid in uids:
            counts["deep_cache"] += cursor.execute(
                "DELETE FROM llm_cache WHERE cache_key LIKE ?", (f"deep:{uid}:%",)).rowcount
        counts["global_uid_map"] = 0
        for mh in mid_hashes:
            counts["global_uid_map"] += cursor.execute(
                "DELETE FROM global_uid_map WHERE mid_hash = ?", (mh,)).rowcount
        counts["users"] = 0
        for uid in uids:
            counts["users"] += cursor.execute("DELETE FROM users WHERE uid = ?", (uid,)).rowcount
        conn.commit()
    return counts


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


# ========== LLM 结果缓存（省 token：重跑同视频/同证据包零调用） ==========

def load_llm_cache(cache_key: str) -> str | None:
    """读取 LLM 缓存；不存在或读取异常均返回 None（视为未命中，不中断流水线）"""
    try:
        with closing(get_db()) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT result_json FROM llm_cache WHERE cache_key = ?", (cache_key,))
            row = cursor.fetchone()
        return row["result_json"] if row else None
    except Exception:
        return None


def save_llm_cache(cache_key: str, result_json: str):
    """写入 LLM 缓存；异常只打印警告（缓存失败不影响主流程）"""
    try:
        with closing(get_db()) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO llm_cache (cache_key, result_json, created_at)
                VALUES (?, ?, ?)
            ''', (cache_key, result_json, datetime.now().isoformat()))
            conn.commit()
    except Exception as e:
        print(f"[Storage] 警告: LLM 缓存写入失败（{e}），忽略")
