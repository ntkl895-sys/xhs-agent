"""规律提炼 Agent 的提示词（版本化，第 18 步）。

``INSIGHTS_PROMPT_VERSION`` 取自配置（默认 ``v1``），会写进
``knowledge_snippets.meta.prompt_version``，便于回溯「这条规律是用哪版提示词
提炼的」。改动模板时必须同步升版本号。

红线（自检断言）：提示词**明文禁止外部数字**（行业平均 / 爆款门槛 / 平台算法），
要求规律只能写成「在本账号已发布的 N 篇笔记中……」的个人经验表述；
禁止编造未提供的数据。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import settings

#: 提示词版本（写入知识块 meta.prompt_version；改模板必须同步升版本）
INSIGHTS_PROMPT_VERSION = settings.INSIGHTS_PROMPT_VERSION

_SYSTEM_TEMPLATE = """你是一位资深的账号运营顾问，负责从「本账号自己的运营数据」中归纳可复用的爆款规律。

【铁律（违反即不合格）】
1. 只能依据下方给出的真实数据归纳；**禁止编造未提供的数据**；**禁止出现行业平均、爆款门槛、
   平台官方算法、大盘数据等任何外部数字**（本账号是唯一数据来源）；
2. 每条规律必须写成「在本账号已发布的 {sample_size} 篇笔记中，表现较好的……」这类
   **个人经验**表述，禁止写成普适行业定律（如「爆款一定……」「所有账号都……」）；
3. 每条规律必须齐全：pattern（规律文本）、pattern_type、scenario（适用场景）、
   source_note_ids（支撑来源，只能从给定笔记 id 中选取）、evidence（对应指标证据，
   引用给定数字）、confidence（0~1）；
4. 规律要「可复用」：能直接指导后续的选题方向、标题句式、开头/结构、标签组合、
   发布时间或内容手法；
5. 数据不足的主题宁可少写，不要凑数；按置信度从高到低排序，最多 {max_items} 条，
   单条文本不超过 {text_max_len} 字。

【pattern_type 二选一】
- "hit_pattern"：现象型规律（表现较好笔记的共性：选题 / 标题 / 结构 / 标签 / 时间 / 手法）；
- "review_insight"：改进型心得（从诊断的 weaknesses / suggestions 归纳的行动方向）。

【输出格式（硬性）】
只输出一个 JSON 对象，不要输出 ``` 代码围栏、不要任何解释文字：
{{"patterns": [
  {{
    "pattern": "在本账号已发布的 N 篇笔记中，……",
    "pattern_type": "hit_pattern 或 review_insight",
    "scenario": "适用场景（一句话）",
    "source_note_ids": ["笔记id1", "笔记id2"],
    "evidence": "对应指标证据（引用给定数字，如 views / like_rate）",
    "confidence": 0.0
  }}
]}}
"""

_USER_HEADER = """下面是本账号已发布笔记的真实运营数据（共 {sample_size} 篇，\
发布时间范围 {date_range}）。

请对比「表现较好」与「表现较差」的笔记，归纳 1~{max_items} 条可复用规律。

【数据说明】
- 六指标 = views（浏览）/ likes（点赞）/ collects（收藏）/ comments（评论）/
  shares（分享）/ new_followers（新增粉丝），均为**本账号快照**的最新值；
- 六率 = 对应互动 / views（4 位小数）；
- 总增量 = 首个有效快照 → 最新快照的差值；
- 诊断摘要 = 该笔记最新一条单篇诊断的结论（可能没有）。
"""


def _format_pills(items: Any) -> str:
    """把字符串数组渲染成「a / b / c」（空 → （无））。"""
    if isinstance(items, list) and items:
        return " / ".join(str(item) for item in items)
    return "（无）"


def _format_rates(rates: Any) -> str:
    """渲染六率（只显示非 None 的键值对；全空 → （不可计算））。"""
    if not isinstance(rates, dict):
        return "（不可计算）"
    parts = [f"{key}={value}" for key, value in rates.items() if value is not None]
    return "；".join(parts) if parts else "（不可计算）"


def _format_metrics(metrics: Any) -> str:
    """渲染六指标（最新值）。"""
    if not isinstance(metrics, dict):
        return "（无数据）"
    parts = []
    for key in ("views", "likes", "collects", "comments", "shares", "new_followers"):
        value = metrics.get(key)
        parts.append(f"{key}={value if value is not None else '—'}")
    return " ".join(parts)


def _format_deltas(deltas: Any) -> str:
    """渲染总增量（正数带 +；缺 → （不可计算））。"""
    if not isinstance(deltas, dict):
        return "（不可计算）"
    parts = []
    for key, value in deltas.items():
        if value is None:
            continue
        sign = "+" if isinstance(value, int) and value >= 0 else ""
        parts.append(f"{key} {sign}{value}")
    return " / ".join(parts) if parts else "（不可计算）"


def _format_diagnosis(diagnosis: Any) -> str:
    """渲染最新诊断摘要（无 → （无诊断））。"""
    if not isinstance(diagnosis, dict):
        return "（无诊断）"
    lines = [f"score={diagnosis.get('score') if diagnosis.get('score') is not None else '—'}"]
    if diagnosis.get("strengths"):
        lines.append("优点：" + "；".join(str(item) for item in diagnosis["strengths"]))
    if diagnosis.get("weaknesses"):
        lines.append("不足：" + "；".join(str(item) for item in diagnosis["weaknesses"]))
    if diagnosis.get("suggestions"):
        lines.append("建议：" + "；".join(str(item) for item in diagnosis["suggestions"]))
    return "\n    ".join(lines)


def _render_note(index: int, note: dict[str, Any]) -> str:
    """渲染单篇笔记（纯文本，控制体积）。"""
    return (
        f"【笔记 {index}】note_id={note.get('note_id')}\n"
        f"  - 标题：{note.get('title') or '（无）'}\n"
        f"  - 标签：{_format_pills(note.get('tags'))}\n"
        f"  - 发布：{note.get('published_at') or '（未知）'}\n"
        f"  - 六指标（最新）：{_format_metrics(note.get('metrics'))}\n"
        f"  - 六率：{_format_rates(note.get('rates'))}\n"
        f"  - 总增量：{_format_deltas(note.get('total_deltas'))}\n"
        f"  - 诊断摘要：{_format_diagnosis(note.get('diagnosis'))}\n"
        f"  - 正文摘录：{note.get('body_excerpt') or '（空）'}"
    )


def build_messages(
    *,
    facts: dict[str, Any],
    max_items: int,
    text_max_len: int,
    feedback: str | None = None,
) -> list[Any]:
    """渲染本次调用的消息列表（system + user）。

    Args:
        facts: :func:`~app.agent.insights.contracts.build_insight_facts` 的返回
            （普通 dict；含 sample_size / notes / data_range / data_gaps）。
        max_items: 规律条数上限（注入提示词）。
        text_max_len: 单条文本最大字数（注入提示词）。
        feedback: 上一轮不合格原因摘要；非空时追加为「修改要求」。

    Returns:
        ``[SystemMessage, HumanMessage]``
    """
    sample_size = int(facts.get("sample_size") or 0)
    system = _SYSTEM_TEMPLATE.format(
        sample_size=sample_size,
        max_items=max(1, int(max_items)),
        text_max_len=max(1, int(text_max_len)),
    )

    date_range = facts.get("data_range") or {}
    span = "{} ~ {}".format(
        date_range.get("earliest_published_at") or "（未知）",
        date_range.get("latest_published_at") or "（未知）",
    )

    blocks: list[str] = [
        _USER_HEADER.format(
            sample_size=sample_size,
            date_range=span,
            max_items=max(1, int(max_items)),
        )
    ]
    notes = facts.get("notes") or []
    for index, note in enumerate(notes, start=1):
        blocks.append(_render_note(index, note))

    gaps = facts.get("data_gaps") or []
    if gaps:
        blocks.append(
            "【已知数据缺口（分析时请如实回避，不要强行下结论）】\n- "
            + "\n- ".join(str(item) for item in gaps)
        )

    feedback_block = ""
    if feedback:
        feedback_block = (
            "\n【上一轮不合格，请务必修正】\n"
            f"{feedback}\n"
            "请针对上述问题重新输出**完整**规律 JSON（保持证据来自给定数据）。\n"
        )
    blocks.append(feedback_block + "\n直接输出 JSON。")

    return [SystemMessage(content=system), HumanMessage(content="\n\n".join(blocks))]


__all__ = ["INSIGHTS_PROMPT_VERSION", "build_messages"]
