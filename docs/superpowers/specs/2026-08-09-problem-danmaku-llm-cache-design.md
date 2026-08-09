# 问题弹幕检测扩展 + LLM 成本优化 设计文档

日期：2026-08-09
状态：已获用户批准

## 1. 背景与问题

当前选人标准为"刷屏中/高 或 尬语 ≥1 条"，在热门二游视频（BV1wZMy6DE31，池内 3962 条弹幕、数千发送者）上只命中 25 人、报告仅 14 人。根因：刷屏检测只能抓"一个人发很多条"的人，而热门视频里问题用户更多是"单条恶评/引战"，spam_score 恒为 0 永不入名单。

同时 LLM 开销随命中人数线性上涨（7a 全员粗筛是 token 大头），且重跑同一视频时全部 LLM 调用重复花钱。用户对 token 成本敏感。

## 2. 决策（用户已确认）

1. **扩展 LLM 判定类别**：在现有尬语检测的同一批 LLM 调用中增加判定类别，token 不增。
2. **命中者全员深度采集，上限兜底**：沿用 `MAX_ANALYZE_USERS_HARD_CAP = 300`。
3. **省 token**：LLM 结果落库缓存 + 砍掉 7a 全员粗筛；不做 prompt 输入瘦身。

## 3. 问题弹幕检测扩展（cringe_detector.py）

### 3.1 判定类别从 3 类扩到 7 类

现有：中二抒情、尬夸捧杀、引战阴阳
新增：

- **人身攻击**：辱骂、诅咒、攻击其他观众/UP主/角色
- **恶意剧透**：泄露剧情关键信息
- **广告引流**：打广告、引流、推广
- **键政敏感**：政治隐喻、借题发挥的键政引战

常量 `CRINGE_CATEGORIES` 扩展为 7 类并**更名为 `PROBLEM_CATEGORIES`**（该常量仅 `cringe_detector.py` 内部引用，改名影响面小）；函数名 `detect_cringe_danmaku`、文件名 `cringe_detector.py` 保持不变（被 `main.py`、`quick_test.py` 引用，最小改动），docstring 与打印文案升级为"问题弹幕"。聚合 dict 的键名 `categories`/`examples` 等不变（下游 main.py/report.py 按 `cringe` 键消费的结构不动）。

### 3.2 不变的部分

- 判定方式：去重弹幕按 `CRINGE_BATCH_SIZE = 200` 分批喂 LLM，同一批调用判全部类别，**token 开销与现状持平**。
- 判定原则："宁漏勿冤"，正常玩梗、合理讨论、普通应援不误伤。
- 聚合输出结构不变：`{mid_hash: {count, max_severity, categories, examples}}`。
- 降级策略：未配置 Key / 全部批次失败 → 返回空 dict，不中断流水线。

### 3.3 选人规则不变

`main.py` 阶段4 兴趣命中条件保持：`spam_level ∈ {高, 中}` 或 问题弹幕 `count ≥ 1`，全部进解析名单，`MAX_ANALYZE_USERS_HARD_CAP = 300` 兜底截断。类别扩展后命中人数预期从几十涨到上百，属预期行为。

## 4. LLM 结果落库缓存（storage.py + 新表）

### 4.1 新表 `llm_cache`

```sql
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key   TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
```

### 4.2 两类缓存

| 用途 | cache_key 格式 | 命中条件 |
|---|---|---|
| 问题弹幕判定 | `cringe:{bvid}:{去重内容列表sha256[:16]}` | 重跑同视频且去重弹幕集合未变 |
| 重点深掘 | `deep:{uid}:{证据包sha256[:16]}` | 该用户证据包（弹幕/评论/尬语/四维摘要）未变 |

- 命中 → 直接使用缓存结果，**零 LLM 调用**；未命中 → 正常调用并写回缓存。
- 弹幕池进新弹幕导致集合变化 → 问题弹幕判定全量重判（v1 不做增量合并）。
- 缓存读写异常一律捕获忽略（视为未命中），不得中断流水线。
- `clear_video_cache(bvid)`（--force 路径）同步清除 `cringe:{bvid}:%` 条目；深掘缓存按 uid 不清（用户数据没变重跑仍有效）。

### 4.3 缓存 key 的 hash 计算

- 问题弹幕：对去重排序后的内容列表 JSON 做 sha256，取前 16 位十六进制。
- 深掘：对 `_build_deep_prompt` 的证据包 dict JSON 做同样处理（在构建 prompt 前算 hash，prompt 构建逻辑不变）。

## 5. 砍掉 7a 全员粗筛（llm_analyzer.py + main.py + report.py）

- `LLMAnalyzer` 删除批量粗筛：`analyze()`、`_build_prompt()`、`_parse_per_user()`、`_analyze_batch()`，只保留深掘（`analyze_deep()`、`_build_deep_prompt()`、`_analyze_one_deep()`）。
- `main.py` `phase_ai_analysis` 只执行 7b 深掘，阶段标题与日志相应更新（不再有 7a/7b 之分，直接叫"LLM 重点深掘"）。
- 报告：普通用户卡片只有规则标签（`profile_analyzer.py` 生成的 `tags`：新用户/硬核用户/重度刷屏/晚间活跃/游戏深度爱好者等），无 AI 一句话定性区块；重点人员（深掘 top K）保留完整 AI 四节画像（行为定性/动机分析/证据引用/风险等级）。`report.py` 对缺失 `ai_analysis` 的 profile 不渲染该区块（现有渲染逻辑若已有判空则无需改动，实现时核实）。
- `LLM_DEEP_TOP_K` 保留不变。

## 6. 报告措辞调整（report.py）

- "尬语榜"更名为"问题弹幕榜"，7 个类别分色标签展示。
- 用户卡片上尬语相关字段（`cringe`）结构不变，仅展示文案更名。
- 其余结构（风险排序 高→中→低、评论小节、投稿超链接）不动。

## 7. 错误处理

沿用项目铁律：LLM 全部失败降级为空不中断；单用户失败跳过；缓存读写失败忽略。限速仍由 `BiliAPIClient` 保证，LLM 调用不受 B站限速约束但保留深掘的 20 秒退避重试。

## 8. 验证方式

无单元测试框架，实跑验证（需有效 Cookie 与 DeepSeek Key）：

1. `python run.py BV1wZMy6DE31` 第一轮：确认问题弹幕判定覆盖 7 类、兴趣命中人数明显上升、采集与深掘正常、报告"问题弹幕榜"正确渲染。
2. 紧接着第二轮（不加 --force）：确认日志显示缓存命中、LLM 调用次数为零（问题弹幕判定与深掘均命中缓存）。
3. `python quick_test.py` 冒烟通过。

## 9. 明确不做（YAGNI）

- prompt 输入瘦身（用户明确不要）。
- 问题弹幕判定的增量合并（池新增内容时全量重判，v1 不做 diff）。
- 关键词/正则本地预筛（LLM 判定已能覆盖，规则词表维护成本高）。
- cringe_detector.py 文件改名（最小改动，仅内部常量与文案升级）。
- 16 位随机长 UID 解析、弹幕池截断（平台硬限制，本设计不碰）。
