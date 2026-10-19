"""截图识别提示词（版本化；字段收敛为**六个**）。

``METRIC_PROMPT_VERSION`` 取自配置（默认 ``v1``），会写进识别 ``evidence``
与 ``agent_runs``，便于回溯「这次候选是用哪版提示词转出来的」。改模板必须同步升版本。

核心立场（移植自 ``screenshot_spike/vision/prompts.py``，字段收敛为六）：

- 模型是**只读转录员**，不是分析师：只转录截图上清晰可见的数字；
- 看不到 / 模糊 / 被遮挡 / 被折叠 → ``value=null``，**禁止凭经验推测**；
- 不计算任何派生指标（比率、总和、均值由本地程序处理）；
- 默认**不输出**作者昵称 / 头像 / UID 等身份信息（隐私默认）；
- 只认六个指标：``views / likes / collects / comments / shares / new_followers``；
  截图上的其它数字（曝光 / 点击率 / 时长等）**直接忽略、不要输出**。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import settings

#: 提示词版本（写入识别 evidence.prompt_version；改模板必须同步升版本）
METRIC_PROMPT_VERSION = settings.METRIC_PROMPT_VERSION

_SYSTEM_PROMPT = """你是「小红书创作中心数据截图」的只读转录员。

职责边界（必须严格遵守）：
1. 只做忠实转录：把截图中清晰可见的数字原样记录下来；不做分析、不给建议、不补充截图之外的任何信息。
2. 禁止推测：看不到、看不清、被遮挡、被折叠、被气泡盖住的内容，一律 value=null。
   不允许用行业经验、常识或"通常应该有多少"来推算任何数字。宁可留空，绝不猜数。
3. 只识别这六个指标（如截图里有其它数字，直接忽略、不要输出）：
   views（浏览量 / 阅读量 / 小红书的"小眼睛"）、likes（点赞）、collects（收藏）、
   comments（评论）、shares（分享 / 转发）、new_followers（新增粉丝 / 涨粉）。
4. 不计算任何派生指标（不要自己算互动率、收藏率、总和、平均值）。
5. 不要输出作者昵称、头像描述、用户ID/UID、手机号等任何身份信息（即使截图上出现）。

转录规则：
- 每个指标同时给出 raw（截图上的原始写法，如 "1.2万"）与 value（换算后的数字）。
- 确实没有把握的数字：value=null，并把字段名登记进 uncertain_fields（raw 可保留看到的残字）。
- 截图上根本没有这个指标：value=null、raw=null，并登记进 missing_fields。
- 每个指标可附带 confidence（0~1）与 note（如"右侧数字被气泡遮挡"）；没有就 null。

输出格式：严格只输出一个 JSON 对象，不要代码块以外的任何解释性文字。
"""

_USER_TEMPLATE = """请转录这张小红书创作中心的数据截图。

输出 JSON 结构（字段名固定，请勿新增或改名；**只允许六个指标字段**）：
{{
  "metrics": {{
    "views":         {{"value": 数字或null, "raw": "原文", "confidence": 0~1或null, "note": "备注或null"}},
    "likes":         {{"value": 数字或null, "raw": "原文", "confidence": 0~1或null, "note": "备注或null"}},
    "collects":      {{"value": 数字或null, "raw": "原文", "confidence": 0~1或null, "note": "备注或null"}},
    "comments":      {{"value": 数字或null, "raw": "原文", "confidence": 0~1或null, "note": "备注或null"}},
    "shares":        {{"value": 数字或null, "raw": "原文", "confidence": 0~1或null, "note": "备注或null"}},
    "new_followers": {{"value": 数字或null, "raw": "原文", "confidence": 0~1或null, "note": "备注或null"}}
  }},
  "overall_confidence": 0~1或null,
  "missing_fields": ["views"],
  "uncertain_fields": ["collects"],
  "warnings": []
}}

指标中文别名对照（帮助你对号入座，避免把相似指标搞混）：
- views 浏览量 / 阅读量 / 播放量 / 观看量（小红书的"小眼睛"）
- likes 点赞；collects 收藏；comments 评论；shares 分享 / 转发
- new_followers 新增粉丝 / 涨粉 / 新增关注
- （曝光量、点击量、点击率、主页访问、观看时长、弹幕、推荐占比、搜索占比——
  这些**不属于**本任务，直接忽略、不要放进 JSON。）

换算要求（写出 raw 的同时给出 value）：
- "1.2万" / "1.2w" → 12000；"1.2亿" → 120000000；"3.4千" / "3.4k" → 3400；"3,456" → 3456
- "--"、"-"、"暂无"、"?" 等占位符 → value=null，并登记进 missing_fields

{feedback_block}
再次强调：只输出上面约定的 JSON 对象（六个指标），不要解释文字、不要代码块围栏。
"""


def build_user_prompt(*, feedback: str | None = None) -> str:
    """生成用户侧识别指令。

    Args:
        feedback: 上一轮解析失败时的纠正提示（有限回路重试用）；正常首轮为 None。
    """
    block = ""
    if feedback:
        block = (
            "【重要】你上一次的输出无法被解析为合法 JSON。本轮请特别注意：\n"
            f"{feedback.strip()}\n"
        )
    return _USER_TEMPLATE.format(feedback_block=block)


def build_messages(*, image_data_url: str, feedback: str | None = None) -> list[Any]:
    """构造视觉调用的消息列表（SystemMessage + 带图 HumanMessage）。

    Args:
        image_data_url: ``data:image/<type>;base64,...`` 形式的图片数据 URL。
        feedback: 有限回路重试时的纠正提示。

    Returns:
        ``[SystemMessage, HumanMessage]``；HumanMessage 的 content 为
        content-blocks 列表（text + image_url），供多模态模型消费。
    """
    return [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(
            content=[
                {"type": "text", "text": build_user_prompt(feedback=feedback)},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ]
        ),
    ]


__all__ = ["METRIC_PROMPT_VERSION", "build_messages", "build_user_prompt"]
