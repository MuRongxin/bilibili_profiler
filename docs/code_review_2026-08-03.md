# 深度代码审查报告（2026-08-03）

> 审查范围：全量源码（src/ 15 个模块 + 4 个根目录脚本，共约 3736 行），5 路并行审查，含实证验证（CRC32 碰撞实测、暴力破解耗时实测）。
> 状态：**暂缓修复** —— 先进行功能改进，之后再处理本报告的 bug。

## 总体评价

模块划分清晰、参数化 SQL 无注入、"降级而非中断"约定在多数路径落实、限速带随机抖动、cookie 自动刷新设计是亮点。但存在一批会让核心承诺失效的真 bug，以及一个教科书级的安全漏洞面。

---

## Critical（必须修复）

### 1. 报告存储型 XSS —— `src/report.py` 全文几乎不转义
- 弹幕内容（`:44`）、用户名/签名（`:143/149`）、头像 URL（`:135`）、`title="{tip}"` 属性注入（`:94/98`）、视频标题（`:255/364`）全部原样插入 HTML。
- 词云 JSON 内嵌 `<script>` 可被 `</script>` 截断逃逸（`:247/414`）。
- 报告打开者与数据生产者不是同一主体，被分析用户发一条恶意弹幕即可在查看者浏览器执行任意 JS。
- **修复**：统一 `html.escape` + `json.dumps(...).replace("</", "<\\/")`。

### 2. `--force` 完全不生效 —— `src/main.py:247-252`
`force=True` 只跳过一行提示打印，sender 缓存（`:64`）和 user_data 缓存（`:167`）照常命中。`storage.py:242` 的 `clear_progress` 存在但无人调用。
**修复**：force 时清除该 bvid 的缓存记录，并在阶段5跳过 `has_user_data` 检查。

### 3. "断点续采"名不副实 —— `src/main.py:163-186`
阶段5（最慢阶段）采集结果只存内存，直到阶段6才写库（`:213`）。Ctrl+C 时控制台还承诺"进度已保存"，实际全部丢失。
**修复**：采集成功后立即 `save_user_data`。

### 4. WBI 密钥过期重签名是死代码 —— `src/api_client.py:114-117`
重签名时 params 里残留旧 `w_rid`，被拼进待签名串，新签名必然无效。长任务中途 WBI key 过期后所有 wbi 接口持续失败。
**修复**：`_sign_wbi` 开头剔除 `w_rid`/`wts`。

### 5. 纯数字 mid_hash 被静默损毁 —— `src/danmaku.py:49-51`
约 2.3% 的 CRC32 hash 恰好全是十进制数字，`isdigit()` 分支把它们当成明文 UID 转码，对应发送者永远无法解析。
**修复**：删除该分支，统一按 hex 处理。

### 6. CRC32 暴力破解可能返回碰撞 UID 当真实用户 —— `src/uid_resolver.py:80-86`
实测：`calc_crc32(1)` 破解返回 1146140827 而非 1。碰撞 UID 往往真实存在，`verify_uid_exists` 挡不住，无关用户会被画像且弹幕数 ≥5 时还标"高"置信度。
**修复**：暴力路径置信度上限压到中/低，报告标注碰撞风险。

### 7. 并发击穿限速 + 请求量爆炸 —— `src/up_analyzer.py:151`、`src/user_collector.py:416-418`
5 线程共享非线程安全的 `BiliAPIClient`，限速失效且是突发模式（最易触发 -412 风控）；且默认分析全部关注列表，单个画像用户最多额外 2000 次请求。
**修复**：串行化或加锁；`summarize_followings` 传 `sample_size` 上限并进 config。

### 8. LLM API Key 硬编码 —— `src/config.py:67`
明文 Key 在源码里即泄露面，应视为已泄露。
**修复**：默认值改为 `""`，并轮换该密钥。

### 9. `login.py` / `login_bg.py` 不保存 `refresh_token`
只有主流程提取 refresh_token，经这两个脚本登录的用户 cookie 过期后必须重新扫码，自动刷新成为摆设。
**修复**：扫码成功分支补充提取 `data.refresh_token`。

---

## Important（应该修复）

- `main.py:277`：`--max-users` 未透传到阶段5，参数被静默截断。
- `main.py:98-99`：spam 检测结果从不回写数据库，库里全是初始脏值。
- `main.py:64-72`：解析失败（uid=None）的 sender 被永久缓存，评论变多后也永不重试。
- `main.py:247-300`：progress 表形同虚设，中断时根本不写，加载的进度也不参与跳过逻辑。
- `api_client.py:106-119`：`resp.json()` 的 `JSONDecodeError` 未捕获，风控返回 HTML 错误页时异常可穿透调用方中断流水线；最后一次重试的网络异常直接 raise。
- `api_client.py:54-62`：WBI 签名未做 URL 编码和 `!'()*` 过滤，埋雷。
- `auth.py:63-72`：cookie.json 写入非原子、权限 644、损坏后 `load_cookie` 直接崩溃。应 `os.replace` + `chmod 600` + 容错。
- `auth.py:90-158`：cookie 刷新绕过 `BiliAPIClient`，无限速无重试，违反硬约定。
- `auth.py:193`：cookie 完全失效时不尝试 refresh 直接要求扫码。
- `danmaku.py:74-95`：`get_raw` 无重试无状态检查，单分P失败炸掉整条流水线。
- `uid_resolver.py:236-240`：`resolve_all_senders` 无逐人异常隔离，单人异常丢失全部已解析结果。
- `uid_resolver.py:80`：暴力破解实测约 75 秒/人，50 个未命中者要 1 小时以上且无进度提示。可用 `zlib.crc32` 向量化预筛提速 10 倍以上。
- `comment.py:62`：子评论只采预览 3 条，`COMMENT_REPLY_URL` 是死导入，削弱了最可靠的交叉验证手段。
- `uid_resolver.py:116-117`：网络异常被吞成"用户不存在"，交叉验证结果被无谓丢弃并跌入 75 秒级暴力破解。
- `llm_analyzer.py:120`：`max_completion_tokens` 兼容性差（多数兼容端点只认 `max_tokens`）；`:134` 单批失败丢弃全部已完成批次；`:126` `message.content` 可为 None。
- `user_collector.py:389`：收藏夹内容采集无 try/except，异常会导致该用户全部数据被丢弃。
- `main.py:209` × `profile_analyzer.py:235-236`：`analyze_profile` 无 try/except，`play` 字段为 `"--"` 字符串时 TypeError 炸掉整个阶段6。
- `profile_analyzer.py:206-212`：`account_age_days` 实为"采样到最早动态的年龄"，名不副实会误导 LLM 推断。
- `report.py:122`：`**粗体**` 只处理第一对，奇数个 `**` 导致后续全文变粗。
- `report.py:39`：弹幕内容与时间戳 `zip` 按短者截断，静默丢弹幕。
- `quick_test.py:62`：评论失败静默吞掉；`:90` 只看 env 不认 config 的 Key；`:83` 不检查采集错误返回——冒烟入口与主流程行为分叉。

---

## Minor（可选改进）

- `main.py:163-184`：同一 uid 被多个 mid_hash 命中时重复采集（可先 `seen_uids` 去重）。
- `main.py:9-15`：未使用的导入（`os`、`time`、`get_resolved_uids`）。
- `main.py:259-262`：`aid=0` 时仍发起评论采集。
- `storage.py`：连接无 try/finally，建议 `contextlib.closing`。
- `spam_detector.py:36-41`：两两相似度 O(n²)，刷屏用户上千条弹幕会卡数分钟；`detect_bot_pattern` 的 `video_times` 参数从未使用。
- `danmaku.py:174`：`numeric_uids` 死代码且 and/or 优先级错误；`:36` XML 解析未显式禁用实体（XXE 防御）。
- `uid_resolver.py:191`：`unique_contents` 计算后未使用。
- `comment.py:52/75`：`level_info` 为 None 时 AttributeError；`:111` CRC 碰撞静默覆盖。
- `report.py:263/277/294-301`：多处 CSS 缺空格声明无效；`:459` `filter()` 依赖隐式全局 `event`。
- `user_collector.py:287`：硬编码追番 URL（config 已有未使用的 `USER_BANGUMI_URL`）；`:409-411` `followers` 成功是 dict 失败是 list 类型不一致；`:320` 活跃时段依赖部署机器时区，应固定东八区。
- `up_analyzer.py:114`：失效条件造成的侥幸正确；`:7,89` `import time` 重复。
- `llm_analyzer.py:51`：弹幕原文未截断易超上下文；`:91-106` 解析失败静默无日志；`:21` 冗余 env 回退。
- `api_client.py:25`：客户端非线程安全，docstring 应注明；`:141` `get_raw` 无重试；`-412/-403` 分支退避风格不统一。
- `quick_test.py:22-31`：手撸 argv 解析脆弱，建议照搬 argparse。
- `login.py:62-96`：按 Enter 后只轮询两次，体验差，建议复用 `auth.login_by_qrcode()`。
- 设计取舍提醒：实时弹幕池单 cid 上限约数千条，历史分段弹幕未采集，建议在报告/README 明示覆盖率上限。

---

## 建议的修复顺序（待功能改进完成后执行）

1. **安全先行**：report.py 转义（#1）+ 轮换 LLM Key（#8）
2. **数据正确性**：纯数字 hash（#5）、CRC32 碰撞置信度（#6）、WBI 重签名（#4）
3. **核心承诺**：--force（#2）、断点续采（#3）、up_analyzer 并发（#7）
4. Important 批次，最后 Minor 清扫

每个修复后按 AGENTS.md 约定跑一次 `python quick_test.py` 冒烟验证。

---

## 修复状态追踪（2026-08-08 更新，升级路线图阶段 1-7 完成后）

> 路线图：`docs/superpowers/plans/2026-08-03-upgrade-roadmap.md`；各阶段均有独立实施计划与审查记录。

### Critical 9/9 已全部修复（阶段 1-3）

1. 报告 XSS → 全文转义 + 粗体正则修正
2. `--force` 失效 → 清缓存强制重采已生效（argparse help 亦已修正为准确描述）
3. 断点续采 → progress 表真实写入并参与跳过逻辑
4. WBI 重签名死代码 → 密钥过期自动重取重签，-352 末次不再空转
5. 纯数字 mid_hash 损毁 → 保留原值并单独处理
6. CRC32 碰撞当真实用户 → 置信度压制为"中/低"，碰撞假阳性统一标记"CRC32碰撞"
7. 并发击穿限速 → BiliAPIClient 线程安全限速（锁内原子化，docstring 已注明语义）
8. LLM Key 硬编码 → src/config.py 移出 git 追踪 + 环境变量覆盖 + 新增 config.example.py 模板（Key 置空）
9. login.py/login_bg.py 不保存 refresh_token → 已补充提取保存

### Important 已全部处理（阶段 1-5、7）

- `--max-users` 透传、spam 结果回写数据库（update_sender_spam）、失败 sender 重试、
  progress 表实效化、JSONDecodeError 捕获、WBI 参数编码过滤、cookie 原子写入+600权限+容错、
  cookie 刷新走 BiliAPIClient、失效时先试 refresh、danmaku get_raw 重试、
  resolve_all_senders 逐人异常隔离、CRC32 彩虹表毫秒级反查（替代 75 秒暴力破解）、
  子评论补采、网络异常不再吞成"用户不存在"、LLM max_tokens/批次降级/None 防御、
  收藏夹采集 try/except、analyze_profile 异常隔离、account_age_days 口径、
  弹幕 zip 截断、quick_test 与主流程对齐 —— 均已修复。

### Minor 大部分已修复（阶段 6-7）

- 已修复：seen_uids 去重、未使用导入（含 main.py 的 time/get_resolved_uids）、aid=0 跳过评论采集、
  storage contextlib.closing、相似度按唯一内容去重降复杂度、video_times 死参数删除、
  numeric_uids 死代码、XML 显式禁用实体（resolve_entities=False）、unique_contents 死代码、
  level_info None 防御、CRC 碰撞告警、CSS/JS 细节、追番 URL 用 config 常量、
  followers 类型一致、活跃时段固定东八区、up_analyzer 重复 import 与恒真条件、
  LLM 弹幕截断 30 条/解析为空诊断/冗余 env 回退、api_client 线程安全 docstring、
  quick_test argparse 化、报告与 README 明示弹幕池覆盖率上限。

- **仍开放（已知限制，不影响正确性）**：
  - `login.py` 按 Enter 后只轮询两次的体验问题（建议复用 `auth.login_by_qrcode()`）
  - `up_analyzer.py` `last_post` 用 `time.localtime()` 依赖部署机时区（仅影响日期显示）
  - `spam_detector` 规则4 的 `avg_interval` 整体平均对"集中爆发型"漏检（代码内已加注释说明，
    彻底修复需滑动窗口统计）
  - 相似度计算仍为先去重后 O(u²)（u=唯一内容数），极端刷屏用户可能偏慢，但不再卡死
