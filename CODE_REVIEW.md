# bilibili_profiler 全项目深度代码审查报告（第三版 · 六路交叉验证终版）

- **审查日期**：2026-08-27
- **审查方法**：本人逐行通读核心链路（main/api_client/storage/web/report/danmaku/comment/crc_rainbow/
  uid_resolver/combo_pool/auth/proxy_core/clash_ctl/cringe/spam/llm_analyzer/profile_analyzer/user_collector/
  up_analyzer/exporter/config/quick_test/login/login_bg/web_autostart，全文阅读），
  叠加**六路独立子代理盲审**（网络代理认证簇 / 弹幕评论采集簇 / UID 破解簇 / 检测 LLM 画像簇 / Web 前端簇 /
  主流程存储簇，不参考任何既有结论），并对现网环境实测（114MB profiler.db：journal_mode=delete、
  busy_timeout=5000、comments 8.2 万行 / danmaku 7.7 万行；git ls-files 核实凭证文件排除状态）。
- **结论口径**：全部高危项均经至少两条独立证据链（本人读码 + 子代理盲审或实测）确认；
  行号均为实际读码定位。

---

## 总体评价

工程质量整体较高，已核实无误的亮点：

- 断点续采"先落数据、后推检查点"核心次序正确（逐日弹幕 / 整页评论 / 实时池三处）；format 哨兵先行写入，
  主流程内部各中断窗推演无误判——真正的漏洞来自**外部写入者绕过哨兵**（高-1）与失败路径盲标完成（高-7/8）；
- SQL 全程参数化（f-string 场景拼接的均为 ? 占位符），ORDER BY/页大小走白名单，/download 三重拦截防穿越，debug=False；
- esc() 为 html.escape(quote=True)（单双引号均转义）；report.js escHtml 覆盖全部带数据 innerHTML 落点；
  js_json 转义 </ 防 script 逃逸；XML 解析有 XXE 防护；Cookie 写入 mkstemp+os.replace 原子化且 0o600；
  凭证未发现落日志路径（全局 grep 交叉核实）；config.py/cookie.json/profiler.db/cookies/ 均确认被 .gitignore
  排除、未被 git 跟踪；config.example.py 无凭证可安全提交；
- WBI 签名与官方 mixinKeyEncTab 逐位一致（子代理程序化比对），值过滤/排序/编码/md5 流程正确，重签前剥离旧签名参数；
- CRC32 MITM 分治数学实现严谨（前缀×5 位定长后缀切分完整覆盖 ≤10 位 UID，逐候选 zlib 精确复验无假阳性）；
- 限速硬约束：全部 B 站请求经 BiliAPIClient（仅 Clash 控制器自检走裸 requests，本机直连不触风控）；
  超时覆盖齐全（默认 15s、控制器 3-5s、下载 30/120s）。

但存在四类系统性问题：**安全面六处硬伤**、**完成状态标记过于乐观（累积损害最难挽回）**、
**弱恢复/无收敛设计**、**quick_test 对正式库的两处污染**。以下按核实结果列示。

---

## 一、高危（建议立即修复，均已逐条读码复核）

### 高-1 quick_test 采样评论污染续采判据——正式分析永久跳过全量评论采集 ✅双证复核
- **位置**：`quick_test.py:114, 128-129`（fetch_comments 未传 bvid → 所有 phase_state 哨兵被短路不写；
  save_comments 幂等落库且不写哨兵，`storage.py:395-421` 核实）；判据在 `src/main.py:249-264`
- **问题**：quick_test 把 50 条采样评论写入正式库后，run.py 的 phase_comment 看到"库里有评论 + done/mode/format
  三者皆无"即判为旧版完整落库而跳过重采——真实几百上千条评论永久缺失（问题评论榜/高回复评论/争执焦点全残缺），
  唯一出路 --force 又连带清掉 LLM 判定缓存白烧一轮 token。quick_test.py:81 注释"采样数据不落库"只对弹幕成立。
- **修复**：评论采样同弹幕采样一样不落库；或 save_comments 前先写 format 哨兵。
- **同源问题（子代理新增）**：`quick_test.py:84` 的 save_video_info 用无 danmaku_coverage 字段的 video_info
  以 INSERT OR REPLACE 整体覆盖 videos 行——正式跑完的视频其覆盖率统计被冒烟测试冲掉（违反"不删除数据"约定）。
  建议冒烟测试不写 videos，或合并保留旧覆盖率字段。

### 高-2 批次 LLM 响应未经解析校验即写缓存——坏响应永久毒化判定 ✅双证复核
- **位置**：`src/cringe_detector.py:146-149`（save 在 parse 之前）、`:177-179`（解析为空仅警告视为成功）、
  `:281-282 / 389-390`（残缺聚合再存整段缓存）；`:128-132`（空串缓存也命中）
- **问题**：JSON 截断（LLM_MAX_TOKENS）、格式漂移（markdown 包裹）、空 content 都以"成功"姿态固化进 llm_cache；
  请求不进重试链，重跑命中毒缓存静默丢失该批判定且无告警。与"不放弃任何批次"的模块自述相悖。
- **修复**：改为"先 _parse_verdicts 校验、成功才落缓存"；空响应与解析失败拒绝入库并计入重试。

### 高-3 存储型 XSS：争执焦点关系图 JSON 直拼单引号属性 ✅三证复核（本人+Web 簇+前版）
- **位置**：`web.py:849`（`data-af-graph='{json.dumps(graph, ensure_ascii=False)}'`）；数据源 `web.py:728-762, 831-846`
- **问题**：图节点 name 来自 `comments.uname`/`users.name`（外部用户可控）。json.dumps 只转义双引号不转义单引号，
  昵称含 ' 即突破 HTML 属性边界注入 `x'onmouseover='alert(1)` 类载荷。注入内容存入 _PAGE_CACHE 稳定复现；
  页面无 CSP，脚本同源运行后可调用全部 API（含删除报告）并外传报告数据。
- **纠偏**：`web.py:1230` 的 data-wc 虽同款写法但安全——分词只产出纯中文词（`up_analyzer._tokenize` 正则
  [\u4e00-\u9fff]{2,}），词云 JSON 不含引号。真正可注入的仅 data-af-graph 一处。
- **修复**：改为 `esc(json.dumps(graph, ensure_ascii=False))` 双层封装（dataset 读取端无需改动）；加基础 CSP。

### 高-4 删除 / 重新生成接口零鉴权零 CSRF ✅双证复核
- **位置**：`web.py:1856-1870`（delete）、`1889-1902`（regenerate）
- **问题**：两个 POST 不读请求体、无 Origin/自定义头/Host 校验。绑 127.0.0.1 挡不住浏览器跨站简单请求
  （无 body 的 POST 表单即简单请求，无 preflight）：任意网页可
  fetch('http://127.0.0.1:8000/api/video/BVxx/delete', {method:'POST', mode:'no-cors'})
  删光六表数据+导出文件（false_positive 误报标记、深掘缓存一并消失，部分不可恢复），或静默触发 regenerate
  驱使本机登录 Cookie 跑全量流水线撞风控。api_analyze/api_false_positive 因依赖 request.get_json 被 preflight
  间接保护，唯独这两个破坏性接口裸奔。服务端不校验 Host 头，DNS rebinding 场景可升级为读响应窃取报告数据。
- **修复**：强制自定义头（迫使预检失败）或校验 Origin 为空或 http://127.0.0.1:<port>，同时校验 Host。

### 高-5 CSV 导出公式注入 ✅双证复核
- **位置**：`src/exporter.py:24-45`（字段来源：昵称/签名/刷屏原因/IP属地/标签等上游不可信数据）、`:48-57`
- **问题**：以 = + - @ \t 开头的单元格未做任何转义，文件用 utf-8-sig BOM 明确面向 Excel 直开。
  任意 B 站用户把昵称改成 =HYPERLINK(...) 即进入导出文件，Excel 打开即执行公式——可泄露本地文件路径或钓鱼跳转。
- **修复**：按 OWASP CSV Injection 规范对首字符统一前置 '；过滤控制字符。

### 高-6 SQLite 未启用 WAL/busy_timeout，双进程读写必撞锁 ✅实测复核
- **位置**：`src/storage.py:18-22`（get_db 仅 connect+row_factory）；**实测现网库 journal_mode=delete**，
  busy_timeout=5000（默认值）
- **问题**：web.py 读进程（每请求六表聚合指纹 + face_cache 补采线程写库 + 手动分析 job 写库）与 run.py 写进程并存，
  回滚日志模式下读写互斥；写锁持有超 5 秒即抛 database is locked → video_page/index 无异常兜底直接 500
  （对比 api_danmaku 有 sqlite3.Error 兜底）。附带两点放大：
  (a) cringe 判定 16 线程并发写批次缓存，save_llm_cache 遇锁只打警告（`storage.py:671-672`）→ 缓存静默丢失
  → 重跑重复调用 LLM 白烧 token；
  (b) phase5 多号分片的 save_user_data 无异常保护，锁竞争可从 fut.result()（`main.py:566`）炸穿整个阶段 5；
  (c) face_cache 补采线程 `web.py:670` 的 save_face 未包 try，锁冲突以未捕获异常逃出线程、本轮补采中断。
- **修复**：get_db 一次性设 PRAGMA journal_mode=WAL; busy_timeout=10000; synchronous=NORMAL；
  video_page/index 包 sqlite3.Error 降级；save_user_data 纳入 collect_one 的 try 范围；save_face 包 try。

### 高-7 历史弹幕无论成败一律置 done=1——整轮失败被永久判"已采集" ✅双证复核
- **位置**：`src/danmaku_history.py:248-249`（无条件写 done），联动 `:153-155`（月份索引失败静默跳过）、
  `:233-236`（单日异常仅警告）、`:161-162`（HTTP 200 错误页解析出 0 条照常计入 fetched_days）
- **问题**：三层失败全静默滑向完成：SESSDATA 失效/风控期跑一遍后历史弹幕从此永久缺失，非 --force 无法自愈。
  与 comment.py 的 natural_end 模式（`comment.py:142, 158-162`）形成鲜明对比。
- **修复**：仅当时间窗完整迭代且无失败迹象才写 done；月份索引失败与单日失败置 natural_end=False；
  seg.so 返回体增加 protobuf 特征校验（合法 DmSegMobileReply 首字段应为 field 1 + wire type 2）。

### 高-8 评论断点续采页码计数器跨 wbi/legacy 双接口复用，模式切换漏采大段评论并误标完成 ✅双证复核
- **位置**：`src/comment.py:273-287`（resume_page 在 `:276` 不区分上次 mode 即传入两路径；offset 在 278-281 才校验模式）
- **问题**：docstring（267-268）声称"模式互换时忽略游标从头翻页"，实现只忽略游标、页码不归零：
  "上次 legacy 采 57 页 + 本次切 wbi" 组合 → 从第 1 页内容开始却按 58 号计页，MAX_COMMENT_PAGES 预算被虚耗；
  极端情况 resume_page >= max_pages 时循环体一次不执行、natural_end 保持初值 True、
  `:198-199` 照样写 done="1"——零请求直接宣布采集完成，评论区后半段永久丢失。反向切换同病。
- **修复**：mode 不一致则 resume_page 与游标一并归零从头翻页（UNIQUE 约束保证幂等）；
  本次实际请求数为 0 时不应具备 natural_end 资格。

### 高-9 LLM 整轮重试对致命错误无终止条件 ✅双证复核
- **位置**：`src/cringe_detector.py:188-193`（while pending 无轮次上限）、`:150-159`
  （一切非 RateLimitError 直接换厂商最终 raise）
- **问题**：AuthenticationError(401)/BadRequest(400)/NotFoundError（模型名错）/DNS 中断等不可恢复错误都走
  generic 分支——主备厂商各瞬间失败一次后进入"等待 60s×轮次递增封顶 300s → 整轮重试"死循环，
  永不收敛也不退出报告原因。直接违背"失败要降级而非中断"硬约定。
- **附带**：APITimeoutError/APIConnectionError 等高度瞬态错误（150-159 行）既不短退避也不在同厂商重试，
  直接烧掉备用厂商机会并升级为 ≥60s 整轮等待。
- **修复**：4xx 致命类型直接上抛交由 phase 层降级；给 while 加总耗时熔断（如 30 分钟）；
  瞬态连接类错误纳入同厂商 1–2 次短退避。

### 高-10 combo_pool 多线程分片下长冷却兜底失效 ✅复核
- **位置**：`src/combo_pool.py:86-114`（fn(client) 不持池锁、111 行冷却 sleep 在锁内）、
  配套 `src/main.py:563`（shards[i % len(shards)] 按提交序分配，任务从共享队列领取，线程与分片不绑定）
- **问题**：线程 A 进入长冷却睡 600s+ 期间，线程 B 可继续用同一子池高速撞 -412/-352——
  每个 RiskControlError 又逐一阻塞在同一把池锁上，冷却结束后圈计数再 +1 再睡，直到 MAX_RISK_ROUNDS 耗尽抛错。
  结果是"冷却期内仍高速打请求"（加重风控）+ 本应 10 分钟封顶的事拖到 30 分钟以上。
- **修复**：任务分配改为线程↔分片绑定；或冷却前先短锁轮换、等待移到锁外并以"冷却截止时刻"让后续请求方统一遵守。

### 高-11 Ctrl+C 在阶段 5 多号并行分片下无法及时退出 ✅复核
- **位置**：`src/main.py:561-569`（with ThreadPoolExecutor 块）、`:539-549`（collect_one 异常覆盖不全）
- **问题**：(a) with 块 __exit__ 调 shutdown(wait=True) 且不取消队列：KeyboardInterrupt 后成百上千个排队任务
  逐个执行完才退出（小时级），与"任意位置 Ctrl+C 可续"承诺冲突；(b) collect_one 的 try 只包住 collect_user_data，
  load_user_data/save_user_data 的 sqlite 异常（如 database is locked）会从 fut.result() 炸穿整个阶段 5。
- **修复**：捕获 KeyboardInterrupt 后 ex.shutdown(wait=False, cancel_futures=True)（Py3.9+）；
  try 覆盖扩展到缓存读写与落库全程。

### 高-13 批量预验证无异常防护——风控/代理故障中断整个解析阶段 ✅子代理新增（本人复核）
- **位置**：\`src/uid_resolver.py:110\`（_batch_verify_uids 的 client.get 无 try/except），对照单点路径
  \`verify_uid_exists:65-92\` 有完整兜底
- **问题**：组合池成员在风控圈数耗尽时上抛 RiskControlError（\`combo_pool.py:105-107\`）、代理故障时上抛
  ProxyConnError（\`:93-95\`）；批量路径的 client.get 只处理"返回 dict 且 code!=0"的降级，不处理**抛异常**
  路径。异常沿 _batch_verify_uids → resolve_all_senders → phase_resolve（\`main.py:733\` 无 try）冒泡到
  run_analysis，最终 SystemExit(1)/re-raise——**整个视频分析在恰恰要对抗的风控窗口内被中断**，违背
  "失败要降级而非中断"硬约定（函数文档自述"整批失败降级单点逐验"，实现未兑现）。
- **修复**：client.get 包 try/except，捕获 RiskControlError/ProxyConnError 后该 chunk 降级单点逐验
  （verify_uid_exists 自身已兜底返回 UNKNOWN），保证该函数任何情况下只返回 (found, unknown) 不外抛。

### 高-12 自动下载并执行未做完整性校验的 mihomo 二进制（供应链 RCE）✅子代理新增
- **位置**：`src/proxy_core.py:48-92`（_download_binary）、`:95-106, 178-194`（ensure_binary/start 默认
  allow_download=True）、`:208-210`（Popen 拉起）
- **问题**：本机无 vendor/mihomo、data/mihomo 且 MIHOMO_PATH 未设时自动下载：先经 GH_PROXIES（默认含
  gh-proxy.cn、gh-proxy.com、mirror.ghproxy.com 等多个第三方镜像域）查询 release，再下载 .gz 解压、
  chmod 0o755 后直接以当前用户权限执行。全程无 SHA256/签名/发布方校验。
- **影响**：任一 gh 镜像被攻破/投毒（TLS 只保证到镜像本身），即可让恶意二进制以当前用户身份运行；
  该进程具备网络能力，且可直接读取 data/mihomo_runtime/config.yaml（含订阅凭证，0o600）。
  RCE + 凭证窃取级风险。
- **修复**：锁定下载版本并内置官方 SHA256 校验；优先 github.com 直连（仍要校验哈希）；
  或默认 allow_download=False 强制手动放置受控二进制。

---

## 二、中危

### 架构与正确性
| # | 问题 | 位置 | 说明 |
|---|------|------|------|
| 中-1 | IP 池摘代理降级后永不恢复 | `combo_pool.py:128-148, 234-236` | 连续 3 次失败即进程内永久直连，机场短暂抽风毁掉全程换 IP 能力；_clash 对象连同节点列表一起丢弃。应设冷却计时器到期重探 |
| 中-2 | crc_rainbow 惰性建表非线程安全 | `crc_rainbow.py:54-70` | 三个全局变量分步赋值、哨兵只查第一个；并发首查可读到 _small_uid_map=None → lookup 抛 AttributeError（resolve_all_senders 330 行无 try 承接），最坏炸掉阶段 4。web 上并发分析 job 即可触发。局部构建完最后一步设哨兵或加锁 |
| 中-3 | 历史弹幕单日失败天空洞不可自愈 | `danmaku_history.py:229-243` | 第 i 天失败仅警告、第 i+1 天成功推进 last_date 后，i 天被单调游标永久越过。为失败日记账（phase_state 增 failed_dates），续采优先补采 |
| 中-4 | 今日快照天然不完整且 done 终结滚动机制 | `danmaku_history.py:240-249` | 分析当晚新弹幕不再回填、未来新日期快照也无法刷新历史重叠区。重跑总是额外追拉最近几天（dmid 幂等） |
| 中-5 | 半成品续采完成后 danmaku_coverage 永久缺席 | `main.py:74-80, 90-98` 对照 `120-127` | 首次运行中断于历史采集成时 videos 行从未写过覆盖率，后续半成品续采分支不重算 coverage，概览页永久缺覆盖率信息（叠加高-1 的 quick_test 覆盖清空问题） |
| 中-6 | 概览/弹幕浏览器类别统计绕过误报扣除 | `web.py:479-483, 1622`（扣除点 590-622） | 类别分布从 users.profile_json 的 cringe.categories 汇总，感知不到 false_positive 表，与 UI 文案"标记后不计入聚合"矛盾 |
| 中-7 | 非 WBI 接口 -352/-403 也被强行重签重试 | `api_client.py:239-262` | 关注列表等非 wbi 端点命中风控码会被附加无关 w_rid/wts 签名参数重发；应先判 _is_wbi_api(url) 再走重签分支 |
| 中-8 | WBI 密钥获取失败无负缓存 | `api_client.py:85, 95-97` | NAV 正被风控期间，每个 wbi 请求先附打一次注定失败的 NAV，请求量翻倍且全打在风控敏感期加重雪球。失败后记 N 秒负缓存（对齐 _ensure_buvid3 的每进程一次模式） |
| 中-9 | post() 不解析业务风控码 | `api_client.py:325-328` | HTTP 200 + code=-412 不触发换号不计圈数，原样返回当正常数据；且 post 的 HTTP412 分支无 get() 的短退避原地重试（`312-359`）。当前 post 调用方少故列中危，但语义不对称属隐患 |
| 中-10 | spam_detector 最坏情况无上限保护（时间+内存双重） | `spam_detector.py:39-46, 87-89` | similarities 列表全量物化 n(n-1)/2 个 float（n=5000 → 约 1250 万项数百 MB；n=1 万 → GB 级）+ O(u²) 次 SequenceMatcher + 突发窗口 O(n²)。改滑动窗口 O(n) + 加权累加不物化列表 |
| 中-11 | mihomo 子进程孤儿驻留 | `proxy_core.py:208-216, 179-181, 232-240` | 清理仅依赖 atexit，SIGKILL/OOM/终端硬终止后核心常驻并周期性外联（gstatic 600s + 订阅 86400s），下次运行又随机端口拉新核心并存。stop() 不取 _start_lock 与 start() 竞态、不清理含凭证的 config.yaml。建议 PDEATHSIG + 启动时清理历史实例 |
| 中-12 | 批次缓存 key 与语义输入错位 | `cringe_detector.py:121-124` vs `222-229` | batch key 只含纯 content digest，整段 key 含 count/语境 → 活跃视频隔日重跑 count 变化使全部已完成批次缓存 miss（孤儿键堆积无 TTL 清理）；标题/简介变更反而不失效任何缓存 |
| 中-13 | false_positive 按 dm 全文标记误伤面大 | `storage.py:171-179`；PK (bvid,kind,target) | "哈哈哈""6"类高频文本一条标误报，全员同名内容从聚合/疑似分/浏览器列表全扣且无法单独恢复；面板"N 条"远小于实际隐藏量。确认框至少提示影响条数，长期改 content_hash|mid_hash 组合键 |
| 中-14 | 达上限截断写入 done 后增量永久失效 | `danmaku_history.py:219-221, 248-249`；`comment.py:194-199` | HISTORY_MAX_DAYS 打满/MAX_COMMENT_PAGES 耗尽均无条件 done=1，调大上限重跑自动补齐的期望落空。写 truncated 标记供续采识别 |
| 中-15 | web job API 三处竞态 + 外部进程盲区 | `web.py:1812-1836, 1850-1862, 1894-1900` | /analyze 是唯一不做 _has_running_job 检查就起 job 的入口（多标签页/curl 可并发起同 BV 流水线互相抢限速，mid_hashes 列表长度无上限）；regen/delete 的"检查-注册"两次加锁非原子；JOBS 从不淘汰；内存 JOBS 感知不到外部 CLI 进程写的库，删除可与 run.py 写入交错。统一入口临界段注册 + SQLite 互斥占位 |
| 中-16 | af 关系图监听器泄漏 | `report.js:175-535`（464-492 box 级、495-519 window 级） | drawAfEdges 每次切标签/resize 重绑监听从不移除，旧 SVG/edgeRecs 被 N 份闭包滞留，N 次后每个手势 handler 执行 N 次。一次性绑定标志位或 abortController |
| 中-17 | 整页一次性巨型 DOM | `web.py:1463-1474, 1075-1080, 1122-1148`；`report.js:614-630` | ≤1000 张卡片+不截断回复树服务端一次拼出可达数 MB，前端分页只是 display:none，筛选每次 appendChild 移动数百深卡片节点回流。卡片懒加载 + 回复树点击展开时惰性生成 |
| 中-18 | 历史弹幕天数上限保留最旧 400 天、丢弃最新（与"近期优先"设计意图相反） | `danmaku_history.py:210-228` | 月份升序+日期升序遍历，fetched_days 先耗尽上限 → 保留最早日期；而月级截断（months[-HISTORY_MAX_MONTHS:]）保留最近月份，两者方向矛盾，`:219-220` 日志"停止回溯更早月份"与真实行为相反。逆序遍历或统一语义 |
| 中-19 | 评论续采 seen_rpids 从空集重建，真重复页检测失效 + 子评论重复补采 | `comment.py:143-144, 170-175, 181` | 数据正确性靠 UNIQUE 兜底，但游标回卷时翻页无法提前终止，且每页每条主评论重走 _fetch_sub_replies（最多 25 页/条）重复请求。续采时从库加载 rpid 集合初始化 |
| 中-20 | 深掘每用户新建 OpenAI client 且无显式超时 | `llm_analyzer.py:71-83` | client 每次调用新建（无连接复用），create() 未设 timeout（SDK 默认 600s）→ 单用户最多挂 10 分钟×2 次重试才跳过；top K 串行放大总耗时。client 复用到 __init__，timeout 设 60-120s |
| 中-21 | get_favorite_contents 未包 try，单维度失败丢弃该用户已采集全部数据 | `user_collector.py:489-492` | 其它维度均被 try 包裹唯独此调用裸奔；抛异常 → collect_user_data 整体上抛 → main 记 error 不落库，已消耗的 card/space/bangumi/folders 全部作废。子代理盲审新增 |
| 中-22 | 问题评论全量缓存键只指纹内容、不含 rpid 归属 | `cringe_detector.py:355-358, 366-367` | 续采新增与已判定内容相同的评论（复制粘贴"前排"）时内容集合不变 → 命中缓存 → 新 rpid 不在旧映射里 → 该新评论漏标。命中后用当前 content_rpids 重新展开 |
| 中-23 | config.py 内置明文真实凭证 | `config.py:77, 166, 177` | LLM_API_KEY/MIMO_API_KEY 默认为形如 sk- 的真实密钥、SUB_URLS 内嵌订阅 token。虽已被 .gitignore 排除且未入 git（实测核实），但明文驻留工作副本：复制/打包目录即泄露、误改环境变量即误用付费额度。建议默认置空强制走环境变量并**轮换现有 Key** |
| 中-24 | 续采"完整"路径 get_video_info 失败即整场失败 | `main.py:74` 对照 `danmaku.py:14-19` | resume 分支即使命中完整数据仍无条件刷新视频信息（1 个请求），get_video_info 非 0 即 raise 且无 try——本已完整缓存、无需重采的视频因一次风控/网络抖动就 SystemExit(1)/re-raise 崩溃，违背"失败要降级"。失败时回退 load_video_info 库内缓存 |
| 中-25 | wbi 续采半成品首页失败不降级旧接口、永久原地打转 | `comment.py:160-161, 284` | 降级判据是 page==1 and not resume_offset；续采时 resume_offset 非空 → 首续页失败走 break 返回 []（非 None）→ fetch_comments 不降级 legacy、不写 done。wbi 长期不可用时评论永久停在半成品，每次重跑原地打转不告警 |
| 中-26 | global_uid_map upsert 允许低置信结果覆盖高置信映射 | `storage.py:619-634` | ON CONFLICT 无条件 uid=excluded.uid, source=excluded.source。跨运行场景下，后到的单候选 CRC32 破解结果可覆盖先前"评论区验证"的明文映射（main.py:406-408 仅排除多候选碰撞，未排除单候选误命中覆盖）。按 source 优先级决定是否覆盖 |
| 中-27 | "账号×IP"退化为多账号共享单一出口 IP | `combo_pool.py:149-169, 70-84, 122-147`；`clash_ctl.py:119-135`；`proxy_core.py:156, 171-175` | 内置核心单 mixed-port 单 select 组，ClashCtl 为全部子池共享单例；任一分片切节点对所有分片同时生效。任意时刻所有账号共用一个出口 IP，被风控标记的 IP 承载了全部账号，"新号+新IP"只剩"新号"一个维度；且切节点会瞬时改变其他账号在途请求的出口。若维持现状至少显式声明"IP 维度为全局单点" |
| 中-28 | 单账号（子）池触发风控后单单元最多阻塞约 20 分钟 | `combo_pool.py:99-114`；`config.py:62, 95` | 子池单账号时任何 RiskControlError 都使 all(marks) 立即为真 → 长冷却 600s+ 抖动，循环 MAX_RISK_ROUNDS=3 次才抛 ≈ 20 分钟/单元，且 sleep 持池锁。单账号池没有"换号"兜底，长冷却无意义，应缩短或可中断 |
| 中-29 | ComboPool.run() 状态机锁外读 _idx，风险标记可能张冠李戴 | `combo_pool.py:88-89, 100-101, 58-68` | run() 在锁外读 _idx 并捕获 (name,client)，锁内用"当前" _idx 打风控标记：并发调用时 T1 用账号 A 请求失败、期间 T2 rotate 到 B，则 B 被标记风控、圈计数错乱。当前分片方案恰好单线程暂不触发，但 docstring 的线程安全承诺不成立。锁内快照 idx+client 或明确单线程约定 |
| 中-30 | Cookie 刷新成功但落盘失败被误判"刷新失败" | `auth.py:150-208`（save_cookie 在 try 内，206 行兜底 return False） | refresh POST 成功后 session 内存 jar 已是新 cookie；save_cookie 抛异常（磁盘满/权限）→ return False → load_extra_clients 跳过该小号、login_by_qrcode 回退重扫。应区分"刷新失败"与"刷新成功但落盘失败"，后者返回 True 并告警 |
| 中-31 | MITM 逐 hash 全前缀循环性能热点 + 明文命中者空跑破解 | `crc_rainbow.py:102-110`；`uid_resolver.py:325-332` | lookup 每次完整跑 range(1,100000) 前缀循环，预热后实测约 0.21s/hash——按上限 1000 发送者计仅本地计算 ≈3.5 分钟纯 CPU；且明文命中的 hash 仍空跑 crack_crc32 并并入批量验证 universe（结果在 _finalize 方法 1 直接短路弃用），产生无谓 API 调用。预计算 _advance5(prefix_crc[p])^z5 全表一次供所有 hash 复用；明文命中者跳过 MITM 与候选验证 |
| 中-32 | 单候选 CRC32 破解结果沉淀全局库，错误归因跨视频放大 | `uid_resolver.py:210-214`；`main.py:405-408`；`web.py:253-255` | "唯一存在候选"无法与"真实发送者是 16 位长 UID、CRC32 恰好撞上存在的 ≤10 位 UID"区分（文档自认 16 位不可解）；单候选不触发多候选禁沉淀条件 → 入库后在其他视频以明文语义复用。_plaintext_result 对 source=CRC32破解 压置信度+置 collision_risk 属部分缓解，但错误归因仍可跨视频累积。建议提高单候选破解持久化门槛，或复用端始终保留"破解"语义 |

### 附：其他值得记录的中危项
- **deep 缓存几乎总 miss**：`llm_analyzer.py:97-102, 36`——uid=None 共享命名空间 + follower/archive_count 等日变字段进 hash，与"跨视频复用"目标相反。
- **up_analyzer 三处裸 except 吞系统性失败**：`up_analyzer.py:86-87, 131-132, 159-161`——card/投稿接口结构变化时静默产空，仍计入 sampled/n/small_creators，统计口径被稀释无任何线索。
- **summarize_followings 默认 sample_size=0（全量）**：`up_analyzer.py:144`，安全上限全靠调用方传 MAX_UP_SAMPLE。
- **删除报告连坐跨视频资产**：`storage.py:602-612`——global_uid_map 按 mid_hash 删除会连坐其它视频的同人映射，
  users/deep 缓存无条件删除（声明过的取舍，建议 Web 端删除确认提示关联影响）。

---

## 三、低危（择要）

**认证与凭证**
- Cookie 刷新 confirm 与落盘之间存在窗口崩溃丢唯一登录态；多进程并发刷新同一 cookie 文件完全无锁（`auth.py:144-208, 284-307`）——建议 refresh 成功先盘后确认 + flock 互斥。另：confirm/refresh 用旧 refresh_token（197 行）且忽略 confirm 返回结果，需对照官方实现核实（子代理待核实项）。
- `_check_needs_refresh` 是死代码从未生效（`auth.py:123-132`）；load_cookie 不捕 OSError 族、glob 不过滤目录会炸穿整个小号发现（`auth.py:93-108, 292-296`）。
- buvid3/bili_ticket 启动首秒一次失败即整场裸奔（`api_client.py:116-139, 145`）——失败标记改带 TTL；且从 cookie 文件加载的 buvid3 不被识别（_buvid3 实例属性仍 None），每次新进程冗余打一次 spi。
- set_proxy/update_cookies 未加锁，与并发请求存在数据竞争（`api_client.py:399-405`；`combo_pool.py:139-147`）。
- CLASH_SECRET 以明文 HTTP Bearer 发送（`clash_ctl.py:42-43`）——loopback 下风险低，建议校验 CLASH_API_URL 必须为回环地址。

**解析健壮性**
- 手写 protobuf 长度字段不做越界校验，损坏流静默截断/残条默认值入库（`danmaku_history.py:53-65, 78-81`；`danmaku.py:206-209` 同病）。
- 互动弹幕单条 CommandDm 异常丢整份列表（`danmaku.py:243-251`，外层 255 行 except 兜底 return []），与 history 模块 per-item 策略不一致。
- p 属性第 7 段空串聚成一个 "" 伪发送者参与统计（`danmaku.py:60-62`）。
- 实时池 mid_hash 不 zfill(8)，与历史池/calc_crc32 的 8 位键不对称（`danmaku.py:60` 对比 `danmaku_history.py:100`）——待实测实时 XML 是否恒 8 位。
- 评论检查点 int() 无校验，脏 phase_state 抛 ValueError 中断评论阶段（`comment.py:276, 281`）。
- 充电名单单条脏 pay_mid 使整批 uid_map 归零（`comment.py:380-388` 的 int() 在最外层 except 内）。
- rpid 缺失默认 0，脏行在 UNIQUE(bvid,rpid,uid) 下互相顶替塌缩（`comment.py:37-38` 对照 `storage.py:395-421`）。
- 历史时间窗/up_analyzer.last_post 依赖宿主机时区，非东八区机器月界漏采（`danmaku_history.py:197-203`；`up_analyzer.py:128-130`）；WBI 密钥按本地日期滚动非 CST（`api_client.py:84-85`）；users.collected_at 用本地时间而 videos.created_at/false_positive 用 UTC，三种时间语义并存（`storage.py:41, 326, 612`）。
- `_fetch_sub_replies` 的 for...else 引用循环体内变量 total（`comment.py:92`），COMMENT_REPLY_MAX_PAGES 误设 0 时 NameError。
- ClashCtl._fetch_group 对非 dict JSON 抛 AttributeError 未被 list_nodes 捕获（`clash_ctl.py:53-68, 70-76`），第三方服务恰好占用探测端口时中断建池。

**检测与 LLM**
- 缓存版本号硬编码 v2/v3（`cringe_detector.py:229, 358`；`llm_analyzer.py:102`），prompt 变更需手工 bump 否则旧口径继续命中。
- 提示词注入面：弹幕/评论原文裸拼 prompt 无数据/指令边界隔离（`cringe_detector.py:56, 292`；`llm_analyzer.py:60`）——风险限于判定结果被内容操控，无外泄面。
- 判定索引 i 未排除 bool、severity 校验口径不一（`cringe_detector.py:253, 377` 对比 `:269, 382`）；`detect_problem_comments` 的 like 取 max 未防 None（`:340`，待核实 like 是否恒 int）。
- official_type 未归一化（`user_collector.py:52` 未 _safe_int 而 `:84` 有），若 B 站返回字符串则 `official_type >= 0` 抛 TypeError 致该用户画像静默丢失（`profile_analyzer.py:53`，待核实）。
- uid=0 未解析时误贴"B站原住民"标签（`profile_analyzer.py:16-21`）。
- 刷屏判定阈值/窗口散落硬编码未入 config（`spam_detector.py:87-94, 156, 164`），违背配置集中约定。
- cringe clients 字典惰性初始化线程竞态（浪费而非错果，`cringe_detector.py:107-113`）；模型重复编号不去重虚增 count（`:250-277`）；温度参数两模块口径不一（`:144` vs `llm_analyzer.py:79-80`）。
- CRC32 逻辑双实现（`comment.py:308` 内联 vs calc_crc32），建议统一走 uid_resolver。
- 批量/单点两路径 user_info 字段不一致（`uid_resolver.py:72-84` vs `:121-128`）——下游基本当死字段丢弃，建议补齐或移出返回结构。
- `_finalize` 的 2a 分支（候选∩明文 UID，`uid_resolver.py:194-204`）在"键恒等于 value 的 CRC32"不变式下不可达，属防御性冗余；其 next(...) 静默取首个的写法与"唯一消歧"文档不符。
- 批量名片接口"缺席=不存在"契约无防御（`uid_resolver.py:113-121`）：接口未来若截断/分页，缺席的真实 UID 会被误判碰撞假阳性。建议校验返回条数或疑似不完整时降级单点。

**存储与导出**
- senders OR REPLACE rowid 高水位单调膨胀；save_video_info 二次保存 created_at 被刷新重置（INSERT OR REPLACE 对省略列应用 DEFAULT，非置 NULL），Web 首页按 created_at 排序时重跑会把视频顶到最前——建议 ON CONFLICT DO UPDATE 保留 created_at。
- save_danmaku 是"先删后插"的死代码（`storage.py:352-368`，全仓 grep 无调用点），未来误调用会清空该视频弹幕，建议删除。
- comments 冲突时不回写 like/reply_count，高回复评论榜热度永远停在首采快照（`storage.py:417-419`）。
- dmid=0 行无去重防线，崩溃重放同日重复插入（`storage.py:377-384`，展示层 GROUP 归并缓解）。
- toggle_false_positive 先查后插非原子，并发双击 500（`storage.py:471-483`）。
- llm_cache 清理 LIKE 未转义 %/_ 也无 ESCAPE，畸形 bvid 可误删他视频判定缓存（`storage.py:563-564, 598-601`）；bvid 入口校验只有 startswith("BV")。
- 缺 (bvid,mid_hash,time)、(bvid,problem) 复合索引，114MB 实测库上报告渲染随发送者规模变慢。
- 导出文件名秒级精度、只增不删（--force/删除报告不清旧导出文件）；json.dump(indent=2) 内存峰值约数据两倍（`exporter.py:54-68`）。
- save_comments 静默丢弃 uid/content 为空的评论（`storage.py:409`），与"全量落库"文档存在细微偏差。

**Web 与前端**
- 内联 onclick 的 JS 字符串上下文转义盲区（`web.py:1547`，esc 的 &#x27; 会被 HTML 解析回 ' 无法保护内层 JS 单引号；mid_hash 为 B 站侧生成、当前闭合，属模式预警——JS 字符串值应改用 js_json 输出）；弹幕颜色 chip style 可注 CSS 外联信标（`:1557`，颜色由服务端 int 格式化，风险低）。
- 弹幕搜索 LIKE 通配符未转义，输入 % 退化全组扫描（`web.py:1973-1975`）。
- `_client_failed` 粘性失效，扫码恢复后必须重启 web.py 才能用分析功能（`web.py:103, 118-143`）——建议给出可重置途径。
- 指纹不含 videos 表与 REPORT_DIR 文件（`web.py:75-94`）：元数据更新不失效、CLI 追加导出后下载链接区陈旧；
  另 face_cache 用全局 COUNT 作指纹分量，任一视频补采头像令所有视频缓存过度失效（低效但正确）。
- 手动分析完成 flash 分支 JSON.parse 无 try/catch 可中断恢复流程（`report.js:976-993`）。
- delete/regen 用 URL 段构造 glob 模式，混入 [*? 元字符扩大删除范围（`web.py:1866-1869, 1036`）——bvid 仅 startswith("BV") 校验。
- `_load_profiles`/`_danmaku_panel_stats`/`index` 用裸 get_db() 无 closing，异常路径连接泄漏（`web.py:316-323, 462-478, 1344-1354`）。
- JOBS/_PAGE_CACHE 无上限不清理，长跑服务内存缓增（`web.py:65, 99`）；异常 str(e) 原样透出到 /api/job 与日志（`web.py:188, 293, 1847, 1882`），建议对外文案脱敏。
- 争执焦点名额公式硬编码 5 而非 ATTACK_FOCUS_TOP_N（`web.py:765`：5 + edge_cnt // 10），调配置保底与增量基准不联动。
- report.py:396/404 头像与用户名外链 target="_blank" 缺 rel="noopener"（与 450/547 等其它外链不一致）。
- safe_url 仅校验 scheme ∈ {http,https}（`report.py:162-167`），未拒绝 scheme 混用变体，纵深不足。
- `_reply_tree_html` 递归渲染（`web.py:1122-1148`）：环/父级缺失时整分量静默丢弃；极深链理论可 RecursionError（B 站楼层深度有限，风险低）。
- web.py 端口占用检测 TOCTOU（`web.py:2172-2178`：先 bind 探测再关闭再 app.run）。

**编排与工具**
- quick_test `_Tee` 以 "w" 覆盖上一轮日志、stderr 未接管、缺 encoding/fileno/isatty 等协议属性（`quick_test.py:33-47`）。
- login.py 扫码后仅再试一次 poll（`login.py:105-120`）；web_autostart log_f 句柄父进程持有至退出且 Popen 异常时不关闭（`web_autostart.py:41-44`）。
- web_autostart 端口探测只要任意服务响应即判 web.py 就绪——8000 被无关进程占用时跳过启动并把浏览器导向错误服务（`web_autostart.py:29-34, 52`）；bvid 未做字符集校验与 URL 编码（`:22-23`）。
- login.py/login_bg.py 小号登录二维码仍写主号路径 data/qrcode.png，主/小号并发登录互相覆盖（`login.py:61`、`login_bg.py:50`）。
- `stop()` 强杀路径缺最终 wait 产生瞬时僵尸；随机端口 TOCTOU 与 config.yaml chmod 前权限窗口（`proxy_core.py:232-240, 32-37, 201-203`）。
- ClashCtl 持锁内串行最多三个网络请求阻塞竞争线程（`clash_ctl.py:90-130`）。
- danmaku.get_video_info 失败即抛（`danmaku.py:17-18`）：已删除视频在全新路径（`main.py:101`）以异常 traceback 收场而非友好提示（resume 路径见中-24）。
- --max-users 未夹紧下界，0/负值静默产出空画像（`main.py:369-372, 520`；quick_test 已做 max(1,...) 处理不一致）。
- 空弹幕视频早退路径不写 done（`main.py:692-694`），每次重跑全量重采无缓存收益。
- 批量清单行内 # 注释被当 BV 号计入失败项（`main.py:810-821, 840`），文档"忽略 # 注释行"语义有歧义。
- `fetch_danmaku` 的 resp.encoding 赋值是死代码（`danmaku.py:82`，解析走 resp.content）；danmaku 模块跨文件导入 danmaku_history 私有 wire 函数，耦合待抽公共模块。

---

## 四、已核实为安全的关注项（不计入问题）

- **断点续采核心契约次序正确**：history 每"日落库（append_danmaku 按 dmid 去重）→ 再推 last_date"、
  comment 每"整页落库 → 再推 offset/page"；format 哨兵先行写入，主流程内部各中断窗推演无误判；
  --force 的 clear_video_cache 清理范围未误删跨视频资产（users 按引用计数保护、deep 缓存保留、
  llm_cache 仅清本 bvid 前缀）。
- **CRC32 MITM 数学实现严谨**：前缀×5 位定长后缀切分完整覆盖 ≤10 位 UID 无遗漏，每个候选经 zlib 精确复验
  保证无假阳性——子代理以实际运行校验（仿射恒等式 crc32(s,x)==_advance5(x)^crc32(0^5)^crc32(s) 对 2000 组随机
  样本成立、边界值 100000/999999/9999999999 无漏候选、无假阳性、覆盖无 off-by-one）确认；
  明文优先链顺序固定（评论>充电>互动>元信息>全局库）；多候选取最小 + 碰撞条目不入库口径一致。
- **WBI 签名正确**：WBI_OE 与官方 mixinKeyEncTab 逐位一致；值过滤 !'()* → 排序 → quote 大写百分号 →
  wts → md5 流程与官方 encodeURIComponent 语义一致；重签前剥离旧 w_rid/wts。
- **限速/重试/风控状态机边界正确**（api_client.get/get_raw）：-412/-352/-403/HTTP412 的 retry→raise→降级
  边界清晰，末次 attempt 不再浪费冷却；冷却截止时间戳自过期；退避 sleep 在锁外、全局冷却在锁内与注释语义一致。
- **SQL 安全**：全部 execute 参数化；ORDER BY/页大小走白名单；IN 列表全为 ? 占位符；/download 有 report_ 前缀
  + / + .. 三重拦截 + send_from_directory 内建 safe_join 双重防护；debug=False。
- **前端转义**：esc() = html.escape(quote=True)（单双引号均转义）；escHtml（report.js:727）覆盖 5 处带数据
  innerHTML 落点；js_json 转义 </；URL 还原路径均为 dataset/数值赋值无 DOM 拼接；data-wc 词云属性因分词仅产出
  纯中文词而安全（前版误列为同款风险，已纠偏）；static/index.js 无 XSS 无监听器泄漏。
- **凭证面**：print 全库无凭证交叉；config.py/cookie.json/profiler.db/cookies/ 均被 .gitignore 排除且未被
  git 跟踪（git ls-files 实测）；config.example.py 无凭证；Cookie 写入 mkstemp+os.replace 原子且 0o600；
  账号名正则 [A-Za-z0-9_-]{1,32} 防路径注入；订阅链接剔除引号/反斜杠/换行防 yaml 注入、警告只打印序号不打印 URL。
- **语义幂等**：comments.problem 回写先清后写幂等；疑似分阈值 >= 边界与 config 注释一致；spam 除零守卫齐全；
  双厂商缓存 key 含模型名互不污染、v3 口径版本升级天然 miss；--batch 确实 launch_web=False。
- **弹幕 JSON API**：search/sender/category 的 IN 过滤全参数化，sort/order/page_size 走白名单，无 SQL 注入。

---

## 五、建议修复顺序

1. **日常可用性**：高-10（风控放大）、高-11（Ctrl+C 挂死）、高-9（LLM 死循环）、中-28（单号池 20 分钟级冷却）；
2. **数据完整性（累积损害最难挽回）**：高-1（quick_test 双污染）、高-7 / 高-8 / 中-3 / 中-14 / 中-18 / 中-25
   （done 盲标、页码错位、天空洞、截断即完成、天数上限方向反了、wbi 续采卡死）；
3. **安全七件套**：高-3（XSS）、高-4（CSRF）、高-5（CSV 注入）、高-2（毒缓存）、高-6（WAL）、
   高-12（mihomo 供应链校验）、高-13（批量预验证异常防护）+ 中-23（轮换明文 Key）；
4. 其余中危按使用频率取舍（建议优先 中-2 建表竞态、中-15 job 竞态、中-21 favorite_contents 裸调用、
   中-24 resume 降级、中-26 映射覆盖优先级、中-31 MITM 性能）；低危可在触碰对应文件时顺手修。

---

### 修订记录
- v1（2026-08-27 首版）：11 高 / 17 中 + 低危清单。
- v2：逐条复核通过并补充触发细节；纠偏两处（data-wc 属性实际安全；save_video_info created_at 语义为刷新而非置 NULL）。
- v3（终版）：并入六路独立子代理盲审成果——新增 高-12（供应链 RCE）、高-13（批量预验证无异常防护）、
  中-18~中-32（天数上限方向反了、
  seen_rpids 空集、深掘无超时、favorite_contents 裸调用、问题评论缓存漏标、明文凭证、resume 无降级、
  wbi 续采卡死、全局映射覆盖优先级、共享单出口 IP、单号池冷却、run() 竞态、刷新落盘误判）、
  高-1 同源 quick_test 覆盖覆盖率、高-6 的 save_face/save_user_data 放大路径，及四十余条低危
  （save_danmaku 死代码、时间语义混用、rel=noopener、top_n 硬编码 5、qrcode 路径、--max-users 夹紧、
  空弹幕 done、批注行内注释、_Tee 协议、safe_url 白名单、JOBS 无清理等）；实测确认现网库 journal_mode=delete。

---

## 六、修复记录（2026-08-29，全部条目逐项核实后修复）

核实方式：8 路独立只读子代理对全部条目按当前代码（HEAD 94d2fbe）逐条复核；修复方式：10 路并行修复（按文件分组互不冲突）+ 常量统一收口 config；验证：全量 py_compile / 导入冒烟 / 各组离线桩测试 / B站链路单请求实测（get_video_info 正常）/ web 服务起停 + CSRF 防护实测（坏 Origin/Host → 403，正常本机源放行）/ 现网库 journal_mode 实测已切换 WAL。

### 高危：13 项全部已修
- 高-1 已修：quick_test 评论采样不再落库（纯内存统计）；save_video_info 落库前合并保留旧行 danmaku_coverage；_Tee 追加模式+接管 stderr+补齐协议属性。
- 高-2 已修：批次响应先解析校验成功才落缓存，空串/坏响应进重试链；缓存命中处空串视为未命中；整段缓存仅在零失败批次时才写（弹幕/评论两套同构）。
- 高-3 已修：data-af-graph 改双引号属性+esc() 整段转义；内联 onclick 的 mid_hash 改用 js_json 字面量。
- 高-4 已修：before_request 钩子对所有非 GET 请求校验 Origin（仅放行 127.0.0.1/localhost 同源）与 Host（防 DNS rebinding），违例 403。
- 高-5 已修：CSV 单元格首字符 ∈ {=,+,-,@,\t} 前置 '，剥离控制字符。
- 高-6 已修：get_db 每连接 busy_timeout=10000 + synchronous=NORMAL + WAL（现网库实测已 wal）；index/video_page 加 sqlite3.Error → 503 兜底；save_llm_cache 返回 bool 且告警明示 token 损失；collect_one 缓存读写与落库全程容错；face 补采线程包 try。
- 高-7 已修：响应体 0x0A protobuf 特征校验（错误页按当日失败）；月份索引/单日/特征失败均记账；仅时间窗完整无失败才写 done=1。
- 高-8 已修：检查点 mode 与实际不一致时页码/游标一并归零；本轮 0 实际请求时 natural_end=False 不写 done。
- 高-9 已修：致命 4xx（认证/参数/模型不存在）直接上抛由 phase 层降级；瞬态错误同厂商短退避 2 次再换厂商；整轮重试加 1800s 总耗时熔断（LLM_RETRY_BUDGET_SECONDS）。
- 高-10 已修：冷却改为「截止时刻锁内记录、锁外统一等待」；main.py 任务分配改线程↔分片绑定（每分片一个 worker 串行消费）。
- 高-11 已修：KeyboardInterrupt 走 shutdown(wait=False, cancel_futures=True)；collect_one try 覆盖 has/load/save_user_data 全程。
- 高-12 已修：锁定 mihomo v1.19.30，内置官方 release 资产 sha256 表（来源：GitHub API assets[].digest），下载后校验不匹配拒绝执行；github.com 直连优先、镜像仅回退。
- 高-13 已修：_batch_verify_uids chunk 循环包 try，异常 chunk 降级单点逐验，函数承诺不外抛。

### 中危：31 项已修 / 1 项跳过
- 已修：中-1（摘代理 600s 重探恢复）、中-2（建表双检锁）、中-3（failed_dates 记账+优先补采）、中-4（done=1 滚动补采最近 HISTORY_RECENT_REFRESH_DAYS=3 天，dmid 幂等）、中-5（续采补算 danmaku_coverage）、中-6（类别统计源头扣除误报）、中-7（非 WBI 不重签）、中-8（WBI 密钥 60s 负缓存）、中-9（post 风控码语义对齐 get + 412 短退避）、中-10（相似度边算边累加 + 突发窗口滑动双指针 O(n)）、中-11（PDEATHSIG + stop 取锁补 wait + stop 清理含凭证 config.yaml + 0o600 原子写入）、中-12（批次缓存改稳定前缀 cringe:{bvid}/cmt:{bvid}）、中-13（误报标记前端提示影响条数，count_only 预查）、中-14（截断写 truncated=1；评论截断不写 done）、中-15（job 原子注册互斥 + JOBS 淘汰 100 + mid_hashes 上限 200）、中-16（关系图监听只绑一次）、中-18（降序遍历保留最新日期）、中-19（续采从库重建 seen_rpids）、中-20（深掘 client 复用 + timeout=120s）、中-21（favorite_contents 包 try 降级）、中-22（评论缓存改 content→verdict 语义，口径 v4）、中-24（resume 失败回退 load_video_info）、中-25（本轮首请求失败即降级 legacy）、中-26（global_uid_map 按来源优先级覆盖，明文>破解）、中-28（单账号池冷却缩至 120s）、中-29（锁内快照 idx+client）、中-30（落盘失败仍返回 True+醒目告警；刷新顺序改先落盘后 confirm）、中-31（预计算 adv5 表 0.21→0.049s/hash；明文命中跳过破解与验证）、中-32（CRC32 破解一律不沉淀全局库，main/web 双侧）。
- 中-17 跳过：整页巨型 DOM 懒加载属架构级改造（卡片懒加载+回复树惰性生成需新增 API 与前端重构），另行评估；当前规模（≤1000 卡片）可接受。
- 中-23 部分处理：代码未动（config.py 为 gitignored 用户配置，置空会破坏现有运行）；**请用户尽快轮换 config.py 中的 LLM_API_KEY / MIMO_API_KEY 与 SUB_URLS 订阅 token**，长期建议改用环境变量。
- 中-27 by design：已在 combo_pool/proxy_core docstring 显式声明「IP 维度全局单点，所有账号共享同一出口 IP」。
- 附项：deep 缓存 digest 维持现状（prompt 输入必须全部参与指纹，跨视频命中属错误复用）；up_analyzer 三处裸 except 已加告警、sample_size 默认改 50；删除报告连坐为已知取舍未改。

### 低危：大部分已修
- 已修（择要）：_Tee 协议、--max-users 夹紧、空弹幕早退写 done（resume 判据改 phase_state 哨兵）、批量行内 # 注释、视频不存在友好提示、弹幕搜索 LIKE 转义、/api/reload_client 复位、指纹纳入 videos 表、flash JSON.parse 兜底、glob.escape(bvid)、裸 get_db 改 closing、对外错误文案脱敏、ATTACK_FOCUS_TOP_N 联动、rel=noopener、safe_url hostname 校验、回复树防环+深度上限、protobuf 越界校验、互动弹幕逐条容错、空 mid_hash 跳过、实时池 zfill(8)、检查点 int() 容错、充电名单逐条 try、rpid 兜底、total 初始化、删 resp.encoding 死代码、时间窗改显式 UTC+8、CRC32 统一 calc_crc32、_finalize 2a 死代码删除、批量返回对账降级、批量 user_info 精简集注释、buvid3/bili_ticket 失败 TTL+jar 复用、set_proxy/update_cookies 加锁、CLASH_API_URL 回环校验、_fetch_group 类型防御、save_video_info/save_sender 改 ON CONFLICT DO UPDATE（保留 created_at、消 rowid 膨胀）、删 save_danmaku 死代码、comments 冲突回写 like/reply_count、toggle_false_positive 原子化、llm_cache 清理 LIKE 转义、新增 (bvid,mid_hash,time)/(bvid,problem) 复合索引、save_comments 丢弃计数、登录限时轮询、web_autostart 就绪探测改专有端点+bvid 校验编码、log_f 异常关闭、小号二维码分名存放。
- 未改（判定不成立或风险低于改动代价）：data-wc 词云属性（核实安全）、颜色 chip（颜色恒为计算值 #xxxxxx）、温度口径差异（判定 0.3/深掘 1.0 为有意设计）、uid=0 误贴标签（main.py:596 已过滤不可达）、confirm 用旧 refresh_token（B站协议规定动作）、json.dump 内存峰值（流式写出不成立）、like=None 防御已顺手加、时间语义三制并存（改动代价大于收益）、端口 TOCTOU（本地低风险）、face_cache 全局 COUNT 指纹（低效但正确）、导出文件只增不删（已知取舍）、ClashCtl 锁内网络请求（≤2 个，可接受）、severity 口径（前版已修）。

### 新增配置常量（config.py / config.example.py 末尾「审查修复新增」区块）
LLM_DEEP_TIMEOUT=120、LLM_RETRY_BUDGET_SECONDS=1800、LLM_TRANSIENT_RETRIES=2、HISTORY_RECENT_REFRESH_DAYS=3、PROXY_RETRY_AFTER=600、SINGLE_ACCOUNT_RISK_COOLDOWN=120、WBI_KEY_FAIL_TTL=60、CRED_FAIL_TTL=300、REPLY_TREE_MAX_DEPTH=50、WEB_JOB_MAX_KEPT=100、ANALYZE_MAX_TARGETS=200、SPAM_BURST_WINDOW_SECONDS=10、SPAM_BURST_HIGH_COUNT=5、SPAM_BURST_MEDIUM_COUNT=3、SPAM_VARIANT_SIMILARITY=0.8、SPAM_VARIANT_MIN_COUNT=5、SPAM_BURST_MIN_COUNT=10、SPAM_BURST_MAX_INTERVAL=2。
