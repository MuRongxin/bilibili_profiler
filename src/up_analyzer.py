"""
UP主分析器 — 分析用户关注列表中UP主的投稿特征

参考 biliscope 的思路，通过 UP主 的投稿历史分析其内容类型和活跃度。
支持并行分析（ThreadPoolExecutor）加速：BiliAPIClient 已线程安全，
限速为全局共享（多线程不会突破 config.REQUEST_DELAY 的请求频率）。
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import USER_VIDEOS_LEGACY_URL, USER_CARD_URL, COLLECT_WORKERS
from api_client import BiliAPIClient


def _tokenize(text: str) -> list[str]:
    """简单中文分词：提取2字及以上连续中文字符"""
    words = re.findall(r'[\u4e00-\u9fff]{2,}', text)
    # 过滤常见停用词
    stop = {'一个','一下','可以','什么','没有','不是','这个','那个','还是','不要','已经','知道','觉得','真的','就是'}
    return [w for w in words if w not in stop]


CATEGORY_MAP = {
    # 动画
    1: "动画", 24: "MAD·AMV", 25: "MMD·3D", 47: "短片·手书·配音", 86: "手办·模玩",
    27: "综合",
    # 游戏
    4: "游戏", 17: "单机游戏", 65: "网络游戏", 121: "电子竞技", 136: "手机游戏",
    140: "桌游棋牌", 165: "音游", 172: "GMV", 173: "Mugen", 250: "游戏综合",
    # 知识
    36: "知识", 122: "科学科普", 124: "社科法律", 228: "人文历史",
    207: "校园学习", 208: "职业职场", 209: "设计·创意",
    # 生活
    160: "生活", 21: "日常", 76: "美食", 75: "动物圈", 138: "搞笑",
    161: "手工", 162: "绘画", 163: "运动", 174: "汽车", 175: "三农",
    # 娱乐
    5: "娱乐", 71: "综艺", 137: "明星", 239: "Korea相关",
    # 影视
    181: "影视", 182: "影视杂谈", 183: "影视剪辑", 85: "短片",
    177: "纪录片", 23: "电影", 11: "电视剧",
    # 音乐
    3: "音乐", 28: "原创音乐", 29: "翻唱", 30: "VOCALOID", 31: "演奏",
    59: "音乐综合", 130: "MV", 193: "电音", 194: "说唱",
    # 时尚
    155: "时尚", 156: "美妆", 157: "服饰", 158: "健身",
    # 科技
    188: "科技", 189: "数码", 190: "软件应用", 191: "计算机技术",
    # 鬼畜
    119: "鬼畜", 22: "鬼畜调教", 26: "音MAD", 126: "人力VOCALOID",
}


def _friendly_category(typeid: int) -> str:
    return CATEGORY_MAP.get(typeid, f"其他({typeid})")


def analyze_up(uid: int, client) -> dict:
    """
    分析单个UP主的投稿特征

    Returns:
        {
            "uid": int, "name": str,
            "video_count": 总投稿数,
            "follower": 粉丝数,
            "top_category": "游戏" | "知识" | ...,
            "category_dist": {"游戏": 30, "生活": 10, ...},
            "active_30d": 近30天投稿数,
            "last_post": "2025-01-15" | None,
        }
    """
    result = {
        "uid": uid, "name": "",
        "video_count": 0, "follower": 0,
        "top_category": "未知", "category_dist": {},
        "active_30d": 0, "last_post": None,
        "word_freq": {},  # 标题词频
    }

    # 名片与投稿列表两个请求互不依赖，并行发出（懒加载场景下词云出图时间直接减半）
    with ThreadPoolExecutor(max_workers=2) as ex:
        card_fut = ex.submit(lambda: client.get(USER_CARD_URL, params={"mid": uid}))
        videos_fut = ex.submit(lambda: client.get(USER_VIDEOS_LEGACY_URL, params={
            "mid": uid, "ps": 50, "pn": 1,
            "order": "pubdate", "order_avoided": "true",
        }))

    # 获取 UP主 基础信息
    try:
        card = card_fut.result()
        if card.get("code") == 0:
            cdata = card["data"].get("card", {})
            result["name"] = cdata.get("name", "")
            result["follower"] = cdata.get("fans", 0)
    except Exception as e:
        print(f"  [UP] 警告: 名片接口异常（UID:{uid}）: {e}")

    # 获取投稿列表（第一页就够了，分析分区分布和最近活跃度）
    now = time.time()
    threshold_30d = now - 30 * 24 * 3600

    try:
        # 分区分析依赖 typeid，新接口 recArchivesByKeywords 不返回分区，固定用旧 arc/search
        data = videos_fut.result()
        if data.get("code") == 0:
            vlist = data["data"]["list"]["vlist"]
            result["video_count"] = data["data"]["page"]["count"]

            cats = {}
            word_freq = {}
            for v in vlist:
                tid = v.get("typeid", 0)
                cat_name = _friendly_category(tid)
                cats[cat_name] = cats.get(cat_name, 0) + 1

                # 标题词频（简单分词：2字及以上词组）
                title = v.get("title", "")
                for w in _tokenize(title):
                    word_freq[w] = word_freq.get(w, 0) + 1

                # 近30天投稿计数（last_post 在循环结束后才赋值，原 not result["last_post"] 条件恒真，已删除）
                if v.get("created", 0) > threshold_30d:
                    result["active_30d"] += 1

            result["category_dist"] = cats
            result["word_freq"] = word_freq
            if cats:
                result["top_category"] = max(cats, key=cats.get)

            # last_post from first (most recent) video
            if vlist:
                created = vlist[0].get("created", 0)
                if created:
                    result["last_post"] = time.strftime(
                        "%Y-%m-%d", time.localtime(created)
                    )
    except Exception as e:
        print(f"  [UP] 警告: 投稿列表接口异常（UID:{uid}）: {e}")

    return result


def fetch_up_wordcloud(uid: int, client: BiliAPIClient) -> dict:
    """轻量词云采集（报告页悬停懒加载专用）：只发 1 个投稿列表请求，且走
    immediate 交互式免限速通道（悬停是单次交互，不属批量采集）——
    对比 analyze_up 的名片+投稿双请求，出词延迟从「2 次限速+2 次网络」降到 1 次网络。
    UP 主昵称从投稿列表响应顺带取（vlist[0].author），名片接口不再调。
    返回 {"name": str, "word_freq": {词: 次}}；失败返回空词频。"""
    result = {"name": "", "word_freq": {}}
    try:
        data = client.get(USER_VIDEOS_LEGACY_URL, params={
            "mid": uid, "ps": 50, "pn": 1,
            "order": "pubdate", "order_avoided": "true",
        }, immediate=True)
        if data.get("code") == 0:
            vlist = data["data"]["list"]["vlist"]
            if vlist:
                result["name"] = vlist[0].get("author", "")
            wf: dict[str, int] = {}
            for v in vlist:
                for w in _tokenize(v.get("title", "")):
                    wf[w] = wf.get(w, 0) + 1
            result["word_freq"] = wf
    except Exception as e:
        print(f"  [UP] 警告: 词云轻量采集异常（UID:{uid}）: {e}")
    return result


def summarize_followings(followings: list[dict], client, sample_size: int = 50) -> dict:
    """
    并行分析关注列表，汇总UP主特征
    sample_size 默认 50 为安全默认（不传时限制请求量，避免对超长关注列表全量打请求）；
    传 0 表示分析全部；调用方一般显式传 config.MAX_UP_SAMPLE。
    analyze_up 内部全部使用局部变量，无共享可变状态，线程安全。
    """
    total = len(followings)
    sample = followings if sample_size <= 0 else followings[:sample_size]

    categories = {}
    total_follower = 0
    big = small = active = 0
    names = []
    results = []

    # 并行分析每个 UP主（受控并发：COLLECT_WORKERS 线程，限速由客户端全局共享）
    uids = [f.get("uid", 0) for f in sample if f.get("uid")]
    with ThreadPoolExecutor(max_workers=COLLECT_WORKERS) as executor:
        futures = {executor.submit(analyze_up, uid, client): uid for uid in uids}
        for future in as_completed(futures):
            try:
                info = future.result()
                results.append(info)
            except Exception as e:
                print(f"  [UP] 警告: 分析UP主 UID:{futures[future]} 失败: {e}")

    # 汇总 + 聚合词频
    word_freq_all = {}
    for info in results:
        names.append(info["name"] or "?")
        for cat, cnt in info["category_dist"].items():
            categories[cat] = categories.get(cat, 0) + 1
        # 聚合词频
        for w, c in info.get("word_freq", {}).items():
            word_freq_all[w] = word_freq_all.get(w, 0) + c

        follower = info["follower"]
        total_follower += follower
        if follower > 100000:
            big += 1
        elif follower < 10000:
            small += 1
        if info["active_30d"] > 0:
            active += 1

    n = max(len(names), 1)
    top_cats = sorted(categories.items(), key=lambda x: x[1], reverse=True)[:5]
    top_words = sorted(word_freq_all.items(), key=lambda x: x[1], reverse=True)[:80]

    return {
        "total": total,
        "sampled": len(names),
        "top_categories": top_cats,
        "avg_follower": total_follower // n,
        "big_creators": big,
        "small_creators": small,
        "active_ratio": f"{active}/{n}",
        "sample_names": names[:10],
        "word_freq": top_words,  # 聚合词频（词云用）
        "up_details": [
            {
                "name": r["name"],
                "uid": r["uid"],
                "follower": r["follower"],
                "video_count": r["video_count"],
                "top_category": r["top_category"],
                "active_30d": r["active_30d"],
                "word_freq": [[w, c] for w, c in sorted(
                    r.get("word_freq", {}).items(),
                    key=lambda x: x[1], reverse=True
                )[:60]],
            }
            for r in results
        ],
    }
