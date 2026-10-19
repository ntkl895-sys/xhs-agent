"""创作 Agent 的提示词（版本化）。

``CONTENT_PROMPT_VERSION`` 取自配置（默认 ``v1``），会写进 ``notes.llm_meta.prompt_version``，
便于回溯「这篇草稿是用哪版提示词生成的」。改动模板时必须同步升版本号。

写作要求（小红书成稿）：

- 严格围绕**选题**（标题 / 角度 / 摘要 / 大纲 / 关键词）与人设定位展开；
- 标题口语化、有信息量、**不标题党**；
- 正文分段、给真实细节与可执行信息，**第一人称体验**；
- 标签是 ``#话题`` 的**纯词**；
- 合规：不使用广告法绝对化用语、不承诺疗效 / 收益、不写站外导流联系方式，
  不涉低俗与高风险内容（命中风险词只会被提示，但请从源头规避）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import settings
from app.services.rag import render_rag_prompt_section

#: 提示词版本（写入 notes.llm_meta.prompt_version；改模板必须同步升版本）
CONTENT_PROMPT_VERSION = settings.CONTENT_PROMPT_VERSION

_SYSTEM_TEMPLATE = """你是一位资深的小红书内容作者，擅长把选题写成**可直接发布的中文笔记**。

【写作人设】
- 人设名：{persona_name}
- 定位：{positioning}
- 目标读者：{target_audience}
- 内容支柱：{pillars}
- 语气风格：{tone}

【写作要求】
1. 严格围绕给定的选题展开，不跑题、不引入其它话题；
2. 标题口语化、说人话、有信息量，**不超过 {title_max_len} 个字**，不标题党；
3. 正文 {body_min_len}~{body_max_len} 字，分段书写（2~5 段），给真实细节与可执行信息，
   用第一人称的真实体验口吻，适度使用 emoji，但不要浮夸；
4. 标签 {min_tags}~{max_tags} 个，是小红书 #话题 的**纯词**（不要带 # 号、不要带空格）；
5. 【合规】不使用广告法绝对化用语（如「最」「第一」「唯一」类表述），不承诺疗效 / 收益，
   不出现站外导流与联系方式，不涉低俗与高风险内容。
{rag_block}
【输出格式（硬性）】
只输出一个 JSON 对象，结构如下，不要输出 ``` 代码围栏、不要任何解释文字：
{{"title": "笔记标题", "body": "正文全文（用 \\n 分段）", "tags": ["标签1", "标签2", "标签3"]}}
"""

_USER_TEMPLATE = """请围绕下面这个**已通过评审的选题**，按人设写一篇完整笔记。

【选题】
- 标题：{topic_title}
- 切入角度：{topic_angle}
- 为什么写 / 核心观点：{topic_summary}
- 内容支柱：{topic_pillar}
- 内容大纲：{topic_outline}
- 目标关键词：{topic_keywords}
- 创作难度：{topic_difficulty}
{feedback_block}
直接输出 JSON。"""


def _format_pillars(pillars: Any) -> str:
    """把内容支柱数组渲染成可读字符串。"""
    if isinstance(pillars, list) and pillars:
        return "、".join(str(item) for item in pillars)
    return "（未设置）"


def _format_list(value: Any) -> str:
    """把字符串数组渲染成「1. xxx；2. xxx」形式（空则给出占位）。"""
    if isinstance(value, list) and value:
        return "；".join(f"{index}. {item}" for index, item in enumerate(value, start=1))
    return "（无）"


def build_messages(
    *,
    persona_snapshot: dict[str, Any],
    topic_snapshot: dict[str, Any],
    title_max_len: int,
    body_min_len: int,
    body_max_len: int,
    min_tags: int,
    max_tags: int,
    feedback: str | None = None,
    rag_context: str | None = None,
) -> list[Any]:
    """渲染本次调用的消息列表（system + user）。

    Args:
        persona_snapshot: 人设纯 dict 快照（name / positioning / target_audience / tone / content_pillars）。
        topic_snapshot: 选题纯 dict 快照（title / angle / summary / pillar / content_outline /
            target_keywords / difficulty）。
        title_max_len / body_min_len / body_max_len / min_tags / max_tags: 阈值（来自 ``NOTE_*`` 配置）。
        feedback: 上一轮不合格原因 + 违禁词命中摘要；非空时追加为「修改要求」。
        rag_context: 第 18 步 · RAG 反哺 —— 本账号历史经验块正文（来自
            ``services/rag.safe_rag_block``）；空 / 缺省时提示词与接入前一致。

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
        body_min_len=body_min_len,
        body_max_len=body_max_len,
        min_tags=min_tags,
        max_tags=max_tags,
        rag_block=render_rag_prompt_section(rag_context),
    )

    feedback_block = ""
    if feedback:
        feedback_block = (
            "\n【上一轮存在问题，请务必修正】\n"
            f"{feedback}\n"
            "请在保持选题方向不变的前提下重写，确保全部问题都被解决。\n"
        )

    user = _USER_TEMPLATE.format(
        topic_title=topic_snapshot.get("title") or "（无标题）",
        topic_angle=topic_snapshot.get("angle") or "（无）",
        topic_summary=topic_snapshot.get("summary") or "（无）",
        topic_pillar=topic_snapshot.get("pillar") or "（无）",
        topic_outline=_format_list(topic_snapshot.get("content_outline")),
        topic_keywords=_format_list(topic_snapshot.get("target_keywords")),
        topic_difficulty=topic_snapshot.get("difficulty") or "（无）",
        feedback_block=feedback_block,
    )
    return [SystemMessage(content=system), HumanMessage(content=user)]


__all__ = ["CONTENT_PROMPT_VERSION", "build_messages"]
