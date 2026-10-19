"""周报提示词（版本化）。

``WEEKLY_PROMPT_VERSION`` 取自配置（默认 ``v1``），会写进周报的
``llm_meta`` / output 摘要，便于回溯「这次周报是哪版提示词生成的」。
改模板必须同步升版本。

核心立场（任务书第 3 部分）：

- 模型是「**只依据给定周期事实**做周度复盘的资深运营顾问」——所有数字
  （计数、合计、六率、增量、诊断摘要）**由本地程序算好**再喂给它，
  **严禁让模型做算术、严禁引入任何外部行业数字**（行业均值 / 爆款门槛 /
  同类账号平均一律禁止，跨篇基准属第 18 步 RAG）；
- 数据不足必须如实写进 ``data_gaps``（周期内无数据也不许硬造亮点）；
- ``next_week_suggestions`` 必须**具体可执行且按重要性排序**
  （标题 / 封面 / 选题方向 / 发布节奏 / 评论区引导等明确动作）；
- 不评价敏感话题、不提供违规手段（刷量 / 诱导 / 搬运）；
- 输出**严格 JSON**（给出字段 schema）。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import settings

#: 提示词版本（写入周报 output 与审计摘要；改模板必须同步升版本）
WEEKLY_PROMPT_VERSION = settings.WEEKLY_PROMPT_VERSION

_SYSTEM_PROMPT = """你是一位资深的小红书账号运营顾问，负责对一个运营周期做**周度复盘**。

你只会拿到「由程序计算好的周期事实」（计数、指标合计与互动率、周期增量、
新发布笔记清单、单篇诊断摘要、数据缺口）。必须严格遵守：

1. 只依据我提供的周期事实做判断。**严禁引入任何外部数字**：不许引用"行业平均
   点赞率"、"爆款门槛"、"同类账号平均涨粉"之类的经验值或阈值——本期没有任何
   外部基准（跨篇对比属后续能力）；你的结论只能来自给定事实。
2. **不要做算术**：所有数字都已算好，直接引用；需要引用时给出具体数字。
3. 数据不足必须如实声明：周期内没有发布、笔记没有快照、只能算部分指标等情况，
   都要写进 data_gaps；**数据不足时不许硬造亮点**（宁少不假）。
4. next_week_suggestions 必须**具体可执行**并按重要性排序：明确动作
   （选题方向怎么定、标题/封面怎么调、发布节奏、评论区怎么引导、数据回填怎么做），
   禁止"多发优质内容"这类空泛口号；禁止任何违规手段（刷量 / 诱导 / 搬运）。
5. 不评价敏感话题；不臆测账号身份与粉丝画像。
6. 输出**严格 JSON**：只输出一个 JSON 对象，不含代码块围栏与任何解释文字。

写作要求：
- summary 用 150~400 字概括周期整体情况（发生了什么、整体表现、最值得注意的点）；
- highlights 是周期内值得肯定的具体发现；issues 是需要警惕或改进的问题；
- data_evidence 每条必须**引用给定事实中的具体数字**（如"本周 views 合计 X"）；
- 亮点 / 问题 / 依据要互相呼应，形成"数据 → 解读 → 行动"的完整闭环。
"""

_USER_TEMPLATE = """请对下面这个运营周期做一次「周度复盘」，输出严格 JSON。

【周期事实（全部由程序计算，直接引用，不要重算）】
{period_facts_json}

【输出要求】
严格只输出一个 JSON 对象（字段名固定，勿新增改名）：
{{
  "summary": "周期整体概述（150~400 字，≤ {summary_max_len} 字）",
  "highlights": ["周期内值得肯定的发现，0~{max_items} 条，每条 ≤ {text_max_len} 字"],
  "issues": ["需要警惕或改进的问题，0~{max_items} 条"],
  "data_evidence": ["关键结论的数据依据（必须引用给定事实中的具体数字），0~{max_items} 条"],
  "next_week_suggestions": ["下周可执行动作（按重要性排序，1~{max_items} 条；每条必须是一个明确动作）"],
  "data_gaps": ["你判断的数据缺口（与给定缺口合并去重）"],
  "confidence": 0~1 的小数或 null
}}

{feedback_block}
再次强调：只输出上述 JSON 对象；所有结论只用给定事实；没有外部基准；
next_week_suggestions 不能为空。
"""


def build_user_prompt(
    *,
    period_facts: dict[str, Any],
    max_items: int,
    text_max_len: int,
    summary_max_len: int,
    feedback: str | None = None,
) -> str:
    """生成用户侧周报指令（周期事实 JSON + 输出 schema + 可选回炉反馈）。"""
    feedback_block = ""
    if feedback:
        feedback_block = (
            "【重要】你上一次的输出存在下列问题，请修正后重新输出完整 JSON：\n"
            f"{feedback.strip()}\n"
        )

    facts_json = json.dumps(period_facts, ensure_ascii=False, default=str)
    return _USER_TEMPLATE.format(
        period_facts_json=facts_json,
        max_items=max_items,
        text_max_len=text_max_len,
        summary_max_len=summary_max_len,
        feedback_block=feedback_block,
    )


def build_messages(
    *,
    period_facts: dict[str, Any],
    max_items: int,
    text_max_len: int,
    summary_max_len: int,
    feedback: str | None = None,
) -> list[Any]:
    """构造周报调用的消息列表（SystemMessage + HumanMessage，纯文本）。"""
    return [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(
            content=build_user_prompt(
                period_facts=period_facts,
                max_items=max_items,
                text_max_len=text_max_len,
                summary_max_len=summary_max_len,
                feedback=feedback,
            )
        ),
    ]


__all__ = ["WEEKLY_PROMPT_VERSION", "build_messages", "build_user_prompt"]
