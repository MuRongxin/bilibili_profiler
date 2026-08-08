# B站非公开 API 调研文档（2026-08-03）

> **用途：** 本项目的接口参考手册。整理自 2026-08-03 的专项调研，来源为 bilibili-API-collect 归档前快照及相关 issue/工具仓库。
>
> **时效与可靠性声明：**
> - [SocialSisterYi/bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect) 于 **2026-01-28 收到 B站委托律所律师函**，2026-01-30 永久归档停更并删除文档。本文基于其归档前快照（fork 镜像 [pskdje/bilibili-API-collect](https://github.com/pskdje/bilibili-API-collect)，2026-01-24/25 与上游同步）。**此后接口变动不再有社区文档跟踪，需自行监控。**
> - 文中所有结论均来自文档与公开讨论，**未经实网验证**。标注规则：**[已证实]** = 有社区文档/代码实证；**[未验证]** = 单一来源或时效存疑。任何接口集成前必须先小规模实测。
> - 合规提醒：本项目属"非公开 API 采集"同类行为，存在法律与账号风险，仅限个人学习研究用途。

---

## 目录

1. [通用签名与风控](#1-通用签名与风控)
2. [弹幕接口](#2-弹幕接口)
3. [评论接口](#3-评论接口)
4. [用户空间接口](#4-用户空间接口)
5. [搜索与批量接口](#5-搜索与批量接口)
6. [mid_hash 机制](#6-mid_hash-机制)
7. [错误码速查](#7-错误码速查)

---

## 1. 通用签名与风控

### 1.1 WBI 签名 [已证实]

来源：[wbi.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/misc/sign/wbi.md)（含 2025 年更新）

- 从 `nav` 接口（或 bili_ticket 接口）响应的 `wbi_img.img_url` / `sub_url` 文件名取 `img_key` / `sub_key`。
- `img_key + sub_key` 经 64 项 `MIXIN_KEY_ENC_TAB` 重排取前 32 字符得 `mixin_key`。
- 参数加 `wts`（秒级时间戳）后：按 key 升序 → **过滤 value 中 `!'()*` 字符** → urlencode（**百分号编码字母大写、空格用 `%20` 而非 `+`**）→ 拼接 `mixin_key` → MD5 得 `w_rid`。
- **`img_key/sub_key` 全站统一、每日更替**，必须缓存 + 定时刷新；签名失效（收到 -352 或评论接口 -403 + `v_voucher`）时强制刷新重签。
- 本项目已知 bug：重签时若 params 残留旧 `w_rid` 会拼进待签名串导致新签名必然无效（`src/api_client.py:114-117`，待修）。
- 特殊要求：`x/space/wbi/acc/info` 额外要求 **Cookie 总项数 ≥ 3**（含 SESSDATA，可凑空值项）。
- `x/space/wbi/arc/search` 2023-11 起额外要求 `dm_img_list`、`dm_img_str`、`dm_cover_img_str` 三个 WebGL 指纹参数，缺失报 -352（[issue #868](https://github.com/SocialSisterYi/bilibili-API-collect/issues/868)）。可用固定值 `dm_img_list=[]&dm_img_str=V2ViR0wgMQ==`（base64("WebGL 1")）。

### 1.2 buvid3 / b_nut [已证实]

来源：[buvid3_4.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/misc/buvid3_4.md)

- 获取方式一：`GET https://www.bilibili.com/`，从 `Set-Cookie` 取 `buvid3` / `b_nut`。
- 获取方式二：`GET /x/frontend/finger/spi`，返回 `b_3`（buvid3）/ `b_4`。
- 搜索等接口 2022-08 起强制要求 buvid3，缺失直接 -412。
- 注意：主页对含 `python`/`curl` 等子串的 UA **不下发 buvid3**。

### 1.3 bili_ticket [已证实]

来源：[bili_ticket.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/misc/sign/bili_ticket.md)

- `POST /bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket`
- 参数 `hexsign = hmac_sha256(key="XgwSnGZ1p", msg="ts" + timestamp)`
- 返回 JWT 形式 ticket，**有效期 3 天**，放入 Cookie。
- "非必需，但存在可降低风控概率"（文档原话）。

### 1.4 风控触发面与缓解 [已证实]

来源：[errcode.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/misc/errcode.md) 及多个 issue

触发面：
- UA 含 `python` / `curl` / `awa` 等敏感子串（多个接口明文校验）
- Cookie 不全（如缺 buvid3）
- `Referer` 非 bilibili.com 子域
- 高频 / 短间隔请求
- 缺 `dm_img_*` 指纹参数（arc/search）

缓解实践：
- 完整浏览器 UA + `Referer: https://www.bilibili.com/`
- 启动时初始化 buvid3 / b_nut / bili_ticket
- 遇 -412 进入**长冷却**（社区经验：数分钟~数小时，并更换出口 IP/会话），不要短退避后立即重试

### 1.5 exClimbWuzhi [未验证]

`x/internal/gaia-gateway/ExClimbWuzhi`，上传设备指纹（Fingerprint2 V18）"激活" buvid 的风控上报接口，[issue #933](https://github.com/SocialSisterYi/bilibili-API-collect/issues/933)（2024-01-10）有逆向细节。实现复杂、字段未完全公开。本项目暂不采用，仅记录。

---

## 2. 弹幕接口

### 2.1 实时弹幕

| 接口 | 说明 | 来源 |
|---|---|---|
| `GET /x/v1/dm/list.so?oid={cid}` | 旧版 XML 实时池，上限约 1500–3000 条 | [danmaku.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/danmaku/danmaku.md) |
| `GET /x/v2/dm/web/seg.so` | protobuf 实时池，上限约为 XML 的 2 倍 | [danmaku_proto.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/danmaku/danmaku_proto.md) |
| `GET /x/v2/dm/list/seg.so` | APP 端 protobuf 实时池 | 同上 |
| `GET /x/v2/dm/wbi/web/seg.so` | web 新版，需 wbi 签名，字段不变 | 同上 |

protobuf 分段参数：`type=1`（视频弹幕）、`oid=cid`（必要）、`pid=avid`（非必要）、`segment_index`（必要，**每 6 分钟一包**，第 1 包覆盖 progress 0–360000ms），每包最多 **6000 条**。

`DanmakuElem` 字段含：`id`（dmid）、`progress`（视频内毫秒）、`mode`、`fontsize`、`color`、`midHash`（8 位 hex）、`content`、`ctime`、`weight`（0–10 智能屏蔽权重）等。

### 2.2 历史弹幕（弹幕池快照）[已证实 + 2026-08-03 本项目实测修正]

来源：[history.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/danmaku/history.md)

- `GET /x/v2/dm/history/index?type=1&oid={cid}&month=YYYY-MM` → 该月有弹幕的日期列表（**需登录**）
- `GET /x/v2/dm/web/history/seg.so?type=1&oid={cid}&date=YYYY-MM-DD` → 弹幕池快照 protobuf（**需 SESSDATA**）

**⚠️ 本项目 2026-08-03 实测修正（与社区文档"该日全量弹幕"的描述不同）：**
- seg.so 的真实语义是**"截至该日期的最新 1000 条弹幕池快照"**，不是"该日发送的弹幕"——返回弹幕的 ctime 可显著早于请求日期（实测请求 2024-09-15 返回 ctime 为 2024-05 的弹幕）。
- **每日上限 1000 条**；相邻日快照可能零重叠（热门期池子整天滚动）或大量重叠（平静期）。
- 逐日遍历 + 按 dmid 去重可逼近全量历史，但热门日期快照间滚出的弹幕仍会丢失。
- 历史接口的 `midHash` **省略前导零**（6-8 字符不等），需 `zfill(8)` 后才能与实时池格式对齐。
- `weight`/`pool` 字段不下发（恒 0）。

解析提示：proto 消息为 `bilibili.community.service.dm.v1.DmSegMobileReply`，纯 Python 可手写 wire 格式解析（免 protoc 编译），实现见本项目 `src/danmaku_history.py`。

### 2.3 弹幕元信息 dm/web/view [已证实]

来源：[danmaku_view_proto.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/danmaku/danmaku_view_proto.md)

`GET /x/v2/dm/web/view?type=1&oid={cid}&pid={aid}`（需 Cookie），返回：
- 弹幕开放状态、**实际弹幕总数 `count`**（有 1500–6000 上限，可用于评估实时池截断率）
- BAS/代码弹幕专包 URL
- **互动弹幕 `commandDms`——直接含明文 `mid`**（仅 UP主头像弹幕/关联视频/关注按钮三类，量少）
- 个人弹幕配置

### 2.4 弹幕点赞 [已证实]

来源：[thumbup.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/danmaku/thumbup.md)

弹幕级点赞数查询，可作为弹幕质量/刷屏权重的新维度。

---

## 3. 评论接口

来源：[comment/list.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/comment/list.md)（示例含 2024-08 时间戳）

### 3.1 主评论列表 [已证实]

- **现行接口**：`GET /x/v2/reply/wbi/main`（旧 `/x/v2/reply/main` 已弃用），**需 wbi 签名**。
- **cursor 分页**：响应 `data.cursor.pagination_reply.next_offset` 是"字符串皮 JSON"（如 `{"type":3,"direction":1,"Data":{"cursor":71859}}`）；下一页将其放入 URL 参数 `pagination_str={"offset":"<next_offset>"}`；`data.cursor.is_end` 判断终止。旧 `next` 参数已弃用。
- **⚠️ 本项目 2026-08-08 实测修正**：`next_offset` 实测为不透明 base64 串，且**可能连续多页完全相同但每页内容不同**——不能用"游标重复"做终止条件，否则会在第 2 页误杀翻页。可靠的终止条件：`is_end`、空 replies 页、或"整页 rpid 无新增"（真重复页检测）。
- 排序 `mode`：0/3 仅热度、1 热度+时间、2 仅时间。
- 返回条目含 **IP 属地**（`reply_control.location`）。
- 旧翻页接口 `/x/v2/reply`（`pn`/`ps`，ps≤20）文档中仍存在，需登录或 APP token。

### 3.2 子评论（楼中楼）[已证实]

- `GET /x/v2/reply/reply`，**仍是 `pn`/`ps` 分页**（非 cursor）。
- 必要参数：`type`、`oid`、`root={rpid}`。
- `ps` 定义域 1–49，**但实际每页最多只返回 20 条**；用 `data.page.count`（二级评论总数）做终止判断。

### 3.3 校验与限流

- 评论系接口对 UA 敏感子串（`python`、`curl`）与 Cookie 完备性敏感。
- wbi 签错返回 **-403**（其他接口多为 -352）。
- 未见公开的评论接口限流数值 [未验证]。

---

## 4. 用户空间接口

### 4.1 账号信息 [已证实]

来源：[user/info.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/user/info.md)

- **现行**：`GET /x/space/wbi/acc/info?mid={uid}` —— 必须 wbi + 登录 Cookie（≥3 项）。旧 `acc/info` 已废弃。
- `jointime` / `moral` 字段恒 0（已失效）。

### 4.2 批量名片（本项目未用上的关键接口）[已证实]

- `GET /x/polymer/pc-electron/v1/user/cards?uids={逗号分隔}`
- **一次最多 50 人，仅需登录 Cookie、无需 wbi**
- 返回昵称/头像/认证/大会员状态
- 用途：大批量发送者的基础信息打底，仅深度画像用户再调 4.1

### 4.3 关注/粉丝列表 [已证实]

来源：[relation.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/user/relation.md)

- `GET /x/relation/followings?vmid={uid}`：需登录 + Referer 为 bilibili.com 子域 + UA 不含 `python`；**看他人仅限前 100 个**（超过返回空列表但 code=0），自己可看全部；隐私设置时返回 22115。
- `GET /x/relation/fans`（新）：同上校验；**他人仅前 100、自己前 1000**；支持 `offset` 游标。
- ⚠️ 本项目当前 `MAX_FOLLOWING_PAGES=50`（即假设可采 1000 人）的假设是错的。

### 4.4 用户投稿 [已证实]

- `GET /x/space/wbi/arc/search?mid={uid}`：wbi 签名，2023-11 起需 `dm_img_*` 指纹参数（见 1.1）。
- **推荐替代**：`GET /x/series/recArchivesByKeywords?mid={uid}&keywords=` —— 文档注明**"暂未发现风控校验"**；keywords 为空取全部；`ps` 最大 100；`pn=0` 时忽略 ps 全量返回。来源：[collection.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/video/collection.md)

### 4.5 收藏夹 [已证实]

来源：[fav/list.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/fav/list.md)

- 现行为 `GET /x/v3/fav/resource/list`（Cookie/APP，按收藏夹 id 翻页）；旧 `space/fav/arc` 已不在文档中。

### 4.6 空间动态 [已证实]

来源：[dynamic/space.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/dynamic/space.md)

- `GET /x/polymer/web-dynamic/v1/feed/space?host_mid={uid}`
- **登录只需 SESSDATA**；未登录需 buvid3 + wbi + `dm_img` 系列指纹且"存在运气成分"（文档原话，[issue #686](https://github.com/SocialSisterYi/bilibili-API-collect/issues/686)）。

---

## 5. 搜索与批量接口

### 5.1 昵称 → UID 批量转换 [已证实]

来源：[batch.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/user/batch.md)

- `GET /x/polymer/web-dynamic/v1/name-to-uid` 批量昵称→UID。

### 5.2 用户搜索 [已证实]

来源：[search_request.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/search/search_request.md)

- `GET /x/web-interface/search/type?search_type=bili_user&keyword=` 按昵称模糊搜用户（需 wbi + buvid3）。

### 5.3 免登录旁路 [未验证]

`line3-h5-mobile-api.biligame.com/game/center/h5/user/relationship/follower_list`（无需认证、返回前 100 粉丝）等 H5 游戏中心接口，文档有记载但可用性与时效未验证。

---

## 6. mid_hash 机制

- **[已证实]** mid_hash 仍是 `CRC32(mid)` 的 8 位小写 hex。归档前最新文档写明 midHash"用于屏蔽用户和查看用户发送的所有弹幕，**也可反查用户id**"。第三方工具 [bilibili-danmaku-tracker](https://github.com/qianjiachun/bilibili-danmaku-tracker)（changelog 更新至 2026 年）仍在用 CRC32 反查，机制未变。
- **[已证实]** 没有官方/半官方 hash 反查接口。反查只有两条路：CRC32 暴力/彩虹表、评论区等明文 UID 交叉验证。
- **[未验证]** 新注册长 UID（16 位）无法暴力反推：[GetDanmuSender](https://github.com/cwuom/GetDanmuSender) README 声明"16 位 mid 以及超过 10 位以上的 mid 被加密后都无法正常反推，8、9 位 UID 基本正确"（2023 年）。数学上合理（搜索空间超 2^32 且碰撞增多），但无更多独立验证。
- **CRC32 碰撞提醒（本项目实测）**：32 位 CRC 空间内碰撞必然存在，暴力破解按前缀升序返回第一个碰撞者而非真实 UID（例：`CRC32("1")` 的破解结果为 1146140827）。暴力路径结果必须标注歧义、压低置信度。
- **彩虹表方案**：预生成 UID 0–5000 万的 `crc32 → uid` 映射落盘（定长二进制，约 400MB 内，numpy 向量化构建分钟级），查询 mmap O(1)。>10 位 UID 直接标记不可破跳过。

---

## 7. 错误码速查

来源：[errcode.md](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/misc/errcode.md)

| 错误码 | 含义 | 应对 |
|---|---|---|
| 0 | 成功 | — |
| -352 | 风控校验失败（UA 或 wbi 参数不合法），伴随 `v_voucher` | 检查 UA/签名/指纹参数，刷新 wbi 密钥 |
| -403 | 评论 wbi 接口签名错误 | 刷新 wbi 密钥重签 |
| -412 | 请求被拦截（IP 被风控） | 长冷却（≥10 分钟），轮换 buvid/会话 |
| -101 | 未登录 / Cookie 失效 | 刷新或重扫码登录 |
| 22115 | 用户隐私设置禁止查看 | 降级跳过 |
| 86038 | 扫码登录二维码已过期 | 重新生成二维码 |

---

## 附：主要参考来源

- bilibili-API-collect 归档前快照镜像：https://github.com/pskdje/bilibili-API-collect （2026-01-24/25 与上游同步；上游已于 2026-01-30 归档删除）
- bilibili-danmaku-tracker：https://github.com/qianjiachun/bilibili-danmaku-tracker
- GetDanmuSender：https://github.com/cwuom/GetDanmuSender
- 关联 issue：#686（动态接口）、#868（arc/search 指纹参数）、#933（exClimbWuzhi）
