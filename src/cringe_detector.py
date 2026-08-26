"""
问题弹幕检测（LLM 判定）

对合并去重后的全部弹幕按批喂给 LLM，判定八类问题弹幕：
中二抒情 / 尬夸捧杀 / 引战阴阳 / 人身攻击 / 恶意剧透 / 广告引流 / 键政敏感 / 批评吐槽。
按发送者聚合输出，驱动兴趣分选人与报告问题弹幕榜。
未配置 LLM_API_KEY 时返回空 dict（降级不中断）；失败批次不放弃——
429 退避 → 换备用厂商 → 整轮等待重试，直到全部判定成功。

历史说明：模块与函数名沿用 cringe（尬语）命名是兼容旧调用方的最小改动，
实际判定范围已扩展为"问题弹幕"。
"""
import hashlib
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import openai
from openai import OpenAI

from config import (LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_MAX_TOKENS, LLM_CONCURRENCY,
                    LLM_FALLBACK, CRINGE_BATCH_SIZE, COMMENT_CRINGE_BATCH_SIZE,
                    COMMENT_CRINGE_MAX_ITEMS)
from storage import load_llm_cache, save_llm_cache

# 问题弹幕类别（prompt 与聚合均引用，勿散落硬编码字符串）
PROBLEM_CATEGORIES = ["中二抒情", "尬夸捧杀", "引战阴阳", "人身攻击", "恶意剧透", "广告引流", "键政敏感", "批评吐槽"]


def _dedup_contents(danmaku_list: list[dict]) -> list[dict]:
    """按内容去重弹幕，附出现次数（降低 token 消耗）。返回 [{content, count}]，按次数降序"""
    counts = {}
    for dm in danmaku_list:
        c = (dm.get("content") or "").strip()
        if c:
            counts[c] = counts.get(c, 0) + 1
    items = [{"content": c, "count": n} for c, n in counts.items()]
    items.sort(key=lambda x: x["count"], reverse=True)
    return items


def _video_context(video_info: dict) -> str:
    """视频语境行：标题 + 简介截断，帮助 LLM 锚定游戏/社区语境（二游判定防现实语境误读）"""
    title = video_info.get("title", "未知视频")
    desc = (video_info.get("desc") or "").strip().replace("\n", " ")[:100]
    ctx = f"视频《{title}》"
    if desc:
        ctx += f"（简介：{desc}）"
    return ctx


def _build_prompt(batch: list[dict], start_idx: int, video_info: dict) -> str:
    """构建单批问题弹幕判定 prompt（编号为全局下标，便于跨批映射）"""
    lines = [f'{start_idx + i}. {it["content"]}（出现{it["count"]}次）' for i, it in enumerate(batch)]
    return f"""你是熟悉二次元游戏（原神、鸣潮等二游）社区生态的内容审核专家。以下是B站{_video_context(video_info)}的弹幕列表（已按内容去重）。
语境提示：这是二游视频，游戏世界观名词、剧情设定、角色名、卡池/强度讨论、社区黑话都属于游戏语境，不要按现实语境过度解读。
请逐条判定是否属于以下八类"问题弹幕"之一：
- 中二抒情：咯噔文学、疼痛文学、自我感动式过度抒情。发癫文学、"XX我老婆"等二游常见发电行为属正常玩梗，不算
- 尬夸捧杀：无脑吹、饭圈式夸张应援、明显违心的吹捧；对角色/剧情/演出的正常夸赞不算
- 引战阴阳：拉踩其他游戏/角色/阵营/玩家群体、对线、反串、阴阳怪气等攻击性内容
- 人身攻击：辱骂、诅咒、攻击其他观众/UP主/声优；只针对游戏内容本身的批评归「批评吐槽」类
- 恶意剧透：泄露剧情关键信息、结局、反转，或未实装角色/卡池的内鬼爆料
- 广告引流：打广告、推广、引流到其他平台或商品（含代练、卖号、私服）
- 键政敏感：把游戏内容借题引申到现实政治人物、事件、意识形态的键政引战；游戏世界观内的国家/战争/政变等设定讨论不算，歌词/台词/梗中引用的符号性词汇也不算。例：「至冬解体」是游戏国家设定联想、「火焰和钢铁--镰刀与锤子」是歌词接龙，都不算键政敏感
- 批评吐槽：对游戏剧情、角色强度、运营策划的批评、锐评、吐槽（对事不对人；含辱骂或攻击玩家/UP主的归人身攻击；此类严重度一般给1）
正常玩梗、合理讨论、普通应援不算问题弹幕，宁漏勿冤。

弹幕列表：
{chr(10).join(lines)}

请严格只输出一个 JSON 数组，每个元素对应一条判定（只输出判为问题弹幕的条目）：
[{{"i": 编号, "category": "中二抒情|尬夸捧杀|引战阴阳|人身攻击|恶意剧透|广告引流|键政敏感|批评吐槽", "severity": 1到3的整数, "reason": "10字内理由"}}]
没有问题弹幕就输出 []。不要输出任何 JSON 之外的内容。"""


def _parse_verdicts(raw_text: str) -> list[dict]:
    """从 LLM 响应提取 JSON 数组（容错：截取首个 [ 到末个 ]）"""
    left, right = raw_text.find("["), raw_text.rfind("]")
    if left == -1 or right <= left:
        return []
    try:
        data = json.loads(raw_text[left:right + 1])
    except json.JSONDecodeError:
        return []
    return [v for v in data if isinstance(v, dict) and "i" in v]


def _judge_batches(items: list[dict], batch_size: int, video_info: dict,
                   prompt_builder, label: str) -> tuple[list[dict], int, int]:
    """并发执行 LLM 判定批次，返回 (原始verdicts, 失败批次数, 总批次数)。

    实际并发路数 = min(批次数, LLM_CONCURRENCY)——判定阶段无缓存命中，越早判完越好，
    批次少时不空占线程、批次多时打满上限。单批失败重试链：主厂商 429 退避 2 次
    → 换备用厂商（LLM_FALLBACK，双厂商 key 都配了才启用）→ 仍失败进入整轮
    等待重试（60s×轮次递增、封顶 300s），不放弃任何批次直到成功，
    因此返回的失败批次数恒为 0（保留三元组仅维持调用方签名）。
    """
    # 厂商链：主用 + 备用（备用 key 为空则不启用）
    providers = [(LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, "主用")]
    if LLM_FALLBACK[0]:
        providers.append(LLM_FALLBACK)
    clients = {}    # 惰性建 client，线程间共享（openai client 线程安全）

    def client_of(pi: int) -> OpenAI:
        if pi not in clients:
            key, base, _, _ = providers[pi]
            clients[pi] = OpenAI(api_key=key, base_url=base)
        return clients[pi]

    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    total = len(batches)
    workers = max(1, min(total, LLM_CONCURRENCY))

    def work(bi: int) -> str:
        prompt = prompt_builder(batches[bi], bi * batch_size, video_info)
        last_err = None
        for pi, (_, _, model, tag) in enumerate(providers):
            if pi > 0:
                print(f"[{label}] 批次 {bi + 1} 主厂商失败，换备用厂商（{tag}）重试...")
            for retry in range(3):      # 限速退避：429 时等一会再试，最多 2 次重试
                try:
                    resp = client_of(pi).chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=LLM_MAX_TOKENS,
                        temperature=0.3,  # 判定类任务低温，减少格式漂移
                    )
                    return resp.choices[0].message.content or ""
                except openai.RateLimitError as e:
                    last_err = e
                    if retry == 2:
                        break           # 本厂商限速重试耗尽，换下一厂商
                    wait = 10 * (retry + 1) + random.uniform(0, 3)
                    print(f"[{label}] 批次 {bi + 1} 触发限速，{wait:.0f}s 后重试...")
                    time.sleep(wait)
                except Exception as e:
                    last_err = e
                    break               # 非限速错误重试同厂商无意义，直接换下一厂商
        raise last_err

    verdicts = []

    def run_pass(pending: list[int]) -> list[int]:
        """跑一轮并发判定，返回仍失败的批次下标"""
        still_failed = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(work, bi): bi for bi in pending}
            for fut in as_completed(futures):
                bi = futures[fut]
                try:
                    raw = fut.result()
                except Exception as e:
                    print(f"[{label}] 警告: 批次 {bi + 1} 请求失败（{e}），稍后重试")
                    still_failed.append(bi)
                    continue
                batch_verdicts = _parse_verdicts(raw)
                if not batch_verdicts and raw.strip() not in ("", "[]"):
                    print(f"[{label}] 警告: 批次 {bi + 1} 响应解析为空，原始响应前200字符: {raw[:200]!r}")
                verdicts.extend(batch_verdicts)
                print(f"[{label}] 批次 {bi + 1}/{total} 完成（解析 {len(batch_verdicts)} 条）")
        return still_failed

    # 失败批次不放弃：等待递增（60s×轮次，封顶 300s）后整轮重试，直到全部成功
    print(f"[{label}] 判定 {total} 批（并发 {workers} 路，LLM请求中）...")
    pending = run_pass(list(range(total)))
    round_no = 1
    while pending:
        wait = min(60 * round_no, 300) + random.uniform(0, 10)
        print(f"[{label}] {len(pending)} 个批次未成功，{wait:.0f}s 后重试（第 {round_no} 轮）...")
        time.sleep(wait)
        pending = run_pass(pending)
        round_no += 1
    return verdicts, 0, total


def detect_cringe_danmaku(danmaku_list: list[dict], sender_groups: dict[str, dict],
                          video_info: dict) -> dict[str, dict]:
    """
    问题弹幕检测主入口

    Returns:
        {mid_hash: {
            "count": int,            # 该发送者被判问题弹幕的去重内容条数
            "max_severity": int,     # 最高严重度 1-3
            "categories": [str],     # 涉及的问题弹幕类别
            "examples": [{content, category, severity, reason}],  # 至多5条代表原文
            "items": [{content, category, severity, reason}],     # 全量命中（供误报重算聚合）
        }}
        未配置 Key / 全部批次失败 / 无问题弹幕时为相应子集或空 dict
    """
    if not LLM_API_KEY:
        print("[问题弹幕] 未配置 LLM_API_KEY，跳过问题弹幕检测")
        return {}

    items = _dedup_contents(danmaku_list)
    if not items:
        return {}

    # 判定结果缓存：同一视频去重内容集合未变 → 直接复用（重跑零 LLM 调用）
    bvid = video_info.get("bvid", "")
    cache_key = ""
    if bvid:
        # 确定性排序：同内容集合无论输入次序都产生相同 digest（count 降序 + 内容字典序决胜）
        hash_items = sorted(items, key=lambda x: (-x["count"], x["content"]))
        digest = hashlib.sha256(
            json.dumps(hash_items, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        cache_key = f"cringe:{bvid}:v3:{LLM_MODEL}:{digest}"
        cached = load_llm_cache(cache_key)
        if cached is not None:
            try:
                results = json.loads(cached)
                if not isinstance(results, dict):
                    raise ValueError("缓存结果不是 dict")
                print(f"[问题弹幕] 缓存命中（{len(results)} 个发送者），跳过 LLM 判定")
                return results
            except (json.JSONDecodeError, ValueError):
                print("[问题弹幕] 警告: 缓存内容损坏，重新判定")

    # 内容 -> 发送者集合（同一内容可能被多人发送，各自归属）
    content_senders: dict[str, set] = {}
    for mid_hash, group in sender_groups.items():
        for c in group.get("contents", []):
            content_senders.setdefault((c or "").strip(), set()).add(mid_hash)

    raw_verdicts, _, total = _judge_batches(items, CRINGE_BATCH_SIZE, video_info,
                                            _build_prompt, "问题弹幕")

    verdicts = []
    for v in raw_verdicts:
        idx = v.get("i")
        if isinstance(idx, int) and 0 <= idx < len(items) and v.get("category") in PROBLEM_CATEGORIES:
            v["_content"] = items[idx]["content"]
            verdicts.append(v)
    print(f"[问题弹幕] 全部 {total} 批完成：采纳 {len(verdicts)} 条")

    # 按发送者聚合
    results: dict[str, dict] = {}
    for v in verdicts:
        content = v["_content"]
        for mid_hash in content_senders.get(content, ()):
            ent = results.setdefault(mid_hash, {"count": 0, "max_severity": 0,
                                                "categories": [], "examples": [],
                                                "items": []})
            ent["count"] += 1
            sev = v.get("severity", 1)
            # 归一化一次、两处复用：挡 bool/字符串/超界值，钳制到 1-3 的 int
            sev = sev if isinstance(sev, int) and not isinstance(sev, bool) and 1 <= sev <= 3 else 1
            ent["max_severity"] = max(ent["max_severity"], sev)
            cat = v["category"]
            if cat not in ent["categories"]:
                ent["categories"].append(cat)
            item = {"content": content, "category": cat,
                    "severity": sev, "reason": v.get("reason", "")}
            # items 存全量命中（误报标记后 web 端按内容重算聚合）；examples 仍是前5条代表原文
            ent["items"].append(item)
            if len(ent["examples"]) < 5:
                ent["examples"].append(item)

    if cache_key:
        save_llm_cache(cache_key, json.dumps(results, ensure_ascii=False))

    print(f"[问题弹幕] 检测完成: {len(verdicts)} 条问题弹幕，涉及 {len(results)} 个发送者")
    return results


# ========== 问题评论检测（同一 LLM 判定口径，对象为评论） ==========

def _build_comment_prompt(batch: list[dict], start_idx: int, video_info: dict) -> str:
    """构建单批问题评论判定 prompt（编号为全局下标，便于跨批映射）"""
    lines = [f'{start_idx + i}. {it["content"]}' for i, it in enumerate(batch)]
    return f"""你是熟悉二次元游戏（原神、鸣潮等二游）社区生态的内容审核专家。以下是B站{_video_context(video_info)}的评论列表（已按内容去重）。
语境提示：这是二游视频，游戏世界观名词、剧情设定、角色名、卡池/强度讨论、社区黑话都属于游戏语境，不要按现实语境过度解读。
请逐条判定是否属于以下八类"问题评论"之一：
- 中二抒情：咯噔文学、疼痛文学、自我感动式过度抒情。发癫文学、"XX我老婆"等二游常见发电行为属正常玩梗，不算
- 尬夸捧杀：无脑吹、饭圈式夸张应援、明显违心的吹捧；对角色/剧情/演出的正常夸赞不算
- 引战阴阳：拉踩其他游戏/角色/阵营/玩家群体、对线、反串、阴阳怪气等攻击性内容
- 人身攻击：辱骂、诅咒、攻击其他观众/UP主/声优；只针对游戏内容本身的批评归「批评吐槽」类
- 恶意剧透：泄露剧情关键信息、结局、反转，或未实装角色/卡池的内鬼爆料
- 广告引流：打广告、推广、引流到其他平台或商品（含代练、卖号、私服）
- 键政敏感：把游戏内容借题引申到现实政治人物、事件、意识形态的键政引战；游戏世界观内的国家/战争/政变等设定讨论不算，歌词/台词/梗中引用的符号性词汇也不算。例：「至冬解体」是游戏国家设定联想、「火焰和钢铁--镰刀与锤子」是歌词接龙，都不算键政敏感
- 批评吐槽：对游戏剧情、角色强度、运营策划的批评、锐评、吐槽（对事不对人；含辱骂或攻击玩家/UP主的归人身攻击；此类严重度一般给1）
正常玩梗、合理讨论不算问题评论，宁漏勿冤。

评论列表：
{chr(10).join(lines)}

请严格只输出一个 JSON 数组，每个元素对应一条判定（只输出判为问题评论的条目）：
[{{"i": 编号, "category": "中二抒情|尬夸捧杀|引战阴阳|人身攻击|恶意剧透|广告引流|键政敏感|批评吐槽", "severity": 1到3的整数, "reason": "10字内理由"}}]
没有问题评论就输出 []。不要输出任何 JSON 之外的内容。"""


def detect_problem_comments(comments: list[dict], video_info: dict) -> dict[int, dict]:
    """
    问题评论检测主入口（LLM 判定，与问题弹幕同口径）

    Args:
        comments: comment.collect_comment_data 返回的评论列表（含 rpid/content/like）

    Returns:
        {rpid: {"category": str, "severity": int, "reason": str}}
        未配置 Key / 全部批次失败 / 无问题评论时为空 dict
    """
    if not LLM_API_KEY:
        print("[问题评论] 未配置 LLM_API_KEY，跳过问题评论检测")
        return {}
    if not comments:
        return {}

    # 按内容去重（刷屏复制粘贴的评论只判定一次），内容 → rpid 集合用于回映
    content_rpids: dict[str, list[int]] = {}
    like_of: dict[str, int] = {}
    for c in comments:
        content = (c.get("content") or "").strip()
        rpid = c.get("rpid")
        if not content or not rpid:
            continue
        content_rpids.setdefault(content, []).append(rpid)
        like_of[content] = max(like_of.get(content, 0), c.get("like", 0))
    items = [{"content": c} for c in content_rpids]
    # 评论量比弹幕更难压缩：按最高点赞降序截断，优先判定可见度高的评论
    items.sort(key=lambda x: -like_of.get(x["content"], 0))
    if len(items) > COMMENT_CRINGE_MAX_ITEMS:
        print(f"[问题评论] 去重后 {len(items)} 条超出上限，按点赞截取前 {COMMENT_CRINGE_MAX_ITEMS} 条")
        items = items[:COMMENT_CRINGE_MAX_ITEMS]
    if not items:
        return {}

    # 判定结果缓存：同一视频去重内容集合未变 → 直接复用（重跑零 LLM 调用）
    bvid = video_info.get("bvid", "")
    cache_key = ""
    if bvid:
        digest = hashlib.sha256(
            json.dumps(sorted(it["content"] for it in items), ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        cache_key = f"cmt:{bvid}:v3:{LLM_MODEL}:{digest}"
        cached = load_llm_cache(cache_key)
        if cached is not None:
            try:
                results = json.loads(cached)
                if not isinstance(results, dict):
                    raise ValueError("缓存结果不是 dict")
                # 缓存的 rpid 键是字符串（JSON 对象键），转回 int
                print(f"[问题评论] 缓存命中（{len(results)} 条问题评论），跳过 LLM 判定")
                return {int(k): v for k, v in results.items()}
            except (json.JSONDecodeError, ValueError, TypeError):
                print("[问题评论] 警告: 缓存内容损坏，重新判定")

    raw_verdicts, _, total = _judge_batches(items, COMMENT_CRINGE_BATCH_SIZE, video_info,
                                            _build_comment_prompt, "问题评论")

    results: dict[int, dict] = {}
    for v in raw_verdicts:
        idx = v.get("i")
        if not (isinstance(idx, int) and 0 <= idx < len(items)
                and v.get("category") in PROBLEM_CATEGORIES):
            continue
        sev = v.get("severity", 1)
        # 归一化：挡 bool/字符串/超界值，钳制到 1-3 的 int
        sev = sev if isinstance(sev, int) and not isinstance(sev, bool) and 1 <= sev <= 3 else 1
        verdict = {"category": v["category"], "severity": sev, "reason": v.get("reason", "")}
        # 同一内容的所有 rpid 都标注（复制粘贴刷屏的评论同源同罪）
        for rpid in content_rpids.get(items[idx]["content"], []):
            results[rpid] = verdict
    print(f"[问题评论] 全部 {total} 批完成：标注 {len(results)} 条")

    if cache_key:
        save_llm_cache(cache_key, json.dumps(results, ensure_ascii=False))

    print(f"[问题评论] 检测完成: {len(results)} 条问题评论")
    return results
