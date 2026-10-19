"""单篇诊断提示词（版本化）。

``DIAG_PROMPT_VERSION`` 取自配置（默认 ``v1``），会写进 ``diagnostics.llm_meta``
与 ``agent_runs`` 的 output 摘要，便于回溯「这次诊断是用哪版提示词生成的」。
改模板必须同步升版本。

核心立场（任务书第 1 / 5 部分）：

- 模型是「**只基于已提供的该笔记事实数据**做诊断的资深运营顾问」——
  事实（最新值 / 比率 / 环比 / 数据缺口）**全部由本地程序算好**再喂给它，
  **严禁让模型做算术、严禁引入任何外部行业数字**（行业均值 / 爆款阈值一律禁止，
  跨篇基准属第 18 步 RAG）；
- 数据不足必须如实声明（写进 ``data_gaps``），严重不足时 ``score`` 必须为 ``null``；
- ``suggestions`` 必须**具体可执行**（标题 / 封面 / 正文结构 / 标签 / 发布时段 /
  评论区引导），区分优先级，禁止空泛口号；不评价敏感话题、不提供违规手段；
- 输出**严格 JSON**（给出字段 schema）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import settings

#: 提示词版本（写入 diagnostics.llm_meta；改模板必须同步升版本）
DIAG_PROMPT_VERSION = settings.DIAG_PROMPT_VERSION

_SYSTEM_PROMPT = """你是一位资深的小红书账号运营顾问，负责对**单篇笔记**做复盘诊断。

你只会拿到这篇笔记的**内容信息**与**由程序计算好的数据事实**（最新指标、互动率、
环比与增长率、数据缺口）。必须严格遵守：

1. 只依据我提供的事实数据做判断。**严禁引入任何外部数字**：不许引用"行业平均
   点赞率"、"爆款门槛"、"通常涨粉速度"之类的经验值或阈值——本期没有任何外部
   基准（跨篇对比属后续能力）；你的结论只能来自这篇笔记自身的数据与内容。
2. 数据不足必须如实声明：缺失的指标、算不了的比率、只有一条快照等情况，
   都要写进 data_gaps；数据严重不足时 score 必须为 null（宁可不评分，不许硬打分）。
3. score 是 0~100 的整数或一位小数，衡量这篇笔记在该账号当前阶段的表现健康度；
   有数据支撑才给分，理由要与 reasons 对应。
4. suggestions 必须**具体可执行**：明确动作（如改标题的什么要素、封面怎么调、
   正文前几句怎么改、标签怎么选、什么时段发、评论区怎么引导），并按重要性排序；
   禁止"多发优质内容"这类空泛口号，禁止任何违规手段（刷量 / 诱导 / 搬运）。
5. 不评价敏感话题；不臆测账号身份与粉丝画像。
6. 输出**严格 JSON**：只输出一个 JSON 对象，不含代码块围栏与任何解释文字。

诊断类型说明（程序已判定，你按它组织语言）：
- cold_start：数据不足的冷启动诊断——以"尽快回填数据 + 内容侧可检查项"为主，
  明确这是内容侧初步检查而非数据诊断；
- review：常规复盘——结合数据事实给出亮点 / 问题 / 原因 / 建议；
- anomaly：异常波动——围绕出现明显负增长的指标分析可能原因（结合内容与发布信息），
  建议以"先核实数据、再针对性调整"为主。
"""

_USER_TEMPLATE = """请对下面这篇笔记做「{diagnosis_type}」诊断。

【笔记内容】
- 标题：{title}
- 正文（节选，共 {body_length} 字）：{body_excerpt}
- 标签：{tags}
- 状态：{status}；发布时间：{published_at}
- 人设：{persona_line}

【数据事实（全部由程序计算，直接采用，不要重算）】
- 快照条数：{snapshot_count}
- 最新指标：{latest_json}
- 互动率：{rates_json}
- 环比与增长：{trends_json}
- 数据覆盖时长（小时）：{coverage_hours}
- 已知数据缺口：{data_gaps_json}

【输出要求】
严格只输出一个 JSON 对象（字段名固定，勿新增改名）：
{{
  "score": 0~100 的数字或 null（数据严重不足时必须 null）,
  "strengths": ["亮点，1~{max_items} 条，每条 ≤ {text_max_len} 字"],
  "weaknesses": ["问题，1~{max_items} 条"],
  "reasons": ["对亮点/问题的原因分析，每条尽量指向具体数据或内容依据；1~{max_items} 条"],
  "suggestions": ["可执行建议，按重要性排序，1~{max_items} 条；每条必须是一个明确动作"],
  "data_gaps": ["你判断的其它数据缺口（与给定缺口合并去重）"],
  "confidence": 0~1 的小数或 null
}}

{feedback_block}
再次强调：只输出上述 JSON 对象；所有结论只用给定事实；没有外部基准。
"""


def _json_or_null(value: Any) -> str:
    """把 dict / list 渲染成紧凑 JSON 文本（供提示词内嵌）。"""
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def build_user_prompt(
    *,
    note_snapshot: dict[str, Any],
    persona_snapshot: dict[str, Any] | None,
    latest: dict[str, Any],
    rates: dict[str, Any],
    trends: dict[str, Any],
    data_gaps: list[str],
    diagnosis_type: str,
    max_items: int,
    text_max_len: int,
    feedback: str | None = None,
) -> str:
    """生成用户侧诊断指令（事实数据 + 输出 schema + 可选回炉反馈）。"""
    feedback_block = ""
    if feedback:
        feedback_block = (
            "【重要】你上一次的输出存在下列问题，请修正后重新输出完整 JSON：\n"
            f"{feedback.strip()}\n"
        )

    persona_line = "未关联人设"
    if persona_snapshot:
        parts = [
            f"名称：{persona_snapshot.get('name') or '—'}",
            f"定位：{persona_snapshot.get('positioning') or '—'}",
            f"语气：{persona_snapshot.get('tone') or '—'}",
        ]
        persona_line = "；".join(parts)

    tags_value = note_snapshot.get("tags") or []
    tags_line = "、".join(str(item) for item in tags_value) if tags_value else "（无标签）"

    return _USER_TEMPLATE.format(
        diagnosis_type=diagnosis_type,
        title=note_snapshot.get("title") or "（无标题）",
        body_length=note_snapshot.get("body_length") or 0,
        body_excerpt=note_snapshot.get("body_excerpt") or "（空）",
        tags=tags_line,
        status=note_snapshot.get("status") or "未知",
        published_at=note_snapshot.get("published_at") or "（未记录）",
        persona_line=persona_line,
        snapshot_count=note_snapshot.get("snapshot_count", 0),
        latest_json=_json_or_null(latest),
        rates_json=_json_or_null(rates),
        trends_json=_json_or_null(trends),
        coverage_hours=trends.get("coverage_hours"),
        data_gaps_json=_json_or_null(data_gaps),
        max_items=max_items,
        text_max_len=text_max_len,
        feedback_block=feedback_block,
    )


def build_messages(
    *,
    note_snapshot: dict[str, Any],
    persona_snapshot: dict[str, Any] | None,
    latest: dict[str, Any],
    rates: dict[str, Any],
    trends: dict[str, Any],
    data_gaps: list[str],
    diagnosis_type: str,
    max_items: int,
    text_max_len: int,
    feedback: str | None = None,
) -> list[Any]:
    """构造诊断调用的消息列表（SystemMessage + HumanMessage，纯文本）。"""
    return [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(
            content=build_user_prompt(
                note_snapshot=note_snapshot,
                persona_snapshot=persona_snapshot,
                latest=latest,
                rates=rates,
                trends=trends,
                data_gaps=data_gaps,
                diagnosis_type=diagnosis_type,
                max_items=max_items,
                text_max_len=text_max_len,
                feedback=feedback,
            )
        ),
    ]


__all__ = ["DIAG_PROMPT_VERSION", "build_messages", "build_user_prompt"]
