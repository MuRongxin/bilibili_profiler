# TODO —— 


## 1 Queery:
- From the output of the console, why was the IP proxy removed when the program started without finding a usable node
  ```log
  [ProxyCore] 内置核心就绪: 46 个节点（混合端口 127.0.0.1:52825）
  [Pool] 自检节点不通，切换到 [🇭🇰HKG-香港01] 重试
  [Pool] 自检节点不通，切换到 [🇲🇴OMA-澳门01] 重试
  [Pool] 自检节点不通，切换到 [🇹🇭TH-泰国01] 重试
  [Pool] 自检节点不通，切换到 [🇹🇼TWN-台湾01-家宽] 重试
  [Pool] 代理自检失败，摘代理转直连（仅账号轮转）
  ```
  
- The current design is: to collect 100 pages of comments and gather plaintext Uids to a maximum of 500 pages. However, from the logs, why does the UID collection start from 1?(The ending is not 500. It might be that there are no 500 pages in total)

  ```log
  [Comment] 已达采集上限 100 页，评论区可能未采完（可调大 MAX_COMMENT_PAGES）
  [Comment] 获取到 8968 条评论（含子评论 6972 条），提取 4876 个独立用户UID，4875   个IP属地
  [Comment] UID收割第 2/500 页: +1（本轮累计 1 个）
  [Comment] UID收割第 5/500 页: +1（本轮累计 2 个）  
  ....
  [Comment] UID收割第 114/500 页: +1（本轮累计 219 个）
  [Comment] UID收割完成：本轮新收 219 个 uid 映射
  [Comment] 充电名单: 1 个明文UID
  ```
- Where are the analysis results of the LLM's in-depth user profiling? I haven't seen them yet. Are they on the user cards? But there is only the collected information above. (I found the target user card based on the LLM analysis logs. )
## Improvement:
- Home page:The section on "跨视频重叠用户", Add the release date of this video after its title. The column "涉及视频数" can be removed. "在 ≥ 2 个已分析视频中都发过弹幕的发送者—跨视频重复出现的账号是水军/带节奏的重点嫌疑对象。点视频条目可展开 TA 在该视频里的弹幕/评论明细。", this sentence can be removied.
- On the specific video analysis report page:
  - on the "Overview page",There is no gap between the "弹幕密度时间轴" card and the three cards of “弹幕密度时间轴” 这个卡片与“用户等级分布” “刷屏风险分布” and “用户标签 Top10“ . Merge the contents of the "Top10 Geographical Distribution" and "用户标签 Top10" cards into one card .
  
  - 在用户画像的卡片上会收集用户的关注列表，为了了解它所关注的UP主是什么样子的，设计了根据该人投稿标题生成词云的功能，现在这个收集过程改为了懒加载，只有在用户把鼠标移动到这个up的名字词条上时，才会收集，目前的问题是，鼠标悬停时，收集、加载词云的过程太慢了，需要改进。（根本没有这个项目生成词云的速度快：https://github.com/gaogaotiantian/biliscope/,如果你搞不定，可以参考一下） 
  再增加一个功能点，点击这个UP主的词条，可以新页面跳转该UP的主页；

- 对于需要深度采集的用户，现在的做法是 把号池里面的账户都用上，多线并发同时进行多个账号的采集，但是这导致log输出变得很混乱；在保持精细度的同时需要改进；
  ```log
  [29/247] 采集 UID:471579363...
  [Collect] UID:471579363 开始采集...
  [Collect] UID:1841627262 维度1(主页/收藏)完成，采集互动足迹...
  [Collect] UID:471579363 维度1(主页/收藏)完成，采集互动足迹...
  [Collect] UID:1841627262 维度2(互动足迹)完成，采集社交关系...
  [Collect] UID:471579363 维度2(互动足迹)完成，采集社交关系...
  [Collect] UID:471579363 维度3(社交关系)完成，分析关注偏好/行为模式...
  [Collect] UID:471579363 YouTube精选智慧- Lv.6 采集完成
  ```


## Feature:
- 