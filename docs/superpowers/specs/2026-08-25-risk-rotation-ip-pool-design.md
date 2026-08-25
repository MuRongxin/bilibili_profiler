# 风控轮换策略重构：账号×IP 组合池设计

日期：2026-08-25
状态：待用户审阅

## 背景与目标

现状：触发风控（-412 / HTTP 412 / 重签后仍 -352/-403）后，`api_client.py` 直接进 600 秒级全局长冷却再重试；阶段5 账号池只做"换号继续轮询"（2026-08-25 已从不剔除改为不剔除换号）。长冷却挡在重试链最前面，多账号无法形成采集加速。

目标：引入 Clash 订阅节点池作为 IP 池，风控时优先**换新号+新 IP** 继续任务，长冷却降级为最后手段；多账号+IP 组轮转实现采集加速。

硬性约束（用户明确要求）：

- **IP 池出问题时程序必须照常工作**：逐级降级，任何一层坏了退到下一层，流水线不中断。
- **最坏保底 = 单账号 + 单 IP**：即使只剩长冷却循环，任务也要能慢慢跑完；进程绝不因风控退出。

## 总体架构

```
run.py / web.py 后台 job
  └─ ComboPool（新，src/combo_pool.py）   编排层：账号轮转 + 节点切换 + 兜底冷却
       ├─ accounts: [("主号", BiliAPIClient), ("alt1", ...), ...]   复用 auth.load_extra_clients
       ├─ ClashCtl（新，src/clash_ctl.py）  Clash 控制器封装：列节点/切节点
       ├─ ProxyCore（新，src/proxy_core.py） 内置 mihomo 核心生命周期（订阅→本地代理端口+控制器）
       └─ BiliAPIClient（改，src/api_client.py）  支持代理；风控抛 RiskControlError 而非长冷却
```

职责划分：

- `BiliAPIClient`：发请求、限速、短退避、识别风控并**上报信号**（抛异常），不再自行长冷却。
- `ComboPool`：持有账号池与节点切换器，维护轮转游标与圈计数；`run(fn)` 执行一个任务单元，风控时换"新号+新IP"重试，兜底冷却。
- `ClashCtl`：纯封装 Clash 外部控制器 API，所有失败静默降级返回 False/空，不抛给上层。对内置核心与外部 ShellCrash 一视同仁。
- `ProxyCore`（新，`src/proxy_core.py`）：内置 mihomo 核心生命周期管理——定位/下载二进制、生成最小配置（订阅链接交给核心自己拉取解析）、拉起子进程、健康检查、退出清理。**使用者无需安装任何梯子工具，给一个或多个机场订阅链接即可获得 IP 池。**

## 组件设计

### 0. `src/proxy_core.py`（新增，内置 mihomo 核心）

职责：把"机场订阅 → 本地代理端口 + 控制器"这一整套 ShellCrash 功能集成进程序。

- 二进制定位顺序：`MIHOMO_PATH` 环境变量 → `vendor/mihomo`（用户自行放置）→ `data/mihomo`（首次运行时按平台自动从 GitHub Releases 下载，linux/amd64 等）→ 全部失败则打印指引并禁用 IP 池（降级链照常工作）。
- 配置生成：写出最小 yaml 到 `data/mihomo_runtime/`：
  - `mixed-port` / `external-controller` 绑定 `127.0.0.1` + 动态空闲端口（避免与已有 Clash 冲突）；
  - `external-controller-secret` 随机生成；
  - `proxy-providers`：每个订阅链接一个 provider（`type: http`），节点拉取/解析（base64 分享链或 Clash yaml）由 mihomo 完成，程序不解析订阅；
  - 一个 `select` 类型节点组（组名固定 `profiler`），`use` 引用全部 provider（多订阅节点汇入同一轮换组）+ 可选 `url-test` 自动组。
- 生命周期：ComboPool 激活时 `start()`（子进程 + 轮询控制器就绪）；`atexit`/流水线结束 `stop()`；核心崩溃 → 记日志并禁用 IP 池降级。
- 订阅链接是凭证：只走 gitignore 的 `config.py` 或环境变量，不落日志、不进仓库。

### 1. `src/clash_ctl.py`（新增）

配置（`config.py`，均支持环境变量覆盖，沿用 LLM_* 先例）：

- `SUB_URLS`（机场订阅链接列表，支持多个；内置核心的节点来源，环境变量 `SUB_URLS` 逗号分隔）
- 外部 Clash（可选）：`CLASH_ENABLED = False`、`CLASH_API_URL` / `CLASH_SECRET` / `CLASH_GROUP` / `CLASH_PROXY_URL`
- 内置核心参数：`MIHOMO_PATH`（覆盖二进制定位）、`CLASH_GROUP` 复用为组名（默认自动创建 `profiler` 组）

IP 池来源自动发现顺序（无需用户做任何配置时也能尽量工作）：

1. **外部控制器自动探测**：依次探测 `CLASH_API_URL`（若显式配置）及本机常见控制器端口（9090/9999/9097），可用即直接接管节点切换——**用户已有运行中的 Clash/ShellCrash 时什么都不用填，也不需要订阅链接**；
2. **`SUB_URLS` 内置核心**：拉起 ProxyCore；
3. 都不可用 → 无 IP 池，降级直连（账号轮转 + 长冷却兜底照常）。

接口：

- `available() -> bool`：控制器连通性自检（GET `/proxies`）。
- `list_nodes() -> list[str]`：组内节点名（过滤 DIRECT/REJECT 等伪节点）；失败返回 `[]`。
- `current_node() -> str | None`。
- `pick_next_node() -> str | None`：组内轮询游标推进，跳过当前节点；节点列表为空返回 None。
- `switch_node(name) -> bool`：PUT `/proxies/{group}` 切换；失败返回 False。

### 2. `src/api_client.py`（修改）

- 新增异常 `RiskControlError`（本模块定义，消息带触发码）。
- 风控处理改造（`get()` 内 -412 分支、HTTP 412 分支、重签后仍 -352/-403 分支）：
  - 保留**一次** 2 秒级短退避原地重试（防瞬时抖动）；
  - 仍失败 → `raise RiskControlError`；**删除 600s 冷却重试循环**与 `_risk_cooldown_until` 全局等待。
- 自适应降速倍率保留不动（撞风控 ×1.5、上限 5.0、成功 ×0.99 回落，按账号隔离）。
- 新增 `set_proxy(url: str | None)`：设置/清除 `session.proxies`。
- `post()`/`get_raw()` 同样把"重试耗尽"语义改为抛 `RiskControlError`（post 原本不做风控码处理，仅对齐网络层 412 语义；get_raw 沿用现有 raise 风格）。

### 3. `src/combo_pool.py`（新增）

```python
class ComboPool:
    def __init__(self, accounts: list[tuple[str, BiliAPIClient]], clash: ClashCtl | None): ...
    def current(self) -> tuple[str, BiliAPIClient]: ...
    def run(self, fn: Callable[[BiliAPIClient], T], unit_desc: str = "") -> T:
        """执行任务单元；风控→rotate()→重试；兜底见下"""
    def rotate(self, reason: str) -> None:
        """游标推进到下一账号 + 切下一节点（切节点失败仅换号并记日志）"""
```

`run()` 兜底链（核心语义）：

1. 风控 → `rotate()`（新号+新IP）→ 重试当前单元。
2. 每个账号记一次风控算一圈；**整圈撞满 → `sleep(RISK_COOLDOWN + random(0,60))` 长冷却**（最后手段），冷却完清零继续。
3. 连续 `MAX_RISK_ROUNDS = 2` 圈仍失败 → 放弃当前任务单元，向上抛 `RiskControlError`（由阶段层按"失败降级不中断"处理）。

代理故障处理（用户硬性约束）：

- **启动自检**：激活时经代理发一次轻量请求（NAV_URL 或视频信息接口）；失败 → 打印警告、全局摘代理，退化为直连+账号轮转。
- **运行中代理连接失败**（`requests.exceptions.ProxyError`/连不上代理端口，与 B 站风控区分）：不算任务失败、不消耗风控圈计数；先切节点重试，连续 3 次代理连接失败 → 判定 IP 池不可用，摘代理转直连继续，日志明确提示。
- **节点切换 API 失败**：仅换号不切 IP，记日志，功能不受阻。

降级链全景：

```
账号×IP 组合轮换
  └─ IP 池坏 → 账号轮转 + 直连
       └─ 账号全风控 → 600s 长冷却循环（单账号+单IP 保底也能跑完）
            └─ 连续 2 圈仍失败 → 跳过当前任务单元（流水线继续，进程不退出）
```

### 4. 各采集阶段接入（任务单元粒度，断点不丢）

手法统一：采集函数签名 `client` → `pool: ComboPool`，函数**内部**把每个请求单元用 `pool.run(lambda c: ..., desc)` 包裹——翻页游标（评论页码、历史弹幕日期循环）保留在函数内，换组合后从断点继续，不重跑已采部分。

- 阶段1 弹幕 `fetch_danmaku` / `collect_danmaku_data`：单请求单元，重跑成本 1 次请求。
- 历史弹幕 `fetch_history_danmaku`：按天循环，单元=一天。
- 阶段3 评论 `_fetch_comments_wbi` / `_fetch_sub_replies`：翻页循环，单元=一页。
- 阶段4 UID 解析 `resolve_all_senders`：按发送者循环，单元=一人（`verify_uid_exists`/`_batch_verify_uids` 批次）。
- 阶段5 用户采集 `phase_collect_users`：per-uid 循环改用 `pool.run(collect_user_data, ...)`，删除现有的"风控换号"手工分支（组合池统一接管；刚改的"不剔除"语义由池内游标天然继承）。
- 互动弹幕/充电名单（`fetch_command_dms`/`fetch_charge_uid_map`）：单请求单元。

### 5. 边界

- **登录/扫码/cookie 校验与刷新（auth.py 全部）：直连主号，不走代理、不轮换**（避免异地登录风控）。代理在 phase_login 完成后才由 ComboPool 统一挂载。
- `web.py` 手动分析后台 job 跑同一套采集函数，签名变更后自然接入组合池。
- LLM 调用（openai 客户端）不走代理池，维持现状。
- 单核心实例同一时刻只有一个出口 IP，节点切换是全局操作："账号×IP"为时间片轮换，非多账号各自独立 IP 并行（真并行需 mihomo listeners 按端口绑节点，后续扩展，不进本次范围）。
- 未配置任何 IP 池（无 `SUB_URLS`、外部控制器探测不到）：行为 = 账号轮转 + 直连 + 长冷却兜底，与现状基本一致。

## 配置汇总（config.py 新增）

| 常量 | 默认 | 说明 |
|---|---|---|
| `SUB_URLS` | `[]` | 机场订阅链接列表（支持多个）；内置核心的节点来源（环境变量 `SUB_URLS` 逗号分隔） |
| `MIHOMO_PATH` | `""` | 覆盖内置核心二进制定位 |
| `CLASH_ENABLED` | `False` | 显式指定使用外部 Clash/ShellCrash |
| `CLASH_API_URL` | `http://127.0.0.1:9090` | 外部控制器地址（内置核心时自动填动态端口） |
| `CLASH_SECRET` | `""` | 控制器 secret（内置核心随机生成） |
| `CLASH_GROUP` | `""` | 轮换节点组名（内置核心默认 `profiler`） |
| `CLASH_PROXY_URL` | `http://127.0.0.1:7890` | 混合代理端口（内置核心时自动填动态端口） |
| `MAX_RISK_ROUNDS` | `2` | 单任务单元允许的兜底冷却圈数 |

## 测试与验证

- 项目无单测框架：所有改动过 `py_compile`；`python quick_test.py` 冒烟验证**无代理环境**（默认不配 `SUB_URLS`）行为不回归。
- `clash_ctl.py` 自检：`python -c "from clash_ctl import ClashCtl; ..."` 打印当前节点并试切换。
- 内置核心自检：配 `SUB_URLS` 后 `python -c "from proxy_core import ProxyCore; ..."` 拉起核心、打印节点数与出口 IP、退出清理。
- 有条件时两小号+节点池实跑 `run.py`，观察日志：风控出现"换号+切节点"而非"冷却600秒"。

## 前置条件（用户侧，三档自动择优，无需手工选择）

1. **已有运行中的 Clash/ShellCrash（最省事）**：程序自动探测本机控制器端口（9090/9999/9097），可用即直接接管，**无需订阅链接、无需任何配置**。ShellCrash 建议在「模式设置 → 流量劫持范围」选 `4 不配置流量劫持（纯净模式）`，其它应用流量不受影响。确认方法：`curl -x http://127.0.0.1:<mix_port> https://api.ipify.org` 返回节点 IP，直接 `curl https://api.ipify.org` 仍返回本机真实 IP。
2. **只给订阅链接（零安装）**：在 `config.py` 或环境变量填 `SUB_URLS`（支持多个），程序自动下载/拉起内置 mihomo 核心，只监听 127.0.0.1 随机端口，其它应用流量完全不经过它。
3. **什么都不配**：无 IP 池，行为同现状（账号轮转 + 长冷却兜底）。
