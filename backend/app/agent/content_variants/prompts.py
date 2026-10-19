"""多版本创作 Agent 的提示词（版本化）。

``CONTENT_VARIANTS_PROMPT_VERSION`` 取自配置（默认 ``v1``），会写进
``notes.llm_meta.prompt_version``，便于回溯「这批版本是用哪版提示词生成的」。
改动模板时必须同步升版本号。

与第 12 步的关系：

- 写作人设块 / 写作要求 / RAG 块渲染**全部复用第 12 步的口径**
  （``_format_pillars`` / ``_format_list`` / ``render_rag_prompt_section``）；
- 本模块只新增「多版本要求」块与数组输出的硬性格式要求：
  单次调用产出 N 个对象，每个对象含 ``angle`` / ``title`` / ``body`` / ``tags``，
  **各版角度必须明显不同、不得重复**。

内置中文角度池（任务书口径，**不写 env**）：

    实用干货 / 情绪共鸣 / 反差观点 / 亲身经历 / 清单盘点 / 避坑提醒
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.content.prompts import _format_list, _format_pillars  # 复用第 12 步渲染工具
from app.core.config import settings
from app.services.rag import render_rag_prompt_section

#: 提示词版本（写入 notes.llm_meta.prompt_version；改模板必须同步升版本）
CONTENT_VARIANTS_PROMPT_VERSION = settings.CONTENT_VARIANTS_PROMPT_VERSION

#: 内置中文角度池（auto 模式下模型从中自选 N 个互不重复的角度）
ANGLE_POOL: tuple[str, ...] = (
    "实用干货",
    "情绪共鸣",
    "反差观点",
    "亲身经历",
    "清单盘点",
    "避坑提醒",
)

_SYSTEM_TEMPLATE = """你是一位资深的小红书内容作者，擅长把**同一个选题**写出多个角度明显不同的版本。

【写作人设】
- 人设名：{persona_name}
- 定位：{positioning}
- 目标读者：{target_audience}
- 内容支柱：{pillars}
- 语气风格：{tone}

【版本要求（本次任务的核心）】
1. 请一次产出 {n} 个版本，每个版本都必须**完整**包含 angle / title / body / tags，
   像独立成品一样可供横向对比；
2. 各版本的【角度 / 切入方式】必须明显不同，**角度不得重复**；
3. {angle_instruction}
4. 下面每条写作要求**对每个版本都生效**。

【写作要求（逐版适用）】
1. 严格围绕给定的选题展开，不跑题、不引入其它话题；
2. 标题口语化、说人话、有信息量，**不超过 {title_max_len} 个字**，不标题党；
3. 正文 {body_min_len}~{body_max_len} 字，分段书写（2~5 段），给真实细节与可执行信息，
   用第一人称的真实体验口吻，适度使用 emoji，但不要浮夸；
4. 标签 {min_tags}~{max_tags} 个，是小红书 #话题 的**纯词**（不要带 # 号、不要带空格）；
5. 【合规】不使用广告法绝对化用语（如「最」「第一」「唯一」类表述），不承诺疗效 / 收益，
   不出现站外导流与联系方式，不涉低俗与高风险内容。
{rag_block}
【输出格式（硬性）】
只输出一个 JSON **数组**（不是对象），包含 {n} 个对象，不要输出 ``` 代码围栏、不要任何解释文字：
[
  {{"angle": "本版角度", "title": "笔记标题", "body": "正文全文（用 \\n 分段）", "tags": ["标签1", "标签2", "标签3"]}}
]
数组中的每个对象结构相同，共 {n} 个。

"""

_AUTO_ANGLE_INSTRUCTION = (
    "请你从下面的**内置角度池**中选取 {n} 个互不相同的角度（每个版本用一个）："
    "{pool}。"
)

_FIXED_ANGLE_INSTRUCTION = (
    "本次必须**严格使用**以下 {count} 个角度（一个版本一个、顺序对应、不得改写、"
    "不得重复）：{angles}。"
)

_USER_TEMPLATE = """请围绕下面这个**已通过评审的选题**，按人设写出 {n} 个角度明显不同的完整版本。

【选题】
- 标题：{topic_title}
- 切入角度：{topic_angle}
- 为什么写 / 核心观点：{topic_summary}
- 内容支柱：{topic_pillar}
- 内容大纲：{topic_outline}
- 目标关键词：{topic_keywords}
- 创作难度：{topic_difficulty}
{feedback_block}
直接输出 JSON 数组（{n} 个对象）。"""


def build_variants_messages(
    *,
    persona_snapshot: dict[str, Any],
    topic_snapshot: dict[str, Any],
    n: int,
    angles: list[str] | None,
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
        persona_snapshot / topic_snapshot: 与第 12 步同构的纯 dict 快照。
        n: 版本数（1~5）。
        angles: 显式指定且**已去重、数量等于 n** 的角度列表；``None`` = 从角度池自选。
        title_max_len / body_min_len / body_max_len / min_tags / max_tags: ``NOTE_*`` 阈值。
        feedback: 上一轮存在 rejected 版本时的修正要求；非空时追加为「修改要求」。
        rag_context: 第 18 步 · RAG 反哺块正文；空 / 缺省时提示词与接入前一致。

    Returns:
        ``[SystemMessage, HumanMessage]``
    """
    if angles:
        angle_instruction = _FIXED_ANGLE_INSTRUCTION.format(
            count=len(angles), angles="、".join(angles)
        )
    else:
        angle_instruction = _AUTO_ANGLE_INSTRUCTION.format(
            n=n, pool="、".join(ANGLE_POOL)
        )

    system = _SYSTEM_TEMPLATE.format(
        persona_name=persona_snapshot.get("name") or "（未命名）",
        positioning=persona_snapshot.get("positioning") or "（未设置）",
        target_audience=persona_snapshot.get("target_audience") or "（未设置）",
        pillars=_format_pillars(persona_snapshot.get("content_pillars")),
        tone=persona_snapshot.get("tone") or "（未设置）",
        n=n,
        angle_instruction=angle_instruction,
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
            "请在保持选题方向不变的前提下重写全部版本，确保每个版本都完整、可用。\n"
        )

    user = _USER_TEMPLATE.format(
        n=n,
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


__all__ = ["ANGLE_POOL", "CONTENT_VARIANTS_PROMPT_VERSION", "build_variants_messages"]
