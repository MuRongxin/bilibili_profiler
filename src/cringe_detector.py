"""
问题弹幕检测（LLM 判定）

对合并去重后的全部弹幕按批喂给 LLM，判定七类问题弹幕：
中二抒情 / 尬夸捧杀 / 引战阴阳 / 人身攻击 / 恶意剧透 / 广告引流 / 键政敏感。
按发送者聚合输出，驱动兴趣分选人与报告问题弹幕榜。
未配置 LLM_API_KEY 或全部批次失败时返回空 dict（降级不中断）。

历史说明：模块与函数名沿用 cringe（尬语）命名是兼容旧调用方的最小改动，
实际判定范围已扩展为"问题弹幕"。
"""
import hashlib
import json

from openai import OpenAI

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_MAX_TOKENS, CRINGE_BATCH_SIZE
from storage import load_llm_cache, save_llm_cache

# 问题弹幕类别（与 spec 一致；prompt 与聚合均引用，勿散落硬编码字符串）
PROBLEM_CATEGORIES = ["中二抒情", "尬夸捧杀", "引战阴阳", "人身攻击", "恶意剧透", "广告引流", "键政敏感"]


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


def _build_prompt(batch: list[dict], start_idx: int, video_title: str) -> str:
    """构建单批问题弹幕判定 prompt（编号为全局下标，便于跨批映射）"""
    lines = [f'{start_idx + i}. {it["content"]}（出现{it["count"]}次）' for i, it in enumerate(batch)]
    return f"""你是中文互联网内容审核专家。以下是B站视频《{video_title}》的弹幕列表（已按内容去重）。
请逐条判定是否属于以下七类"问题弹幕"之一：
- 中二抒情：咯噔文学、疼痛文学、过度深情、自我感动式抒情
- 尬夸捧杀：无脑吹、饭圈式夸张应援、明显违心的吹捧
- 引战阴阳：拉踩、对线、反串、阴阳怪气等攻击性内容
- 人身攻击：辱骂、诅咒、攻击其他观众/UP主/视频角色
- 恶意剧透：泄露剧情关键信息、结局、反转
- 广告引流：打广告、推广、引流到其他平台或商品
- 键政敏感：借题发挥的政治隐喻、键政引战
正常玩梗、合理讨论、普通应援不算问题弹幕，宁漏勿冤。

弹幕列表：
{chr(10).join(lines)}

请严格只输出一个 JSON 数组，每个元素对应一条判定（只输出判为问题弹幕的条目）：
[{{"i": 编号, "category": "中二抒情|尬夸捧杀|引战阴阳|人身攻击|恶意剧透|广告引流|键政敏感", "severity": 1到3的整数, "reason": "10字内理由"}}]
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
        cache_key = f"cringe:{bvid}:{digest}"
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

    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    title = video_info.get("title", "未知视频")
    verdicts = []
    batches = [items[i:i + CRINGE_BATCH_SIZE] for i in range(0, len(items), CRINGE_BATCH_SIZE)]
    failed = 0
    for bi, batch in enumerate(batches, 1):
        start_idx = (bi - 1) * CRINGE_BATCH_SIZE
        print(f"[问题弹幕] 判定 {bi}/{len(batches)} 批（{len(batch)} 条，LLM请求中）...")
        try:
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": _build_prompt(batch, start_idx, title)}],
                max_tokens=LLM_MAX_TOKENS,
                temperature=0.3,  # 判定类任务低温，减少格式漂移
            )
            raw = resp.choices[0].message.content or ""
        except Exception as e:
            print(f"[问题弹幕] 警告: 批次 {bi} 请求失败（{e}），跳过该批")
            failed += 1
            continue
        batch_verdicts = _parse_verdicts(raw)
        if not batch_verdicts and raw.strip() not in ("", "[]"):
            print(f"[问题弹幕] 警告: 批次 {bi} 响应解析为空，原始响应前200字符: {raw[:200]!r}")
        accepted = 0
        for v in batch_verdicts:
            idx = v.get("i")
            if isinstance(idx, int) and 0 <= idx < len(items) and v.get("category") in PROBLEM_CATEGORIES:
                v["_content"] = items[idx]["content"]
                verdicts.append(v)
                accepted += 1
        print(f"[问题弹幕] 批次 {bi}: 采纳 {accepted} 条（解析 {len(batch_verdicts)} 条）")

    if failed == len(batches):
        print("[问题弹幕] 警告: 全部批次失败，问题弹幕检测降级为空")
        return {}

    # 按发送者聚合
    results: dict[str, dict] = {}
    for v in verdicts:
        content = v["_content"]
        for mid_hash in content_senders.get(content, ()):
            ent = results.setdefault(mid_hash, {"count": 0, "max_severity": 0,
                                                "categories": [], "examples": []})
            ent["count"] += 1
            sev = v.get("severity", 1)
            # 归一化一次、两处复用：挡 bool/字符串/超界值，钳制到 1-3 的 int
            sev = sev if isinstance(sev, int) and not isinstance(sev, bool) and 1 <= sev <= 3 else 1
            ent["max_severity"] = max(ent["max_severity"], sev)
            cat = v["category"]
            if cat not in ent["categories"]:
                ent["categories"].append(cat)
            if len(ent["examples"]) < 5:
                ent["examples"].append({
                    "content": content, "category": cat,
                    "severity": sev, "reason": v.get("reason", ""),
                })

    if cache_key:
        if failed > 0:
            # 部分批次失败时聚合结果不完整，不写缓存避免瞬态波动被冻结复用
            print(f"[问题弹幕] {failed} 个批次失败，本次结果不写入缓存")
        else:
            save_llm_cache(cache_key, json.dumps(results, ensure_ascii=False))

    print(f"[问题弹幕] 检测完成: {len(verdicts)} 条问题弹幕，涉及 {len(results)} 个发送者")
    return results
