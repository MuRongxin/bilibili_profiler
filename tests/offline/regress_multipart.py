# -*- coding: utf-8 -*-
"""多分P 口径：密度时间轴按分P 1、样本 mm:ss 标 P{n}（6 项）

背景：danmaku.time 是「所在分P 内的相对秒数」，而 video_info.duration 是各分P 之和。
多分P 视频若用总时长铺轴，横轴后 (P-1)/P 段恒为 0（历史弹幕也只覆盖分P 1）；
样本 mm:ss 不标分P 则同一时间无法解释。

离线回归脚本：使用隔离临时库，**不联网、不用 Cookie、不消耗 LLM 额度**。
从仓库任意目录均可运行：  python tests/offline/regress_multipart.py
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓库根目录
sys.path.insert(0, str(ROOT / "src"))            # 扁平导入 src/ 下模块
sys.path.insert(0, str(ROOT))                    # 便于 import web.py

import tempfile
TMP = tempfile.mkdtemp(prefix="mp_")
import storage, config
storage.DB_PATH = os.path.join(TMP, "t.db"); config.DB_PATH = storage.DB_PATH
storage.init_db()
import web
import report as rp

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ✔ %s %s" % (name, extra))
    else: fail += 1; print("  ✘ %s %s" % (name, extra))

BVID = "BVmp000001"
def _dm(i, page, time):
    return {"mid_hash": "h%d" % i, "content": "c%d" % i, "time": float(time),
            "timestamp": 1700000000 + i, "mode": 1, "color": "#ffffff", "pool": 0,
            "dmid": i, "page": page}

# 分P 1 三条 + 分P 2 两条（分P 内相对时间，最大 100s）
rows = [_dm(1, 1, 10), _dm(2, 1, 50), _dm(3, 1, 100), _dm(4, 2, 10), _dm(5, 2, 20)]
storage.append_danmaku(BVID, rows, set())

# 4 分P：总时长 418，分P 1 时长 109（与真实 BV1mtTD6rEtQ 同构）
multi_vi = {"duration": 418, "pages": [
    {"cid": 1, "page": 1, "duration": 109}, {"cid": 2, "page": 2, "duration": 103},
    {"cid": 3, "page": 3, "duration": 103}, {"cid": 4, "page": 4, "duration": 103}]}
single_vi = {"duration": 418}

d = web._danmaku_density(BVID, multi_vi, 418)
check("多分P 横轴用分P 1 时长（10 桶、末桶起始 98s）",
      bool(d) and len(d["data"]) == 10 and d["starts"][-1] == 98,
      "(桶 %s, 末桶起始 %s)" % (len(d["data"]) if d else None,
                                d["starts"][-1] if d else None))
check("多分P 只统计分P 1 的弹幕（3 条，不含分P 2）",
      bool(d) and sum(d["data"]) == 3, "(合计 %s)" % (sum(d["data"]) if d else None))
check("多分P 标题标注分P 数", bool(d) and "4P" in d.get("page_label", ""),
      "(%r)" % (d.get("page_label") if d else None))

d1 = web._danmaku_density(BVID, single_vi, 418)
check("单分P 行为不变（全量统计、无分P 标注）",
      bool(d1) and sum(d1["data"]) == 5 and d1.get("page_label") == "",
      "(合计 %s, label=%r)" % (sum(d1["data"]) if d1 else None,
                               d1.get("page_label") if d1 else None))

# 卡片：多分P 样本必须带 P{n}，且按分P 再按分P 内时间排序
card_multi = {"uid": 1, "name": "t", "danmaku": {
    "count": 2, "contents": ["晚-但属P1", "早-但属P2"], "video_times": [100.0, 10.0],
    "video_pages": [1, 2], "multi_page": True, "spam_level": "低", "spam_score": 0.0}}
lis = re.findall(r'<li>(.*?)</li>', rp.generate_user_card(card_multi))
check("多分P 样本带 P{n} 且先按分P 排序",
      len(lis) == 2 and "P1 01:40" in lis[0] and "P2 00:10" in lis[1],
      "(%r)" % (lis,))

card_single = {"uid": 2, "name": "t", "danmaku": {
    "count": 1, "contents": ["x"], "video_times": [10.0],
    "video_pages": [], "multi_page": False, "spam_level": "低", "spam_score": 0.0}}
lis2 = re.findall(r'<li>(.*?)</li>', rp.generate_user_card(card_single))
check("单分P 样本不带 P 前缀（渲染不变）",
      len(lis2) == 1 and "P1" not in lis2[0] and "00:10" in lis2[0], "(%r)" % (lis2,))

print("")
print("==== 多分P 口径: %d 项通过, %d 项失败 ====" % (ok, fail))
sys.exit(1 if fail else 0)
