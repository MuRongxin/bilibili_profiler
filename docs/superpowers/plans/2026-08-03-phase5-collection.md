# 阶段 5：采集能力增强 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 弹幕覆盖从"实时池最近几千条"扩展到"视频全周期历史弹幕"；评论接口迁移新版并补采子评论；用户空间接口现代化降请求量。

**上游文档：** 路线图阶段 5；`docs/bilibili_api_reference.md`（§2 弹幕、§3 评论、§4 用户空间——含全部接口细节与来源）。

**铁律（路线图）：** 每个新接口**先单接口小规模实测**（用项目 client + 现有 cookie 真实请求 1-2 次，确认字段结构）**再集成**。实测必须走 `BiliAPIClient`（限速硬约束）。真实 cookie 在 `data/cookie.json`，经 `auth.get_auth_client()` 或手动 load_cookie + update_cookies 构造。

**测试约定：** 无 pytest；离线逻辑用 python -c 断言；接口实测脚本写临时文件跑完删除；单任务单 commit。

---

### Task 1: danmaku_history.py 历史弹幕采集（含 protobuf wire 手写解析）

**Files:**
- Create: `src/danmaku_history.py`
- Modify: `src/config.py`（gitignore 不提交）

**接口规格（调研实证，docs/bilibili_api_reference.md §2.2/§2.1）：**
- `GET https://api.bilibili.com/x/v2/dm/history/index?type=1&oid={cid}&month=YYYY-MM` → `data` 为有弹幕的日期字符串列表（需登录）
- `GET https://api.bilibili.com/x/v2/dm/web/history/seg.so?type=1&oid={cid}&date=YYYY-MM-DD` → 该日全量弹幕 protobuf（需 SESSDATA）
- protobuf 结构：`DmSegMobileReply { repeated DanmakuElem elems = 1; }`；`DanmakuElem`：id=1(varint), progress=2(varint), mode=3, fontsize=4, color=5, midHash=6(string), content=7(string), ctime=8(varint), weight=9, pool=11, idStr=12(string)。wire 手写解析：reply 的 field 1 是 length-delimited 嵌套消息；elem 内 varint 字段与 length-delimited 字符串字段。

**config.py 新增：**
```python
HISTORY_DANMAKU_ENABLED = True   # 是否采集全量历史弹幕（需登录）
HISTORY_MAX_MONTHS = 24          # 历史弹幕最多回溯月数（防止上古视频请求爆炸）
HISTORY_MAX_DAYS = 400           # 历史弹幕最多采集天数（逐日接口，每日1次请求）
```

**模块接口：**
```python
def parse_danmaku_proto(data: bytes) -> list[dict]:
    """手写 wire 解析 DmSegMobileReply，返回与 danmaku.py parse_danmaku_xml 同构的 dict 列表
    （mid_hash/content/time(progress毫秒转秒)/mode/fontsize/color/timestamp(ctime)/pool/dmid/weight）"""

def fetch_history_danmaku(cid: int, client, pubdate: int = None) -> list[dict]:
    """采集视频全周期历史弹幕。
    从 pubdate（视频发布日，缺省则从今天回溯）起按月调 history/index 收集日期，
    逐日调 history/seg.so 拉取解析合并。受 HISTORY_MAX_MONTHS/HISTORY_MAX_DAYS 限制。
    单日失败打印警告跳过（降级不中断）。全程走 client（get/get_raw）。"""
```

**Step 1:** 先实测：用项目 client 对 BV1vu4y1b7Y9（cid 可用 danmaku.py 现有逻辑获取）调一次 history/index 和一个日期的 seg.so，打印响应结构确认规格。
**Step 2:** 实现 wire 解析器（用实测拿到的真实 seg.so 字节做离线断言：解析条数 > 0、字段完整、mid_hash 为 8 位 hex）。
**Step 3:** 实现 fetch_history_danmaku（月列表 → 日列表 → 逐日拉取，带进度打印）。
**Step 4:** 实测集成：对该视频跑一遍，打印历史弹幕总数（预期 ≫ 实时池 2660 条）。

**提交：** `feat: 全量历史弹幕protobuf采集与wire手写解析`

---

### Task 2: main.py 集成历史弹幕（与实时池合并去重）

**Files:**
- Modify: `src/main.py`（phase_danmaku 或 run_analysis 阶段2）

**Step 1:** 阶段2 实时弹幕采集后，若 `HISTORY_DANMAKU_ENABLED` 且登录态正常：调 `fetch_history_danmaku(cid, client, pubdate)`，与实时弹幕按 `dmid` 合并去重（dmid=0 的保留，不丢数据——"不删除数据"约定：合并只在内容池层面去重，sender_groups 重新聚合）。失败打印警告降级为仅实时池。
**Step 2:** 打印覆盖统计：`实时池 X 条 + 历史 Y 条，去重后 Z 条（覆盖率提升 N 倍）`。
**Step 3:** 历史弹幕的 sender_groups 与实时池合并（同一 mid_hash 的弹幕数累加、内容合并——读 danmaku.py 的 group_by_sender 复用）。

**验证：** 对 BV1vu4y1b7Y9 实测：合并后总数与去重逻辑正确（同一 dmid 只出现一次）；HISTORY_DANMAKU_ENABLED=False 时行为与之前一致。

**提交：** `feat: 主流水线集成历史弹幕与实时池合并去重`

---

### Task 3: comment.py 迁移 wbi/main + 子评论补采 + IP 属地

**Files:**
- Modify: `src/comment.py`
- Modify: `src/config.py`（gitignore 不提交）

**接口规格（调研实证，docs §3）：**
- 主评论：`GET /x/v2/reply/wbi/main?oid={aid}&type=1&mode=3&pagination_str={"offset":""}` 需 wbi 签名（项目 client 的 `_is_wbi_api` 认 `/wbi/` 路径，自动签名）。翻页：响应 `data.cursor.pagination_reply.next_offset`（字符串皮 JSON）→ 下一页 `pagination_str={"offset":"<next_offset>"}`；`data.cursor.is_end` 终止。
- 子评论：`GET /x/v2/reply/reply?type=1&oid={aid}&root={rpid}&pn=N&ps=20`，每页实际最多 20 条，`data.page.count` 终止判断。
- IP 属地：`replies[].reply_control.location`（如 "IP属地：江苏"）。

**config.py 新增：**
```python
COMMENT_REPLY_MAX_PAGES = 5   # 每条主评论的子评论最多补采页数（控制请求量）
```

**Step 1:** 实测 wbi/main 单页（用项目 client.get，确认 wbi 签名通过、cursor 结构）。
**Step 2:** 主评论采集迁移 wbi/main 游标翻页（保留 MAX_COMMENT_PAGES 上限）；提取 `reply_control.location` 存入评论记录。
**Step 3:** 子评论补采：`rcount > len(replies)` 的主评论调 reply/reply 按 pn 翻完（上限 COMMENT_REPLY_MAX_PAGES），子评论同样提取 UID（扩大 UID→CRC32 交叉验证样本）与 location。
**Step 4:** 评论记录结构加 `location` 字段；`build_comment_uid_map` 同时输出 `uid → location` 映射（供画像地域维度，阶段6报告用——本任务只把数据带出来，main.py 传递在 Task 5 集成）。
**Step 5:** 实测：BV1vu4y1b7Y9 评论采集，对比迁移前后独立 UID 数（预期显著增加，当前约 688）。

**提交：** `feat: 评论迁移wbi/main游标接口并补采子评论与IP属地`

---

### Task 4: user_collector 接口现代化

**Files:**
- Modify: `src/user_collector.py`

**接口规格（调研实证，docs §4）：**
- 批量名片：`GET /x/polymer/pc-electron/v1/user/cards?uids={逗号分隔,≤50}` 仅需登录无需 wbi → 昵称/头像/认证/大会员
- 用户投稿替代：`GET /x/series/recArchivesByKeywords?mid={uid}&keywords=&ps=100&pn=1`（文档注明暂无风控；pn=0 全量）
- acc/info 现行：`GET /x/space/wbi/acc/info?mid={uid}` 需 wbi + Cookie≥3 项
- 收藏夹现行：`GET /x/v3/fav/resource/list?media_id={folder_id}&pn=N&ps=20`

**Step 1:** 逐项实测（每项 1-2 次真实请求确认结构与鉴权）：cards、recArchivesByKeywords、wbi/acc/info（当前 user_collector 用的是哪个端点？读代码确认是否需要迁移）、fav/resource/list。
**Step 2:** 投稿列表从 `space/wbi/arc/search`（需 dm_img 指纹，易 -352）迁到 recArchivesByKeywords。
**Step 3:** acc/info 若用的是旧端点则迁 wbi 版（项目 client 自动签名）；确认 cookie ≥3 项。
**Step 4:** 收藏夹内容接口若现用 space/fav/arc 则迁 x/v3/fav/resource/list。
**Step 5:** 批量名片暂不改变采集主流程（深度采集仍需 acc/info），但新增 `get_user_cards_batch(uids, client)` 函数备用（阶段6/后续用于轻量画像）。**若实测发现某接口已失效/结构不符，报告 DONE_WITH_CONCERNS 并保留旧路径。**

**提交：** `feat: 用户空间接口现代化迁移降低风控暴露`

---

### Task 5: 集成与画像数据贯通

**Files:**
- Modify: `src/main.py`、`src/user_collector.py`

**Step 1:** main.py 把评论的 `uid→location` 映射传入用户采集/画像：resolved sender 的 uid 若有 location，注入 user_data（如 `user_data["ip_location"]`），profile_analyzer 透传到 profile（供阶段6报告展示）。
**Step 2:** profile_analyzer.py：profile 加 `ip_location` 字段透传。

**验证：** mock 数据贯通断言；`import main` 正常。

**提交：** `feat: 评论IP属地贯通用户画像`

---

## Self-Review 记录

- 执行顺序：1 → 2 → 3 → 4 → 5（Task 2 依赖 1，Task 5 依赖 3）。
- Spec 覆盖：路线图 5.1（Task 1-2）、5.2（Task 3）、5.3（Task 4，关注列表 100 上限已在阶段4适配；动态接口确认现行——user_collector 已在用 feed/space 的话无需动，Task 4 实测时确认）、地域贯通（Task 5）。
- 报告展示（覆盖率、地域图）属阶段6。
- 每个 Task 的实测步骤受限速约束，单项实测约 1-3 次请求，可接受。

## 全部任务完成后（控制器执行）

- [ ] `python quick_test.py --top 3` 冒烟（重点观察：历史弹幕量、UID 解析率变化）
- [ ] 整体终审（base=main）→ 合并
