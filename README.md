# B站弹幕发送者用户画像分析系统

输入视频BV号，深度分析发送弹幕的用户都是什么样的人。

## 核心特性

- **弹幕采集**：获取视频全部弹幕，按发送者聚合
- **mid_hash 破解**：CRC32 反向搜索 + 评论区明文UID交叉验证
- **评论区交叉验证**：新用户UID过长无法暴力破解时，从评论区获取明文UID计算CRC32进行匹配
- **刷屏智能检测**：不删除、只标记，区分正常重复与恶意刷屏
- **四维度画像分析**：
  1. 用户主页信息（等级、大会员、硬币数、认证等）
  2. 互动内容足迹（投稿、动态、收藏夹、评论）
  3. 社交关系网络（关注列表、粉丝、互关）
  4. 行为模式分析（活跃时段、活跃周期、消费行为）
- **扫码登录**：B站APP扫码，Cookie自动保存复用
- **交互式HTML报告**：Chart.js图表 + 用户卡片 + 筛选功能
- **断点续采**：SQLite持久化，中断后可恢复

## 安装

```bash
cd bilibili_profiler
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 用法

```bash
# 分析视频
python run.py BV1vu4y1b7Y9

# 强制重新分析（清除该视频全部缓存并强制重采全部用户）
python run.py BV1vu4y1b7Y9 --force

# 限制最大分析用户数
python run.py BV1vu4y1b7Y9 --max-users 50
```

首次运行会提示扫码登录，请使用B站APP扫描终端显示的二维码。

## 断点续采机制

- 已解析的发送者（senders）与已采集的用户数据（users）持久化在 `data/profiler.db`。
- 阶段5（用户采集）每采完一人立即落库，Ctrl+C 中断后重跑会自动跳过已完成的解析与采集。
- 同一 UID 被多个弹幕 hash 命中时只采集一次；历史解析失败的发送者会在重跑时自动重试。
- `--force` 会清除该视频的 senders/videos 缓存、删除不再被其他视频引用的孤立 users 记录，并强制重采该视频的全部用户（users 为用户级缓存，重采结果会覆盖更新）。

## 输出

- **HTML报告**：`data/reports/report_{BV号}_{时间}.html`
- **数据库**：`data/profiler.db`（支持中断恢复）
- **Cookie**：`data/cookie.json`（自动管理登录态）

## 技术架构

```
src/
├── main.py              # 主控流程（6阶段流水线）
├── auth.py              # 扫码登录 + Cookie管理
├── api_client.py        # HTTP请求封装（限速、重试）
├── danmaku.py           # 弹幕XML解析 + 发送者聚合
├── comment.py           # 评论区采集 + UID映射
├── uid_resolver.py      # mid_hash破解 + 交叉验证引擎
├── spam_detector.py     # 刷屏智能检测
├── user_collector.py    # 用户深度数据采集（四维度）
├── profile_analyzer.py  # 画像分析 + 标签生成
├── report.py            # HTML报告生成器
├── storage.py           # SQLite持久化
└── config.py            # 配置常量
```

## mid_hash 解析策略

| 方法 | 适用场景 | 可靠性 |
|------|----------|--------|
| 评论区交叉验证 | 所有在评论区出现过UID的用户 | ⭐⭐⭐⭐⭐ |
| CRC32反向破解 | 2021年前老用户（UID < 5000万） | ⭐⭐⭐ |
| API存在性验证 | 所有破解结果 | ⭐⭐⭐⭐ |

## 刷屏判定标准

- **高风险**：大量重复内容 + 机器人式规律间隔
- **中风险**：中度重复或疑似机器人
- **低风险**：正常发言

系统**不删除**任何弹幕数据，仅在画像中标记风险等级。

## 注意事项

- B站API有风控限制，请求间隔已设置为0.6-1.0秒
- 大会员状态、收藏夹等部分数据需要登录后才能获取完整信息
- 用户关注列表等API可能需要登录态，未登录时可能返回有限数据
- 分析大量用户时耗时较长，程序支持Ctrl+C中断并恢复

## 免责声明

本项目仅供个人学习与研究用途。使用者应遵守 bilibili 用户协议与相关法律法规，不得将本工具用于任何侵犯他人隐私、批量爬取牟利或其他违法违规用途。使用本项目产生的一切后果由使用者自行承担。
