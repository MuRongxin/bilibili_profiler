# TODO —— 识别不友善用户：发现的问题与改进项

目标：找出视频里谁在破坏和谐讨论环境。以下为对现有方案的评估中发现的问题与候选改进，按优先级排列。

## P0 —— 高价值、数据基础已具备

- [x] **问题评论作者未进入画像流水线** ✅ 已实现
  `main.select_problem_comment_authors`：问题评论 severity≥COMMENT_AUTHOR_MIN_SEVERITY(2) 或命中≥COMMENT_AUTHOR_MIN_HITS(2) 条的作者，凭明文 UID 以合成键 `cmt:{uid}` 直引 resolved 并落 senders 表（method="问题评论"、confidence="高"、danmaku_count=0），正常走采集/画像/深掘；卡片弹幕行为行尾标注「问题评论 N 条（最高严重度 S）」，风险排序纳入 comment_problem 维度。

- [x] **缺少攻击关系建模（谁攻击谁）** ✅ 已实现
  `web._attack_focus`：问题回复（problem 非空、parent_rpid>0、非自回、未标记误报）JOIN 父评论还原 A→B 攻击边，聚合挑事分/被攻击分；高回复评论页首渲染「⚔️ 争执焦点」双榜（挑事者 Top5 带类别 chips 与主要攻击对象、被围攻者 Top5 带主要来源，均链 /user/<uid>）。

## P1 —— 提升报告可用性

- [x] **弹幕时间线不可跳转核验** ✅ 已实现
  `_danmaku_density` 输出每桶起始秒数 starts；report.js 密度图 onClick 打开 `bilibili.com/video/{bvid}?t={秒}` 新标签页，悬停指针+tooltip 提示，图表标题注明「点击柱条跳转对应时段核验」。

- [x] **问题评论未按热度加权** ✅ 已实现
  `web._problem_comment_board`：全部问题评论按热度=点赞+回复数×COMMENT_HEAT_REPLY_WEIGHT(10) 降序取 Top PROBLEM_COMMENT_TOP_N(30)，高回复评论页首「🔥 问题评论榜」展示（昵称/类别 chip/热度明细/原文链接/误报按钮）。

## P2 —— 准确性兜底

- [x] **LLM 判定无人工纠偏机制** ✅ 已实现
  新增 `false_positive` 表（bvid+kind+target 主键，dm=弹幕内容/cmt=评论rpid）+ `POST /api/video/<bvid>/false_positive` 幂等切换；问题弹幕榜代表原文、高回复评论树根/子节点、问题评论榜均带「误报/撤销」按钮。弹幕侧按内容从 cringe 聚合重算扣除（`_apply_danmaku_fp`，旧 llm_cache 无 items 时回退 examples），用户疑似分（风险排序）随之降级；评论侧剔除出统计/榜单/攻击边、chip 划线可撤销。llm_cache 不动，标记跨重跑保留，删除报告时清除。

- [x] **mid_hash 解析率限制覆盖面** ✅ 已实现
  `main.build_video_meta_uid_map`：视频元信息明文 UID 源（UP主 owner.mid + 联合投稿 staff + 简介@提及 desc_v2 type=1 的 biz_id）并入阶段4交叉验证映射（method="视频信息"），命中时等同明文验证并沉淀全局映射库。

## 现有方案有效性评估（结论）

有效且方向正确：七类 LLM 检测 + 按发送者聚合 + 兴趣分定员的漏斗设计是合理的；`mid_hash` MITM 反查 + 明文交叉验证解决了匿名弹幕归因这个最难的环节。评论区与弹幕区两条线现已汇合（P0 两项已补），「谁不友善」的画像完整。
