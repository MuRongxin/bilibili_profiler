"""
HTML报告生成器

生成包含统计图表、词云和用户深度画像的交互式HTML报告。
"""
import json
import re
import os
from datetime import datetime
from collections import Counter

from config import REPORT_DIR


def generate_user_card(profile: dict) -> str:
    """生成单个用户的画像卡片HTML"""
    uid = profile.get("uid", 0)
    name = profile.get("name", "未知")
    face = profile.get("face", "")
    sign = profile.get("sign", "")
    level = profile.get("level", 0)
    sex = profile.get("sex", "")
    tags = profile.get("tags", [])

    # 标签HTML
    tag_html = "".join(f'<span class="tag">{t}</span>' for t in tags)

    # 基础信息
    follower = profile.get("follower", 0)
    following = profile.get("following", 0)
    like_num = profile.get("like_num", 0)

    # 弹幕信息
    dm = profile.get("danmaku", {})
    dm_count = dm.get("count", 0)
    dm_contents = dm.get("contents", [])
    dm_times = dm.get("video_times", [])
    # 按时间戳排序
    paired = list(zip(dm_contents, dm_times)) if dm_times else [(c, 0) for c in dm_contents]
    paired.sort(key=lambda x: x[1])
    dm_items = []
    for c, t in paired:
        m, s = int(t // 60), int(t % 60)
        dm_items.append(f"<li>{c} <span class=\"dm-time\">{m:02d}:{s:02d}</span></li>")
    dm_list = "".join(dm_items)
    spam_level = dm.get("spam_level", "低")
    spam_reason = dm.get("spam_reason", "")
    spam_class = f"spam-{spam_level}"

    # 活动模式
    act = profile.get("activity_pattern", {})
    act_type = act.get("activity_type", "未知")
    peak_hour = act.get("peak_hour")
    peak_day = act.get("peak_day")

    # 收藏夹
    fav = profile.get("favorite", {})
    fav_names = fav.get("names", [])[:5]

    # 视频
    vid = profile.get("video", {})
    vid_count = vid.get("count", 0)
    vid_titles = vid.get("recent_titles", [])[:3]

    # 动态（过滤空内容）
    dyn = profile.get("dynamic", {})
    dyn_count = dyn.get("count", 0)
    dyn_contents = [c for c in dyn.get("recent_contents", [])[:3] if c.strip()]

    # 关注偏好
    fol_summary = profile.get("following_summary", {})
    all_names = profile.get("all_following_names", [])
    all_raw = profile.get("all_followings_raw", [])
    # name → raw info 映射
    raw_map = {r["name"]: r for r in all_raw}
    fol_section = ""
    if all_names:
        # 构建 name → up_details 映射（有深度分析的才显示详情）
        up_detail_map = {}
        for i, up in enumerate(fol_summary.get("up_details", [])):
            up_detail_map[up["name"]] = (i, up)

        cats_str = "、".join(f"{c}({n})" for c, n in fol_summary.get("top_categories", [])[:4])
        up_names = ""
        for name in all_names:
            if name in up_detail_map:
                i, up = up_detail_map[name]
                wf = up.get("word_freq", [])
                tip = f"粉丝:{up.get('follower',0):,} | 投稿:{up.get('video_count',0)} | 分区:{up.get('top_category','?')}"
                kw = ", ".join(w for w, _ in wf[:5]) if wf else ""
                if kw:
                    tip += f" | 关键词: {kw}"
                up_id = f"up_{uid}_{i}"
                up_names += f'<span class="up-chip" data-upid="{up_id}" data-uid="{uid}" title="{tip}">{name}</span>'
            else:
                raw = raw_map.get(name, {})
                sign = raw.get("sign", "")
                tip = f"签名: {sign}" if sign else "未深度分析"
                up_names += f'<span class="up-chip" title="{tip}">{name}</span>'

        total = fol_summary.get("total", len(all_names))
        fol_section = f'''
            <div class="section fol-section">
                <h4>🕸️ 关注偏好</h4>
                <div class="fol-stats">
                    <span>关注 {total} 人 (全部展示)</span>
                    <span>偏好: {cats_str}</span>
                </div>
                <div class="up-chips">{up_names}</div>
            </div>'''

    # 追番
    bangumi = profile.get("bangumi_titles", [])[:3]
    dramas = profile.get("drama_titles", [])[:3]

    # AI画像分析
    ai_text = profile.get("ai_analysis", "")
    ai_section = ""
    if ai_text:
        # 简单渲染：转义 + 换行 + 粗体
        safe = ai_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        safe = safe.replace("**", "<strong>", 1)
        if "**" in safe:
            safe = safe.replace("**", "</strong>", 1)
        paragraphs = safe.split("\n")
        ai_html = "".join(f"<p>{p}</p>" if p.strip() else "<br>" for p in paragraphs)
        ai_section = f'''
            <div class="section ai-section">
                <h4>🤖 AI 深度画像</h4>
                <div class="ai-text">{ai_html}</div>
            </div>'''

    # 头像 (可点击跳转B站主页)
    profile_url = f"https://space.bilibili.com/{uid}"
    avatar_html = f'<a href="{profile_url}" target="_blank"><img src="{face}" alt="{name}" loading="lazy" onerror="this.style.display=\'none\'"></a>' if face else f'<div class="avatar-text">{name[0] if name else "?"}</div>'

    return f'''
    <div class="user-card" data-level="{level}" data-vip="{profile.get('vip_status',0)==1}" data-spam="{spam_level}" data-official="{profile.get('official_type',-1)>=0}">
        <div class="card-header">
            <div class="avatar">{avatar_html}</div>
            <div class="header-info">
                <div class="name-line">
                    <a href="{profile_url}" target="_blank" class="username-link"><span class="username">{name}</span></a>
                    <span class="uid">UID:{uid}</span>
                    <span class="level-badge">Lv.{level}</span>
                    { '<span class="vip-badge">大会员</span>' if profile.get('vip_status')==1 else '' }
                    { '<span class="risk-badge" title="该UID由CRC32暴力破解得出，存在碰撞误识别风险">可能误识别</span>' if profile.get('collision_risk') else '' }
                </div>
                <div class="tags">{tag_html}</div>
                {f'<div class="sign">{sign}</div>' if sign else ''}
            </div>
        </div>

        <div class="stats-bar">
            <div class="stat"><div class="num">{follower:,}</div><div class="label">粉丝</div></div>
            <div class="stat"><div class="num">{following:,}</div><div class="label">关注</div></div>
            <div class="stat"><div class="num">{like_num:,}</div><div class="label">获赞</div></div>
            <div class="stat"><div class="num">{vid_count}</div><div class="label">投稿</div></div>
            <div class="stat"><div class="num">{dyn_count}</div><div class="label">动态</div></div>
        </div>

        <div class="card-body">
            <div class="section">
                <h4>🎤 弹幕行为 <span class="spam-badge {spam_class}">{spam_level}风险</span></h4>
                <div class="detail">共发送 {dm_count} 条弹幕</div>
                <ol class="dm-list">{dm_list}</ol>
                {f'<div class="reason">判定: {spam_reason}</div>' if spam_reason else ''}
            </div>
            <div class="section">
                <h4>👤 基础信息</h4>
                <div class="info-grid">
                    <span>性别: {sex or "未知"}</span>
                    <span>活跃模式: {act_type}</span>
                    {f'<span>高峰时段: {peak_hour}:00</span>' if peak_hour is not None else ''}
                    {f'<span>活跃星期: {peak_day}</span>' if peak_day else ''}
                </div>
            </div>

            {f'''<div class="section"><h4>📁 收藏夹 ({fav.get("folder_count",0)}个)</h4><div class="samples">{''.join(f'<span class="sample">{n}</span>' for n in fav_names)}</div></div>''' if fav_names else ''}

            {f'''<div class="section"><h4>🎬 最近投稿</h4><ul class="list">{''.join(f'<li>{t}</li>' for t in vid_titles)}</ul></div>''' if vid_titles else ''}

            {fol_section}

            {f'''<div class="section"><h4>📺 追番/追剧</h4><div class="samples">{''.join(f'<span class="sample">{b}</span>' for b in bangumi + dramas)}</div></div>''' if bangumi or dramas else ''}

{ai_section}
        </div>
    </div>
    '''


def generate_summary_stats(profiles: list[dict]) -> dict:
    """生成汇总统计数据"""
    levels = Counter()
    vip_count = 0
    spam_levels = Counter()
    tags = Counter()

    for p in profiles:
        levels[p.get("level", 0)] += 1
        if p.get("vip_status") == 1:
            vip_count += 1
        spam_levels[p.get("danmaku", {}).get("spam_level", "低")] += 1
        for t in p.get("tags", []):
            tags[t] += 1

    return {
        "total": len(profiles),
        "levels": dict(levels),
        "vip_count": vip_count,
        "spam_levels": dict(spam_levels),
        "top_tags": dict(tags.most_common(15)),
    }


def generate_html_report(video_info: dict, profiles: list[dict]) -> str:
    """生成完整HTML报告"""
    stats = generate_summary_stats(profiles)
    title = video_info.get("title", "未知视频")
    bvid = video_info.get("bvid", "")

    # 用户卡片
    cards_html = "".join(generate_user_card(p) for p in profiles)

    # 图表数据
    level_labels = [f"Lv.{i}" for i in range(7)]
    level_data = [stats["levels"].get(i, 0) for i in range(7)]

    spam_labels = ["低", "中", "高"]
    spam_data = [stats["spam_levels"].get(l, 0) for l in spam_labels]

    tag_labels = list(stats["top_tags"].keys())[:10]
    tag_data = list(stats["top_tags"].values())[:10]

    # 统计AI画像覆盖
    ai_count = sum(1 for p in profiles if p.get("ai_analysis"))

    # 收集所有UP主词云数据 (per-upid word freq)
    up_wc_entries = []
    for p in profiles:
        uid = p.get("uid", 0)
        fol = p.get("following_summary", {})
        for i, up in enumerate(fol.get("up_details", [])):
            wf = up.get("word_freq", [])
            if wf:
                up_id = f"up_{uid}_{i}"
                up_wc_entries.append(f'"{up_id}":{json.dumps(wf, ensure_ascii=False)}')
    up_wc_js = "{" + ",".join(up_wc_entries) + "}"

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>B站弹幕用户画像分析 - {title}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/wordcloud@1.2.2/src/wordcloud2.min.js"></script>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:#e8ecf1; color:#333; line-height:1.7; font-size:17px; }}
.container {{ max-width:1400px; margin:0 auto; padding:24px; }}

.header {{ background:linear-gradient(135deg,#00a1d6,#fb7299); color:white; padding:40px; border-radius:16px; margin-bottom:30px; box-shadow:0 10px40px rgba(0,161,214,0.2); }}
.header h1 {{ font-size:30px; margin-bottom:10px; }}
.header .meta {{ opacity:0.9; font-size:15px; }}

.stats-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:15px; margin-bottom:30px; }}
.stat-card {{ background:white; padding:20px; border-radius:12px; box-shadow:0 2px8px rgba(0,0,0,0.04); text-align:center; }}
.stat-card .num {{ font-size:36px; font-weight:700; color:#00a1d6; }}
.stat-card .label {{ font-size:13px; color:#999; margin-top:5px; }}

.charts-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(400px,1fr)); gap:20px; margin-bottom:30px; }}
.chart-card {{ background:white; padding:25px; border-radius:12px; box-shadow:0 2px8px rgba(0,0,0,0.04); }}
.chart-card h3 {{ font-size:17px; margin-bottom:15px; color:#555; }}

.filter-bar {{ display:flex; gap:10px; margin-bottom:20px; flex-wrap:wrap; }}
.filter-btn {{ padding:8px20px; border:2px solid #e0e0e0; border-radius:25px; background:white; cursor:pointer; font-size:14px; transition:all 0.2s; }}
.filter-btn:hover {{ border-color:#00a1d6; color:#00a1d6; }}
.filter-btn.active {{ background:#00a1d6; color:white; border-color:#00a1d6; }}

.user-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(520px,1fr)); gap:20px; }}
.user-card {{ background:white; border-radius:14px; overflow:hidden; box-shadow:0 4px 24px rgba(0,0,0,0.10); transition:all 0.3s; border:1px solid #e8e8e8; }}
.user-card:hover {{ transform:translateY(-3px); box-shadow:0 8px 40px rgba(0,0,0,0.15); }}

.card-header {{ display:flex; align-items:flex-start; padding:20px; background:linear-gradient(135deg,#fafafa,#f0f0f0); }}
.avatar {{ width:60px; height:60px; border-radius:50%; overflow:hidden; margin-right:15px; flex-shrink:0; background:#e0e0e0; display:flex; align-items:center; justify-content:center; }}
.avatar img {{ width:100%; height:100%; object-fit:cover; }}
.avatar-text {{ font-size:24px; font-weight:bold; color:#00a1d6; }}
.header-info {{ flex:1; }}
.name-line {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:6px; }}
.username {{ font-size:20px; font-weight:600; }}
.username-link {{ text-decoration:none; color:inherit; }}
.username-link:hover {{ color:#00a1d6; }}
.uid {{ font-size:12px; color:#999; background:#f0f0f0; padding:2px8px; border-radius:10px; }}
.level-badge {{ font-size:12px; background:#00a1d6; color:white; padding:2px8px; border-radius:10px; }}
.vip-badge {{ font-size:12px; background:#fb7299; color:white; padding:2px8px; border-radius:10px; }}
.risk-badge {{ font-size:12px; background:#ff9800; color:white; padding:2px8px; border-radius:10px; }}
.sign {{ font-size:13px; color:#666; margin-top:4px; }}
.tags {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }}
.tag {{ font-size:12px; background:#e3f2fd; color:#1976d2; padding:3px10px; border-radius:12px; }}

.stats-bar {{ display:grid; grid-template-columns:repeat(5,1fr); gap:10px; padding:15px20px; border-bottom:1px solid #f0f0f0; }}
.stats-bar .stat {{ text-align:center; }}
.stats-bar .num {{ font-size:18px; font-weight:700; color:#00a1d6; }}
.stats-bar .label {{ font-size:12px; color:#999; }}

.card-body {{ padding:15px20px; }}
.section {{ margin-bottom:15px; padding-bottom:15px; border-bottom:1px solid #f5f5f5; }}
.section:last-child {{ border-bottom:none; margin-bottom:0; }}
.section h4 {{ font-size:15px; color:#555; margin-bottom:10px; display:flex; align-items:center; gap:8px; }}
.spam-badge {{ font-size:12px; padding:2px8px; border-radius:10px; margin-left:auto; }}
.spam-低 {{ background:#e8f5e9; color:#388e3c; }}
.spam-中 {{ background:#fff3e0; color:#f57c00; }}
.spam-高 {{ background:#ffebee; color:#d32f2f; }}
.detail {{ font-size:13px; color:#666; margin-bottom:8px; }}
.reason {{ font-size:12px; color:#d32f2f; margin-top:6px; }}
.info-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; font-size:18px; color:#555; }}
.list {{ list-style:none; font-size:13px; color:#555; }}
.list li {{ padding:4px0; border-bottom:1px dotted #eee; }}
.list li:last-child {{ border-bottom:none; }}

/* 弹幕编号列表 */
.dm-list {{ font-size:14px; color:#444; padding-left:24px; margin:6px 0; max-height:200px; overflow-y:auto; }}
.dm-list li {{ padding:6px 0; border-bottom:1px dotted #eee; line-height:1.5; }}
.dm-list li:last-child {{ border-bottom:none; }}

.dm-time {{ font-size:12px; color:#999; margin-left:6px; font-family:monospace; background:#f0f0f0; padding:1px 6px; border-radius:4px; }}

.samples {{ display:flex; flex-wrap:wrap; gap:6px; }}
.sample {{ font-size:13px; background:#f5f5f5; color:#555; padding:4px10px; border-radius:8px; max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.sample.following {{ background:#e8f5e9; color:#2e7d32; }}

.ai-section {{ background:#f8f9ff; border-radius:8px; padding:12px 15px; }}
.ai-section h4 {{ color:#6c5ce7; }}
.ai-text {{ font-size:14px; color:#555; line-height:1.8; }}
.ai-text p {{ margin:6px 0; }}
.ai-text strong {{ color:#333; }}
.ai-text br {{ display:block; content:''; margin:2px 0; }}

/* 关注偏好 */
.fol-section {{ background:#f0f4ff; border-radius:8px; padding:14px 16px; }}
.fol-stats {{ display:flex; gap:16px; flex-wrap:wrap; font-size:14px; color:#555; margin-bottom:10px; }}
.fol-stats span {{ background:white; padding:3px 10px; border-radius:10px; font-weight:500; }}
.up-chips {{ display:flex; flex-wrap:wrap; gap:8px; max-height:120px; overflow-y:auto; }}
.up-chip {{ font-size:14px; background:white; color:#2e7d32; padding:5px 12px; border-radius:10px; cursor:pointer; transition:all 0.2s; border:1px solid #e8f5e9; position:relative; }}
.up-chip:hover {{ background:#2e7d32; color:white; transform:scale(1.05); box-shadow:0 2px 8px rgba(46,125,50,0.3); z-index:10; }}

/* 词云弹窗 */
.wc-popup {{ display:none; position:fixed; z-index:9999; background:white; border-radius:12px; box-shadow:0 8px 30px rgba(0,0,0,0.2); padding:12px; width:300px; height:240px; }}
.wc-popup canvas {{ width:100%; height:100%; }}

@media(max-width:768px){{
.user-grid {{ grid-template-columns:1fr; }}
.stats-bar {{ grid-template-columns:repeat(3,1fr); }}
.info-grid {{ grid-template-columns:repeat(2,1fr); }}
.charts-grid {{ grid-template-columns:1fr; }}
}}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>🎬 B站弹幕用户画像分析</h1>
        <div class="meta">
            <strong>{title}</strong><br>
            BV: {bvid} | 播放: {video_info.get('stat',{}).get('view',0):,} | 
            弹幕: {video_info.get('stat',{}).get('danmaku',0):,} | 
            评论: {video_info.get('stat',{}).get('reply',0):,}<br>
            分析用户数: {stats['total']} | 大会员: {stats['vip_count']} | 
            刷屏用户: {stats['spam_levels'].get('高',0)+stats['spam_levels'].get('中',0)} |
            AI画像: {ai_count}
        </div>
    </div>

    <div class="stats-grid">
        <div class="stat-card"><div class="num">{stats['total']}</div><div class="label">分析用户</div></div>
        <div class="stat-card"><div class="num">{stats['vip_count']}</div><div class="label">大会员</div></div>
        <div class="stat-card"><div class="num">{sum(1 for p in profiles if p.get('level',0)>=5)}</div><div class="label">Lv.5+</div></div>
        <div class="stat-card"><div class="num">{stats['spam_levels'].get('高',0)}</div><div class="label">重度刷屏</div></div>
        <div class="stat-card"><div class="num">{stats['spam_levels'].get('中',0)}</div><div class="label">中度刷屏</div></div>
        <div class="stat-card"><div class="num">{ai_count}</div><div class="label">AI画像</div></div>
    </div>

    <div class="charts-grid">
        <div class="chart-card"><h3>用户等级分布</h3><canvas id="levelChart"></canvas></div>
        <div class="chart-card"><h3>刷屏风险分布</h3><canvas id="spamChart"></canvas></div>
        <div class="chart-card"><h3>用户标签 Top10</h3><canvas id="tagChart"></canvas></div>
    </div>

    <div class="filter-bar">
        <button class="filter-btn active" onclick="filter('all')">全部</button>
        <button class="filter-btn" onclick="filter('high-level')">Lv.5+</button>
        <button class="filter-btn" onclick="filter('vip')">大会员</button>
        <button class="filter-btn" onclick="filter('official')">认证用户</button>
        <button class="filter-btn" onclick="filter('spam')">刷屏用户</button>
        <button class="filter-btn" onclick="filter('creator')">UP主</button>
    </div>

    <div class="user-grid" id="userGrid">
        {cards_html}
    </div>

    <div id="wc-popup" class="wc-popup">
        <canvas id="wc-popup-canvas" width="276" height="216"></canvas>
    </div>

    <div style="text-align:center; padding:40px; color:#999; font-size:12px;">
        报告生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    </div>
</div>

<script>
// 词云数据
// UP主词云数据
const upWcData = {up_wc_js};

// UP主悬停词云弹窗
const popup = document.getElementById('wc-popup');
const popupCanvas = document.getElementById('wc-popup-canvas');
let activeUpId = null;

document.querySelectorAll('.up-chip').forEach(chip => {{
    chip.addEventListener('mouseenter', function(e) {{
        const upId = this.dataset.upid;
        const data = upWcData[upId];
        if (!data || data.length === 0) return;
        activeUpId = upId;

        // 定位弹窗在chip旁边
        const rect = this.getBoundingClientRect();
        popup.style.display = 'block';
        popup.style.left = Math.min(rect.left, window.innerWidth - 320) + 'px';
        popup.style.top = (rect.bottom + 8) + 'px';

        // 渲染词云
        const maxW = Math.max(...data.map(d => d[1]));
        const minW = Math.min(...data.map(d => d[1]));
        const scaled = data.map(d => [d[0], 10 + (d[1] - minW) / Math.max(maxW - minW, 1) * 50]);
        WordCloud(popupCanvas, {{
            list: scaled,
            gridSize: 10,
            weightFactor: 1,
            fontFamily: 'sans-serif',
            color: () => ['#00a1d6','#fb7299','#ff9f43','#6c5ce7','#2e7d32'][Math.floor(Math.random()*5)],
            rotateRatio: 0,
            backgroundColor: '#ffffff',
            shape: 'circle',
            clearCanvas: true,
        }});
    }});
    chip.addEventListener('mouseleave', function() {{
        popup.style.display = 'none';
        activeUpId = null;
    }});
}});

// 筛选
function filter(type) {{
    document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
    event.target.classList.add('active');
    document.querySelectorAll('.user-card').forEach(card=>{{
        let show=true;
        const level=parseInt(card.dataset.level)||0;
        const isVip=card.dataset.vip==='true';
        const spam=card.dataset.spam;
        const official=card.dataset.official==='true';
        const isCreator=parseInt(card.querySelector('.stats-bar .stat:nth-child(4) .num')?.textContent||0)>0;
        switch(type){{
            case 'all': show=true; break;
            case 'high-level': show=level>=5; break;
            case 'vip': show=isVip; break;
            case 'official': show=official; break;
            case 'spam': show=spam!=='低'; break;
            case 'creator': show=isCreator; break;
        }}
        card.style.display=show?'':'none';
    }});
}}

new Chart(document.getElementById('levelChart'),{{
    type:'bar',
    data:{{labels:{json.dumps(level_labels)},datasets:[{{label:'人数',data:{json.dumps(level_data)},backgroundColor:'#00a1d6',borderRadius:6}}]}},
    options:{{responsive:true,plugins:{{legend:{{display:false}}}}}}
}});

new Chart(document.getElementById('spamChart'),{{
    type:'doughnut',
    data:{{labels:['低风险','中风险','高风险'],datasets:[{{data:{json.dumps(spam_data)},backgroundColor:['#4caf50','#ff9800','#f44336']}}]}},
    options:{{responsive:true}}
}});

new Chart(document.getElementById('tagChart'),{{
    type:'bar',
    data:{{labels:{json.dumps(tag_labels)},datasets:[{{label:'出现次数',data:{json.dumps(tag_data)},backgroundColor:'#ff9f43',borderRadius:6}}]}},
    options:{{responsive:true,indexAxis:'y',plugins:{{legend:{{display:false}}}}}}
}});
</script>
</body>
</html>'''

    return html


def save_report(video_info: dict, profiles: list[dict]) -> str:
    """保存HTML报告到文件"""
    bvid = video_info.get("bvid", "unknown")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"report_{bvid}_{timestamp}.html"
    filepath = os.path.join(REPORT_DIR, filename)

    html = generate_html_report(video_info, profiles)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    return filepath
