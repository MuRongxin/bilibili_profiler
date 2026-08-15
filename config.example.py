"""
配置模板 —— 使用方式：
    cp config.example.py src/config.py
然后按需修改 src/config.py。src/config.py 已被 .gitignore 排除，不会被提交。
"""
import os

# ========== 路径配置 ==========
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "profiler.db")
COOKIE_PATH = os.path.join(DATA_DIR, "cookie.json")
REPORT_DIR = os.path.join(DATA_DIR, "reports")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)

# ========== API 端点 ==========
VIDEO_INFO_URL = "https://api.bilibili.com/x/web-interface/view"
DANMAKU_XML_URL = "https://api.bilibili.com/x/v1/dm/list.so"
DANMAKU_HISTORY_INDEX_URL = "https://api.bilibili.com/x/v2/dm/history/index"
DANMAKU_HISTORY_SEG_URL = "https://api.bilibili.com/x/v2/dm/web/history/seg.so"
DANMAKU_VIEW_URL = "https://api.bilibili.com/x/v2/dm/web/view"  # 弹幕元数据（含互动弹幕明文mid，需SESSDATA）
COMMENT_MAIN_WBI_URL = "https://api.bilibili.com/x/v2/reply/wbi/main"  # 主评论新接口（wbi签名+游标翻页）
COMMENT_MAIN_URL = "https://api.bilibili.com/x/v2/reply/main"  # 旧版主评论接口（wbi/main 失败时降级用）
COMMENT_REPLY_URL = "https://api.bilibili.com/x/v2/reply/reply"
CHARGE_LIST_URL = "https://api.bilibili.com/x/web-interface/elec/show"  # 视频充电鸣谢名单（含明文pay_mid）

USER_CARD_URL = "https://api.bilibili.com/x/web-interface/card"
USER_CARDS_BATCH_URL = "https://api.bilibili.com/x/polymer/pc-electron/v1/user/cards"  # 批量名片（≤50人/请求，仅需登录）
USER_SPACE_URL = "https://api.bilibili.com/x/space/wbi/acc/info"  # wbi 版空间信息（旧 acc/info 已废弃）
# 投稿列表主接口：文档注明"暂未发现风控校验"，无需 wbi 签名；keywords 为空取全部
USER_VIDEOS_URL = "https://api.bilibili.com/x/series/recArchivesByKeywords"
# 投稿列表降级接口：wbi 签名，2023-11 起需 dm_img_* 指纹参数（实测当前未强制），但返回 typeid 分区信息
USER_VIDEOS_LEGACY_URL = "https://api.bilibili.com/x/space/wbi/arc/search"
USER_DYNAMICS_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
USER_FOLLOWINGS_URL = "https://api.bilibili.com/x/relation/followings"
USER_FOLLOWERS_URL = "https://api.bilibili.com/x/relation/followers"
USER_FAV_FOLDERS_URL = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
USER_FAV_CONTENTS_URL = "https://api.bilibili.com/x/v3/fav/resource/list"
USER_BANGUMI_URL = "https://api.bilibili.com/x/space/bangumi/follow/list"
USER_LIVE_URL = "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld"
USER_LIKE_URL = "https://api.bilibili.com/x/space/like/video"

QRCODE_GEN_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QRCODE_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

# ========== 请求配置 ==========
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}

BILI_TICKET_ENABLED = True  # 申请bili_ticket降低风控概率

REQUEST_DELAY = 0.6          # 基础请求间隔（秒）
REQUEST_DELAY_LONG = 1.0     # 高风险API间隔（秒）
MAX_RETRY = 3                # 最大重试次数
RETRY_BACKOFF = 2.0          # 重试退避基数（秒）
RISK_COOLDOWN = 600          # 触发-412风控后的长冷却秒数

# ========== 破解配置 ==========
MITM_MAX_UID = 10_000_000_000   # MITM 反查覆盖上限：全部 ≤10 位 UID（16位随机长UID不可解）

# ========== 采集配置 ==========
MAX_COMMENT_PAGES = 100      # 评论最大翻页数（约20条/页，上限~2000条主评论）
COMMENT_REPLY_MAX_PAGES = 25  # 每条主评论的子评论最多补采页数（pn分页，每页20条，上限500条）
HISTORY_DANMAKU_ENABLED = True   # 是否采集全量历史弹幕（需登录）
HISTORY_MAX_MONTHS = 24          # 历史弹幕最多回溯月数
HISTORY_MAX_DAYS = 400           # 历史弹幕最多采集天数（逐日接口，每日1次请求）
MAX_VIDEO_PAGES = 3          # 用户视频最大翻页
MAX_DYNAMIC_PAGES = 10       # 动态最大翻页
# 调研实证：他人关注列表接口仅能查看前 100 个（5页×20），超出返回空列表但 code=0
MAX_FOLLOWING_PAGES = 5       # 关注列表最大翻页（每页20，他人最多100）
MAX_FOLLOWER_PAGES = 2       # 粉丝列表最大翻页
MAX_FAV_CONTENTS = 20        # 收藏夹内容采样数
COLLECT_WORKERS = 3       # 并发采集线程数（BiliAPIClient已线程安全，限速为全局共享）
MAX_UP_SAMPLE = 20        # summarize_followings 深度分析的UP主采样上限（控制请求量防爆）

# ========== 画像配置 ==========
SPAM_HIGH_THRESHOLD = (10, 0.7)    # (弹幕数, 重复率)
SPAM_MEDIUM_THRESHOLD = (5, 0.5)   # (弹幕数, 重复率)
MAX_ANALYZE_USERS_HARD_CAP = 300   # 动态定员安全上限（兴趣命中者超过时按兴趣分截断）
LLM_DEEP_TOP_K = 20                # LLM 重点深掘人数（兴趣分 top K 单人单调用）
CRINGE_BATCH_SIZE = 200            # 问题弹幕检测每批弹幕条数

# ========== Web 报告配置 ==========
WEB_AUTOSTART = True   # run.py/quick_test.py 分析完毕自动启动 web.py 并用浏览器打开报告页（False 关闭）

# ========== LLM 配置 ==========
# DeepSeek 官方 OpenAI 兼容端点（https://api-docs.deepseek.com）
# 填入你的 API Key，或用环境变量 LLM_API_KEY 覆盖；留空则自动跳过 LLM 分析
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "16384"))
