"""
通用 LLM 分析器 — 兼容所有 OpenAI 协议的 API

通过环境变量切换厂商，零代码改动:
    export LLM_API_KEY="sk-xxx"
    export LLM_BASE_URL="https://api.xiaomimimo.com/v1"
    export LLM_MODEL="mimo-v2.5-pro"
"""
import re
import json
from datetime import datetime
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_MAX_TOKENS


class LLMAnalyzer:
    """通用 LLM 分析器，兼容所有 OpenAI 协议 API"""

    def __init__(self):
        self.api_key = LLM_API_KEY
        self.base_url = LLM_BASE_URL
        self.model = LLM_MODEL
        self.max_tokens = LLM_MAX_TOKENS

    def _build_prompt(self, profiles: list[dict], video_info: dict) -> str:
        """为每个用户单独构建画像分析 prompt"""
        title = video_info.get("title", "未知视频")

        # 提取用户摘要数据
        users_data = []
        for p in profiles:
            dm = p.get("danmaku", {})
            users_data.append({
                "uid": p.get("uid"),
                "name": p.get("name", "未知"),
                "level": p.get("level", 0),
                "sex": p.get("sex", "保密"),
                "sign": p.get("sign", ""),
                "vip": p.get("vip_status", 0) == 1,
                "follower": p.get("follower", 0),
                "following_count": p.get("following", 0),
                "archive_count": p.get("archive_count", 0),
                "dynamic_count": p.get("dynamic", {}).get("count", 0),
                "bangumi_count": p.get("bangumi_count", 0),
                "bangumi_titles": p.get("bangumi_titles", []),
                "favorite_folders": [n for n in p.get("favorite", {}).get("names", [])],
                "followings_sample": p.get("following_analysis", {}).get("top_names", []),
                "following_summary": p.get("following_summary", {}),
                "danmaku_count": dm.get("count", 0),
                "danmaku_contents": dm.get("contents", [])[:30],  # 只取前30条，防止刷屏用户prompt超长
                "spam_level": dm.get("spam_level", "低"),
                "tags": p.get("tags", []),
            })

        prompt = f"""你是一位专业的网络行为分析师和人格心理学家。请对以下每位B站用户进行**个体深度画像分析**。

这些用户都曾在视频《{title}》中发送弹幕。

## 分析维度（每个用户单独分析）

1. **人格类型**：基于MBTI或Big Five框架，结合其弹幕内容、签名、收藏夹命名、关注偏好推断
2. **心理动机**：他/她为什么在这个视频发这些弹幕？想表达什么？
3. **社交需求**：在B站社区中扮演什么角色、寻求什么？
4. **消费偏好**：内容品味、付费意愿、使用习惯
5. **异常评估**：如果刷屏等级为"高"或"中"，分析其心理状态

## 数据说明
- B站Lv.6代表硬核老用户；大会员表示有持续付费意愿
- 关注列表中的UP主类型反映信息食谱和价值观
- following_summary 是该用户关注UP主的分析：分区分布(top_categories)、大UP/小UP比例(big_creators/small_creators)、活跃UP占比(active_ratio)
- 收藏夹命名反映性格特征；标签是系统辅助判断

## 用户数据（共{len(users_data)}人）
{json.dumps(users_data, ensure_ascii=False, indent=2)}

## 输出要求

请严格按以下格式输出，每个用户一个section，**每个用户不超过200字**：

### [uid] 用户名
人格类型: （1-2句）
心理动机: （1-2句）
社交需求: （1-2句）
消费偏好: （1-2句）
异常评估: （1句）

每个用户之间用 `---` 分隔。不要输出群体总结。务必覆盖所有用户，不要遗漏。"""
        return prompt

    def _parse_per_user(self, raw_text: str) -> dict[int, str]:
        """按 ### [uid] 分割响应，构建 uid → 分析文本 映射"""
        per_user = {}
        # 匹配 "### [uid]" 或 "### uid 用户名" 格式
        pattern = re.compile(
            r'###\s*\[?(\d+)\]?[^\n]*\n(.*?)(?=\n###\s*\[?\d+|\Z)',
            re.DOTALL
        )
        for match in pattern.finditer(raw_text):
            uid = int(match.group(1))
            text = match.group(2).strip()
            # 去除末尾的 --- 分隔符
            text = re.sub(r'\n*---\s*$', '', text)
            per_user[uid] = text

        return per_user

    def _analyze_batch(self, profiles: list[dict], video_info: dict) -> dict[int, str]:
        """分析一批用户，返回 {uid: analysis_text}"""
        prompt = self._build_prompt(profiles, video_info)

        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

        response = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=self.max_tokens,
            temperature=1.0,
            top_p=0.95,
            stream=False,
        )

        raw_text = response.choices[0].message.content or ""
        per_user = self._parse_per_user(raw_text)
        if not per_user:
            # 解析为空时打印原始响应前200字符，帮助诊断输出格式问题
            print(f"  警告: LLM响应解析为空，原始响应前200字符: {raw_text[:200]!r}")
        return per_user

    def analyze(self, profiles: list[dict], video_info: dict, batch_size: int = 10) -> dict:
        """调用 LLM，分批分析，返回 per_user 结果"""
        all_per_user = {}
        all_texts = []

        for i in range(0, len(profiles), batch_size):
            batch = profiles[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (len(profiles) + batch_size - 1) // batch_size
            print(f"  批次 {batch_num}/{total_batches}: 分析 {len(batch)} 人...")

            try:
                per_user = self._analyze_batch(batch, video_info)
            except Exception as e:
                # 失败降级：跳过该批次，不中断整体分析
                print(f"  警告: 批次 {batch_num} 分析失败（{e}），跳过该批次")
                continue
            all_per_user.update(per_user)
            all_texts.append(f"--- batch {batch_num} ---")
            for uid, text in per_user.items():
                all_texts.append(f"### [{uid}]\n{text}")

        return {
            "model": self.model,
            "timestamp": datetime.now().isoformat(),
            "full_text": "\n\n".join(all_texts),
            "per_user": all_per_user,
        }
