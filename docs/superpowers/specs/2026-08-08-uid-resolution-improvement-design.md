# UID 破解能力改进设计（mid_hash → UID）

日期：2026-08-08

我认为，如果一个用户在弹幕里有所作为，特别是刷屏，那么，ta在评论区留下痕迹的可能性是很高的；
我觉得可以采集全部评论，获得这些评论者的UID，计算之后，然后去比对弹幕发送者，这应该会比暴力破解要快；而且，我们这样就给评论现状分析打好了基础；

## 1. 背景与现状

弹幕中的发送者标识 `midHash = CRC32(str(mid))`（8 位小写 hex）。当前 `src/uid_resolver.py` 提供两条破解路径：

1. **评论区交叉验证**：采集评论拿明文 UID，算 CRC32 与 midHash 比对（最可靠，但只覆盖同时发了评论的发送者）；
2. **CRC32 彩虹表反查**：`src/crc_rainbow.py`，仅覆盖 UID < 5000 万（约 2017 年前注册用户），超出范围直接返回未命中。

调研结论（2026-08-08，详见本文件第 8 节）：

- 社区全部反查手段收敛为两类：CRC32 反查 + 明文 UID 交叉验证，我们没有漏掉任何已知手段；
- **MoePus MITM（中间相遇）算法**只需约 10 万条小表即可秒级反查任意 ≤10 位数字串（esterTion/BiliBili_crc2mid、Aruelius/crc32-crack、bilibili-danmaku-tracker 均用此算法），无需 400MB 彩虹表；
- B站顺序 UID 已分配到 20 多亿（10 位），2022 年 4 月前注册用户基本全覆盖——当前 5000 万上限漏掉了 2017–2022 年注册的海量活跃老用户；
- 16 位 UID 反查、新泄露接口、UP 主后台接口、文本匹配归因均为死路（见第 9 节）。

## 2. 目标

把"未识别发送者"比例降到最低，按 ROI 排序实施三件事：

- **P0 MITM 反查全覆盖**：反查覆盖从 UID<5000 万扩到全部 ≤10 位 UID，配套自动碰撞消歧；
- **P1 全局 mid_hash→UID 映射库**：每次分析沉淀的映射跨视频复用，覆盖率随使用次数累积；
- **P2 新增明文 UID 源**：互动弹幕 commandDms（UP 主 mid）+ 视频充电鸣谢名单（pay_mid）；
- **P3 评论采集扩容**：评论是量最大的明文 UID 源，主评论与子评论采集上限同步放宽（见第 6 节）。

非目标：16 位 UID 反查、文本匹配归因、依赖第三方存档站（aicu 等）。

## 3. P0：MITM 反查引擎 + 碰撞消歧

### 3.1 算法

CRC32 在 GF(2) 上是仿射变换，可将 UID 十进制串拆为"前缀 + 5 位定长后缀"两段做中间相遇：

- 预计算全部 100000 个 5 位后缀（`00000`–`99999`）的 CRC 贡献及逆变换表，常驻内存仅几 MB，首次查询时惰性构建（秒级），无需落盘大表；
- 查询时对目标 hash 枚举前缀（≤10 位即最多 99999 个前缀），反推所需后缀的 CRC 并查表，命中即得一个候选 UID；
- 单次查询返回**全部候选**（≤10 位空间每个 CRC 平均约 2.3 个候选），耗时秒级。

### 3.2 模块改动

- `src/crc_rainbow.py`：**整体替换**为 MITM 实现（文件名保留或改为 `crc_mitm.py`，以 import 面最小改动为准）。对外接口保持 `lookup(crc32_hash) -> list[int]`（返回全部候选）与 `build_table`/`table_exists` 的语义由新实现消化：
  - 不再需要 `build_table` 长流程与 `data/crc_table.bin`；保留一个内存表惰性构建入口；
  - `_crack_crc32_fallback`（uid_resolver.py 内旧增量搜索）删除，MITM 即唯一反查路径；
- `src/config.py`：彩虹表相关常量 `CRC_TABLE_MAX_UID` / `CRC_BUILD_CHUNK` / `CRC_TABLE_PATH` 及旧搜索上限 `CRC32_MAX_SEARCH` / `CRC32_OLD_MAX` 全部移除，新增 `MITM_MAX_UID = 10_000_000_000`（10 位上限）；`uid_resolver.crack_crc32` 的 `max_search` 参数随之改由 `MITM_MAX_UID` 承担语义；
- 本地已构建的 `data/crc_table.bin`（约 381MB，gitignored）不再被引用，由用户自行手动删除，代码不主动删数据文件。

### 3.3 消歧流水线（`resolve_sender` 改造）

候选不止一个时必须消歧，优先级：

1. **评论区 UID 交集**：候选 ∩ 评论明文 UID 集合唯一 → 方法记 `评论区验证`，置信度沿用现有规则（高/中）；
2. 交集为空 → 逐个 `verify_uid_exists` 过滤不存在者：
   - 恰好 1 个存在 → 方法 `CRC32破解`，置信度上限 **中**（沿用现有压置信度规则），`collision_risk=True`；
   - 多个存在 → 取**最小 UID**（注册更早、更可能活跃），方法 `CRC32破解`，置信度 **低**，`collision_risk=True`，并在 user_info 之外记录全部候选供报告展示；
   - 全部不存在 → 方法 `CRC32碰撞`，uid=None（沿用现状）。

`resolve_all_senders` 批处理逻辑不变；`--force` 语义不变。

### 3.4 性能与风控

- MITM 计算为纯 CPU，秒级/人，远快于旧降级路径（75 秒/人）；
- 验证请求走 `BiliAPIClient`（限速硬约束不变）；候选数通常 ≤3，验证开销可控；
- 10 位 UID 覆盖扩大后，验证请求量会上升，属预期内，由现有 0.6s 限速消化。

## 4. P1：全局 mid_hash→UID 映射库

mid_hash 与视频无关（就是 `CRC32(mid)`），因此映射可全局复用。

### 4.1 存储

`src/storage.py` 新增全局表（不属于任何 bvid）：

```sql
CREATE TABLE IF NOT EXISTS global_uid_map (
    mid_hash   TEXT PRIMARY KEY,   -- 8 位小写 hex
    uid        INTEGER NOT NULL,
    source     TEXT NOT NULL,      -- 评论区验证 / CRC32破解 / 充电名单 / 互动弹幕
    first_seen TEXT NOT NULL,      -- ISO 时间
    last_seen  TEXT NOT NULL,
    hit_count  INTEGER NOT NULL DEFAULT 1
);
```

### 4.2 读写时机

- **写**：`resolve_all_senders` 中每个成功解析（评论区验证、MITM 破解且候选唯一）+ 充电名单/互动弹幕交叉命中，立即 upsert（`hit_count+1`, 刷新 `last_seen`）；
- **读**：`phase_resolve` 开始时一次性把全局表读入内存 dict，与当视频 `comment_uid_map` 合并（**当视频评论验证优先**，全局库兜底）；命中全局库且 `verify_uid_exists` 通过 → 方法沿用库中 `source`，置信度按现有弹幕数规则；
- 全局库中带 `collision_risk` 历史（多候选取最小者）的条目**不入库**，只沉淀可靠映射。

### 4.3 边界

- 表只增不删；uid 注销后 `verify_uid_exists` 失败时该条目降级跳过（不删除，沿用"不删除数据"约定）；
- 跨视频累积无上限问题（百万级条目也就几十 MB）。

## 5. P2：新增明文 UID 源

### 5.1 互动弹幕 commandDms（UP 主）

- 接口：`GET https://api.bilibili.com/x/v2/dm/web/view?type=1&oid={cid}&pid={aid}`（protobuf，普通登录 Cookie 可用）；
- `commandDms` 中 UP 主头像弹幕（`#UP#`）、关联视频（`#LINK#`）、引导关注（`#ATTENTION#`）条目含**明文 `mid`**；
- 实现：`src/danmaku.py` 或新函数解析 view 包（项目已有 protobuf wire 手写解析先例，见 `danmaku_history.py`），提取 `mid` + 内容，并入 `up_analyzer`；同时把 `CRC32(mid)` 写入当视频交叉验证映射与全局库（source=`互动弹幕`）；
- 失败降级：接口异常仅打印警告，不影响主流程。

### 5.2 视频充电鸣谢名单

- 接口：充电榜接口返回 `pay_mid`（明文）、`uname`、`rank`（普通登录可用，文档见 bilibili-API-collect `electric/charge_list.md`）；
- 实现：`src/comment.py` 旁新增 `fetch_charge_uid_map(aid, client)`，对每个 `pay_mid` 算 CRC32 产出 `crc32→uid` 映射，与评论映射合并传入 `resolve_sender`（评论映射优先）；命中后方法记 `充电名单`，置信度按弹幕数规则（名单本身即明文证据，置信度同评论验证），同步写全局库；
- 充电用户是重度粉丝，与弹幕发送者重合率高；无充电数据的视频返回空映射，零成本降级。

### 5.3 合并优先级

当视频交叉验证映射的构建顺序（后者不覆盖前者）：

1. 评论区明文 UID（量最大）
2. 充电名单
3. 互动弹幕（基本只有 UP 主）

## 6. P3：评论采集扩容

设计动机（见本文档开头）：刷屏者在评论区留下痕迹的概率很高，评论是量最大、置信度最高的明文 UID 源，当前的采集上限把这条最重要的路径截断了。

### 6.1 改动

- `src/config.py`：
  - `MAX_COMMENT_PAGES`：20 → **100**（主评论约 20 条/页，上限从 ~400 条提升到 ~2000 条）；
  - `COMMENT_REPLY_MAX_PAGES`：5 → **25**（每条主评论的子评论补采上限从 100 条提升到 500 条）；
- `src/comment.py` 翻页/补采逻辑本身不变（wbi/main 游标 + reply/reply 分页均已实现真重复页终止与截断保护），仅默认值变化；
- 采集日志补充"已采 X / 评论区总 Y"的覆盖提示（复用阶段 5/6 的覆盖率口径），被上限截断时明确打印。

### 6.2 成本与风控

- 100 页 × 0.6–1.0s 限速 ≈ 多花 1–2 分钟/视频，子评论补采增量视热度而定；全部走 `BiliAPIClient`，限速硬约束不变；
- 评论少的视频行为与现状完全一致（提前翻页终止，不产生多余请求）。

## 7. 验证方式

- `python quick_test.py <BV号> --top 5`：选一个 2018–2021 年活跃 UP 的老视频（发送者含大量 8–9 位 UID），对比改进前后"已解析/总数"比例；
- 控制台抽查若干 MITM 命中的 `CRC32破解` 结果，人工开空间页核对昵称与弹幕内容是否吻合；
- 全局库生效验证：同一 UP 主的第二个视频，首阶段即应有来自全局库的命中（日志中标注 source）；
- 充电名单/互动弹幕：选有充电数据、有 UP 主互动弹幕的视频各验证一次；
- P3 验证：选评论区总量 >1000 的视频，确认主评论采集突破原 20 页上限、日志打印覆盖率。

## 8. 调研依据（2026-08-08）

- MITM 算法：[esterTion/BiliBili_crc2mid](https://github.com/esterTion/BiliBili_crc2mid)、[Aruelius/crc32-crack](https://github.com/Aruelius/crc32-crack)、bilibili-danmaku-tracker 油猴脚本（greasyfork 434334，源码核实，GitHub 仓库已 404）；
- 10 位覆盖实证：[cwuom/GetDanmuSender](https://github.com/cwuom/GetDanmuSender) README（"8、9 位 UID 基本正确，16 位及超 10 位无法反推"）；
- 最大公开彩虹表 6 亿档：[Bilispeed](https://github.com/whte97284-hue/Bilispeed)（4.8GB/90–120 分钟构建，仍不如 MITM）；
- 16 位 UID 区间结构（首区间 3461562035603456，步长 2^21）：B站动态 opus/1105286932906115089、cv40294215（快照）——压空间后碰撞噪声仍 ~15 候选/hash，不可用；
- commandDms 明文 mid：bilibili-API-collect `docs/danmaku/danmaku_view_proto.md`（已验证）；
- 充电名单 `pay_mid`：bilibili-API-collect `docs/electric/charge_list.md`（已验证）；
- API-collect 及其主要 fork 已于 2026-01-28 归档只读，2024–2026 无任何新泄露接口。

## 9. 明确不做的事（死路清单）

- 16 位 UID 反查（碰撞噪声不可消，社区无人做成）；
- 寻找/等待泄露明文 UID 的新弹幕接口（2024–2026 无迹象）；
- UP 主后台弹幕管理接口（仅限自己稿件）；
- 弹幕↔评论文本匹配归因（无公开先例，短文本误判不可控）；
- 依赖 aicu.cc 等第三方存档站（闭源、CF 盾、无公开 API）。
