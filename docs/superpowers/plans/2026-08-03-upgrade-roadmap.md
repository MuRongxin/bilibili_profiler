# B站弹幕画像系统 全面升级计划（路线图）

> **For agentic workers:** REQUIRED SUB-SKILL: 每个阶段启动时，先用 superpowers:writing-plans 为该阶段生成独立详细实施计划（含 TDD 步骤），再用 superpowers:subagent-driven-development 逐任务执行。本文件是总路线图，不直接执行。

**Goal:** 在修复既有缺陷的基础上，全面升级采集能力（全量历史弹幕、评论新接口）、性能（CRC32 彩虹表、安全并发）、报告与易用性，使系统达到可靠生产水准。

**Architecture:** 保持现有扁平模块结构与"限速/降级/不删数据"硬约定；网络层重构为线程安全 + 风控感知；存储层支撑真断点续采；采集层接入 2026-01 调研发现的新接口。

**Tech Stack:** Python 3、requests、lxml、protobuf wire 手写解析（免编译）、SQLite、Chart.js。

**关联文档：**
- 缺陷清单：`docs/code_review_2026-08-03.md`（本计划的修复项来源，行号以提交 `363418b` 为准）
- API 调研结论：见本文附录 A（2026-08-03 调研，来源为 bilibili-API-collect 归档前快照）；完整接口手册见 `docs/bilibili_api_reference.md`

---

## ⚠️ 阶段 0：合规与风险决策（先于一切）

**背景（调研实证）：** 权威 API 文档库 bilibili-API-collect 于 2026-01-28 收到 B站委托律所律师函，指控"系统性收集整理非公开 API"，仓库已永久归档删除。本项目属同类行为。

**需要用户决策的事项：**
- [ ] 确认继续开发与使用的风险自担范围（个人研究用途 vs 分发）
- [ ] 轮换 `config.py` 中已泄露的 LLM API Key（无论是否继续都必须做）
- [ ] 决定是否在 README 中加入免责声明与"仅个人学习用途"声明

**风险缓解原则（全计划通用）：** 限速只紧不松；-412 进入长冷却（≥10 分钟）而非立即重试；任何新接口先用 quick_test.py 小规模实测再全量。

---

## 阶段 1：安全与数据正确性修复

**目标：** 消除会让分析结论错误或凭证泄露的问题。对应审查报告 Critical #1/#5/#6/#8 + Important 中的数据正确性项。

**范围（文件级）：**
- `src/report.py`：统一 `html.escape`（含 `quote=True`），`json.dumps(...).replace("</", "<\\/")` 防 `</script>` 逃逸；URL 字段校验 `https?://` 前缀；修复 `**粗体**` 配对（`re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", ...)`）
- `src/config.py`：LLM Key 默认值改 `""`，仅环境变量读取
- `src/danmaku.py:49-51`：删除 `isdigit()` 分支，统一 `mid_hash = uid_raw.lower()`
- `src/uid_resolver.py`：暴力破解路径置信度上限压到"中"（弹幕数≥5 也只给"中"），结果标注 `collision_risk=True`；`report.py` 报告页面对暴力破解来源加"可能存在误识别"徽标
- `src/api_client.py:114-117`：`_sign_wbi` 开头剔除残留 `w_rid`/`wts`
- `login.py`、`login_bg.py`：扫码成功分支提取 `data.refresh_token`（对齐 `auth.py:219-221`）
- `src/auth.py`：cookie.json 原子写入（临时文件 + `os.replace`）+ `chmod 600`；`load_cookie` 捕获 `JSONDecodeError` 返回 None 走重新登录
- `src/comment.py:52,75,111`：`level_info` None 防御；CRC 碰撞保留先见者并打印冲突警告
- `src/profile_analyzer.py:206-212`：`account_age_days` 改名 `oldest_activity_days`（同步改报告与 LLM prompt 文案）

**验收：** 构造含 `<script>` 的弹幕内容跑 quick_test.py，报告中该内容显示为纯文本；`python -c` 单测 `crack_crc32` 置信度逻辑；cookie 文件权限为 600。

---

## 阶段 2：网络层重构（风控感知 + 线程安全）

**目标：** 为后续并发与全量采集打底。对应 Critical #4/#7、Important 中 api_client/auth 全部项 + 调研建议 #4/#5。

**范围：**
- `src/api_client.py` 重构：
  - 限速器加 `threading.Lock`（`_sleep_if_needed` 与请求发出必须原子），Session 使用也入锁——使客户端线程安全
  - WBI 签名按调研规范逐项修正：过滤 `!'()*`、百分号编码大写、空格 `%20`、`wts` 秒级；`img_key/sub_key` 每日缓存刷新；收到 -352/-403 + `v_voucher` 时强制刷新密钥重签
  - `get()` 把 `JSONDecodeError`/`ValueError` 纳入可重试异常；重试耗尽统一返回 `{"code": -1}` 不再 raise
  - 启动初始化：GET 主页或 `/x/frontend/finger/spi` 拿 `buvid3`/`b_nut`；申请 bili_ticket（`POST /bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket`，`hexsign=hmac_sha256(key="XgwSnGZ1p", msg="ts"+ts)`，3 天有效）
  - -412 处理改为长冷却（可配置 `RISK_COOLDOWN = 600`）而非短退避
  - UA 用完整浏览器串（不得含 `python`）、统一 `Referer: https://www.bilibili.com/`
  - `get_raw` 补重试与 `raise_for_status`
- `src/auth.py:90-158`：cookie 刷新流程改走 `BiliAPIClient`（为其补 `post()` 方法）；verify 失败时无条件先尝试一次 refresh
- `src/config.py`：新增 `RISK_COOLDOWN`、`BILI_TICKET_ENABLED` 等常量

**验收：** 单测签名输出与 wbi.md 官方示例一致；多线程压测限速器实际间隔 ≥ REQUEST_DELAY；quick_test.py 端到端通过。

---

## 阶段 3：核心承诺修复（--force / 断点续采 / 存储）

**目标：** 兑现文档宣称的行为。对应 Critical #2/#3、Important 中 main/storage 全部项。

**范围：**
- `src/main.py`：
  - `--force`：`init_db()` 后清除该 bvid 的 senders/users/progress（`storage.py` 补 `clear_video_cache(bvid)`，复用现有无人调用的 `clear_progress` 思路）
  - `--max-users` 透传到 `phase_collect_users`
  - 阶段 5 采集成功立即 `save_user_data`（先存空 profile，阶段 6 UPDATE）；循环前 `seen_uids` 去重
  - spam 检测后批量 UPDATE 回 senders 表
  - uid=None 的失败 sender 纳入重试（评论会随时间变好）
  - `phase_analyze` 循环加逐人 try/except
  - `aid=0` 短路评论采集
- `src/storage.py`：连接管理改 `contextlib.closing`；删除或启用 progress 死代码（决策：保留 senders/users 缓存作为唯一续采机制，删除 progress 表逻辑，README 注明）
- `src/profile_analyzer.py:235-236`：`play` 等数值字段采集时强转 int（`user_collector.py` 侧）

**验收：** 跑到阶段 5 中途 Ctrl+C，重跑后已采集用户全部命中缓存；`--force` 后缓存全清重新采集；`--max-users 10` 各阶段口径一致。

---

## 阶段 4：性能提速

**目标：** CRC32 破解从 75 秒/人降到毫秒级；采集总耗时显著下降。对应 Important 中性能项 + 调研建议 #3。

**范围：**
- `src/uid_resolver.py` + 新 `src/crc_rainbow.py`：
  - 彩虹表方案：一次性预生成 UID 0–5000 万的 `crc32 → uid` 映射落盘（`data/crc_table.bin`，定长二进制，约 400MB 内；生成可用 numpy 向量化查表法，构建时间分钟级）
  - 查询时内存映射（`mmap`）O(1) 反查；**碰撞处理**：同一 CRC 多 UID 时返回全部候选，标记歧义，置信度"低"
  - >10 位 UID（16 位新号）直接标记不可破跳过（调研实证无法反推）
  - 表不存在时自动触发一次性构建（带进度显示）
- `src/user_collector.py` / `src/up_analyzer.py` 并发化：
  - 删除 `up_analyzer.py:151` 裸 `ThreadPoolExecutor`；改为受阶段 2 线程安全客户端保护的有限并发（`max_workers=3`，常量 `COLLECT_WORKERS` 入 config）
  - `summarize_followings` 传 `sample_size`（见阶段 5 的关注列表 100 上限适配）
- `src/spam_detector.py:36-41`：相似度先 `Counter` 去重，只对唯一内容两两比较

**验收：** 彩虹表构建后单个 hash 反查 <10ms；50 个未命中发送者的解析阶段从 1 小时+ 降到秒级；quick_test.py 总耗时对比记录。

---

## 阶段 5：采集能力增强（调研成果落地）

**目标：** 弹幕覆盖从"最近几千条"到"视频全周期"；UID 解析率与画像维度提升。**每项先单接口实测再集成。**

**5.1 全量历史弹幕（最大能力提升，调研建议 #1）**
- 新 `src/danmaku_history.py`：
  - `GET /x/v2/dm/history/index?type=1&oid={cid}&month=YYYY-MM` 拿有弹幕日期（需登录）
  - `GET /x/v2/dm/web/history/seg.so?type=1&oid={cid}&date=YYYY-MM-DD` 逐日拉 protobuf（需 SESSDATA）
  - protobuf wire 手写解析（`DmSegMobileReply`，免 protoc；字段：elems 重复嵌套，含 id/midHash/content/ctime/weight）
  - 与实时池按 dmid 合并去重；`config.py` 加 `HISTORY_DANMAKU_ENABLED = True`、`HISTORY_MAX_DAYS` 上限
- `main.py` 阶段 2 改为：实时池 + 历史弹幕合并
- 报告标注覆盖率（历史占比）

**5.2 评论新接口 + 子评论补采（调研建议 #2/#6）**
- `src/comment.py`：
  - 主列表换 `/x/v2/reply/wbi/main` + `pagination_str.offset` 游标（`data.cursor.pagination_reply.next_offset`，`is_end` 终止）
  - 子评论对 `reply_count > len(replies)` 的主评论调 `/x/v2/reply/reply` 按 `pn` 翻完（每页实际只回 20 条，用 `page.count` 终止；走限速）
  - 提取 `reply_control.location` IP 属地入交叉验证数据 → 画像新增地域维度

**5.3 用户空间接口现代化（调研建议 #6）**
- `src/user_collector.py`：
  - 批量打底：`x/polymer/pc-electron/v1/user/cards?uids=...`（50 人/请求，无需 wbi）拿昵称/头像/认证，仅深度用户再调 `acc/info`
  - `acc/info` 改 `x/space/wbi/acc/info`（wbi + Cookie ≥3 项）
  - 投稿列表换 `x/series/recArchivesByKeywords`（keywords 空取全部，ps=100，文档注明暂无风控）
  - 收藏夹改 `x/v3/fav/resource/list`
  - **关注列表适配 100 上限**：他人 `relation/followings` 仅能看前 100（调研实证），`MAX_FOLLOWING_PAGES` 从 50 改为 5，文案与画像逻辑同步；`up_analyzer` 的 sample_size 上限随之收敛（≤20）
- 动态接口确认现行 `x/polymer/web-dynamic/v1/feed/space`（登录只需 SESSDATA）

**验收：** 选一个发布超 1 年的高弹幕视频，历史弹幕量 ≫ 实时池；子评论采集后 UID→CRC32 映射数显著提升；quick_test.py 全绿。

---

## 阶段 6：报告、导出与易用性

**范围：**
- `src/report.py`：修复 CSS 缺空格（`:263/277/294-301`）、`filter()` 改为传 `this`；新增地域分布图（阶段 5.2 数据）、历史弹幕覆盖率展示、CRC 碰撞风险徽标
- 新 `src/exporter.py`：导出 CSV（发送者汇总）/ JSON（完整画像数据），输出到 `data/reports/`
- `src/main.py`：进度显示（tqdm 或手写简易进度，依赖确认为准——tqdm 不在 requirements.txt 则手写）
- `run.py`：支持批量 BV 号（`--batch file.txt`，逐视频串行，共享登录态与客户端）
- `quick_test.py`：argparse 化；修评论静默失败、Key 判断与主流程对齐、采集错误检查（审查 Important 项）
- `README.md`：更新能力说明（历史弹幕覆盖、地域维度、合规声明）

---

## 阶段 7：Minor 清扫

审查报告 Minor 全部项（死代码、未用导入、类型不一致、时区固定东八区等），一次提交前逐项核对划掉。

---

## 执行顺序与依赖

```
阶段 0（用户决策）
  └─ 阶段 1（安全正确性，独立）
       └─ 阶段 2（网络层，后续一切的地基）
            ├─ 阶段 3（核心承诺）
            ├─ 阶段 4（性能，依赖阶段2的线程安全客户端）
            └─ 阶段 5（采集增强，依赖阶段2的wbi/风控）─→ 阶段 6（报告易用性，依赖5的新数据）
                                                          └─ 阶段 7（清扫）
```

每阶段完成后：跑 `quick_test.py` 冒烟 + 提交一个 commit（ conventional commits 中文消息）。

---

## 附录 A：API 调研要点（2026-08-03）

| 发现 | 状态 | 用途 |
|---|---|---|
| `/x/v2/dm/history/index` + `/x/v2/dm/web/history/seg.so` | 已证实（文档） | 全量历史弹幕，需登录 |
| 实时 protobuf 池上限约为 XML 池 2 倍，每 6 分钟一包 ≤6000 条 | 已证实 | 实时采集可升级 |
| mid_hash 仍是 CRC32(mid)，无官方反查接口 | 已证实 | 现有破解路线继续有效 |
| 16 位新 UID 无法暴力反推 | 未验证（数学上合理） | 直接跳过标记 |
| wbi 规范：过滤 `!'()*`、编码大写、空格 %20、密钥每日更替 | 已证实 | 阶段 2 修正 |
| 评论主列表已迁 `/x/v2/reply/wbi/main`（cursor 游标） | 已证实 | 阶段 5.2 |
| 子评论 `/x/v2/reply/reply` 仍 pn/ps 分页，每页实际 20 条 | 已证实 | 阶段 5.2 |
| 评论返回 `reply_control.location` IP 属地 | 已证实 | 地域画像维度 |
| `x/polymer/pc-electron/v1/user/cards` 批量名片 50 人/请求免 wbi | 已证实 | 阶段 5.3 降请求量 |
| `x/series/recArchivesByKeywords` 暂无风控校验 | 已证实（文档原话） | 替代 arc/search |
| `x/space/wbi/acc/info` 需 wbi + Cookie ≥3 项 | 已证实 | 阶段 5.3 |
| `x/space/wbi/arc/search` 需 dm_img_* 指纹参数 | 已证实 | 规避，改用上一行接口 |
| 他人关注列表仅能看前 100 | 已证实 | MAX_FOLLOWING_PAGES 50→5 |
| buvid3/bili_ticket 可降低风控概率 | 已证实（文档） | 阶段 2 |
| exClimbWuzhi 设备指纹激活 | 传闻/复杂 | 不做，仅记录 |

**未实测声明：** 以上均来自 2026-01 归档前社区文档，未发实网验证请求；每个接口集成前必须先小规模实测。
