"""选题 Agent 的提示词（版本化）。

``TOPIC_PROMPT_VERSION`` 会写进 ``topics.meta.prompt_version``，便于回溯「这批选题
是用哪版提示词生成的」。改动模板时必须同步升版本号。

方法论（小红书选题）：从**痛点 / 数字 / 反差 / 人群 / 场景 / 收藏点**切入，
口语化、说人话，**不标题党、不违规**。注入人设定位 / 目标人群 / 内容支柱 / 语气，
以及**单个热点**的来源信息（平台 / 标题 / 链接 / 热度 / 名次）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.services.rag import render_rag_prompt_section

#: 提示词版本（写入 topics.meta.prompt_version）
TOPIC_PROMPT_VERSION = "v1"

_SYSTEM_TEMPLATE = """你是一位资深的小红书内容策划，擅长把一个热点话题拆成若干个**具体、可写、有收藏价值**的选题。

【写作人设】
- 人设名：{persona_name}
- 定位：{positioning}
- 目标读者：{target_audience}
- 内容支柱：{pillars}
- 语气风格：{tone}

【选题方法论】
1. 必须落在上面的人设定位与目标读者上，不要跑偏成泛泛的资讯；
2. 从「痛点 / 数字 / 反差 / 人群 / 场景 / 收藏点」中至少选一个抓手；
3. 标题口语化、说人话、有信息量，**不超过 {title_max_len} 个字**，不标题党、不使用夸张违规词；
4. 内容大纲 3~6 条，每条是一句可执行的小标题，能撑起一篇笔记；
5. 标签 {min_tags}~{max_tags} 个，是小红书 #话题 的**纯词**（不要带 # 号、不要带空格）；
6. 只围绕给定的这**一个**热点展开，不要引入其它话题。
{rag_block}
【输出格式（硬性）】
只输出一个 JSON 对象，结构如下，不要输出 ``` 代码围栏、不要任何解释文字：
{{"topics": [
  {{
    "title": "选题标题",
    "summary": "为什么写 / 写给谁 / 核心观点（一两句）",
    "angle": "切入角度（一句话）",
    "pillar": "所属内容支柱",
    "content_outline": ["大纲1", "大纲2", "大纲3"],
    "tags": ["标签1", "标签2", "标签3"],
    "target_keywords": ["关键词1", "关键词2"],
    "difficulty": "easy | medium | hard",
    "confidence": 0.0
  }}
]}}
其中 "topics" 数组里请给出 {per_hotspot} 条互不重复的选题；"confidence" 是 0~1 的自评分。
"""

_USER_TEMPLATE = """请就下面这**一个热点**，按人设产出 {per_hotspot} 条选题。

【热点来源】
- 平台：{source_name}（{source}）
- 标题：{hot_title}
- 热度：{hot_score}
- 榜单名次：{rank_position}
- 链接：{url}
- 归属日期：{hot_date}
{feedback_block}
直接输出 JSON。"""


def _format_pillars(pillars: Any) -> str:
    """把内容支柱数组渲染成可读字符串。"""
    if isinstance(pillars, list) and pillars:
        return "、".join(str(item) for item in pillars)
    return "（未设置）"


def build_messages(
    *,
    persona_snapshot: dict[str, Any],
    hotspot_snapshot: dict[str, Any],
    per_hotspot: int,
    title_max_len: int,
    min_tags: int,
    max_tags: int,
    feedback: str | None = None,
    rag_context: str | None = None,
    tool_instruction: str | None = None,
    tool_context: str | None = None,
) -> list[Any]:
    """渲染本次调用的消息列表（system + user）。

    Args:
        persona_snapshot: 人设纯 dict 快照（name/positioning/target_audience/tone/content_pillars）。
        hotspot_snapshot: 单个热点纯 dict 快照（source/title/url/hot_score/rank_position/hot_date）。
        per_hotspot: 需要产出的选题条数。
        title_max_len / min_tags / max_tags: 阈值（来自 ``TOPIC_*`` 配置，注入提示词）。
        feedback: 上一轮不合格原因摘要；非空时追加为「修改要求」。
        rag_context: 第 18 步 · RAG 反哺 —— 本账号历史经验块正文（来自
            ``services/rag.safe_rag_block``）；空 / 缺省时提示词与接入前一致。
        tool_instruction: 工具模式下的额外系统指令（只允许只读工具 / 轮数上限 /
            不要编造工具名）；``None`` 时不追加任何字符。
        tool_context: 工具模式收集到的上下文（渲染成用户消息的附录）；
            ``None`` / 空串时不追加任何字符。

    Returns:
        ``[SystemMessage, HumanMessage]``
    """
    system = _SYSTEM_TEMPLATE.format(
        persona_name=persona_snapshot.get("name") or "（未命名）",
        positioning=persona_snapshot.get("positioning") or "（未设置）",
        target_audience=persona_snapshot.get("target_audience") or "（未设置）",
        pillars=_format_pillars(persona_snapshot.get("content_pillars")),
        tone=persona_snapshot.get("tone") or "（未设置）",
        title_max_len=title_max_len,
        min_tags=min_tags,
        max_tags=max_tags,
        per_hotspot=per_hotspot,
        rag_block=render_rag_prompt_section(rag_context),
    )

    feedback_block = ""
    if feedback:
        feedback_block = (
            "\n【上一轮不合格，请务必修正】\n"
            f"{feedback}\n"
            "请针对上述问题重出选题（可以只补足数量，但要确保全部合格）。\n"
        )

    user = _USER_TEMPLATE.format(
        per_hotspot=per_hotspot,
        source_name=hotspot_snapshot.get("source_name") or hotspot_snapshot.get("source") or "未知",
        source=hotspot_snapshot.get("source") or "unknown",
        hot_title=hotspot_snapshot.get("title") or "（无标题）",
        hot_score=hotspot_snapshot.get("hot_score"),
        rank_position=hotspot_snapshot.get("rank_position"),
        url=hotspot_snapshot.get("url") or hotspot_snapshot.get("mobile_url") or "（无）",
        hot_date=hotspot_snapshot.get("hot_date"),
        feedback_block=feedback_block,
    )
    # 工具模式：只追加，不修改既有模板（关闭时两段均为 None → 与接入前逐字节一致）
    if tool_instruction:
        system = f"{system}\n\n{tool_instruction}"
    if tool_context:
        user = f"{user}\n\n【工具收集到的上下文】\n{tool_context}"
    return [SystemMessage(content=system), HumanMessage(content=user)]


__all__ = ["TOPIC_PROMPT_VERSION", "build_messages"]
