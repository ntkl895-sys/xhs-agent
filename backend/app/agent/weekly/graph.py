"""周报图：在 LangGraph harness 上的**第五个业务图**（第 17 步）。

链路（运营周期 → 一次周度复盘，**不写业务表**，结论由 service 回填
``agent_runs.outputs``）：

    START
      │
    load_context    校验周期参数（非法 → WeeklyContextError，service 译 422）；
      │             调 build_period_facts 载入聚合事实（ORM→普通 dict；
      │             计数 / 合计漏斗 / 周期增量 / 新发布清单 / 诊断摘要 / 缺口）
      │
    generate_report 模型（闭包注入）依据**程序算好的周期事实**产出周报 JSON；
      │             JSON 容错与计数由 invoke_json 负责
      │
    validate_report 本地结构校验（summary 必填 / 四个数组条数长度 /
      │             next_week_suggestions 非空为硬伤）
      │
      ├── 合规 ────────────────────────────────────────────► persist_run ─► END
      ├── 不合规且 retry_count < AGENT_JSON_MAX_ATTEMPTS ──► generate（有限回路）
      └── 不合规超限 ──────────────────────────────────────► give_up ─► 抛错（run 记 failed）

要点：

- **真 LangGraph**：五节点 + ``add_conditional_edges`` 条件边 + 有界回路 +
  检查点（由 ``compile_graph`` 绑定）；回路的上界是 ``AGENT_JSON_MAX_ATTEMPTS``，
  最坏模型调用次数 = ``AGENT_JSON_MAX_ATTEMPTS × (1 + AGENT_NODE_MAX_RETRIES)``（3×3=9）；
- **模型经闭包注入**，绝不进状态；周期事实在 ``load_context`` 内由纯函数
  构建为普通 dict 后再放进状态；
- **persist_run 不落业务表**：把归一后的结构化周报组装进 ``state["output"]``，
  由 service 用 ``run_id`` 回填 ``agent_runs``（``llm_raw`` 脱敏 + 截断）；
- **红线（自检断言）**：周报 = 只读 + 建议 —— 本图**零写入**（notes / topics /
  note_metrics / diagnostics 一律不动）；**严禁外部行业数字**（提示词约束 +
  自检黑名单扫描）；数据不足在 ``data_gaps`` 如实声明。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START

from app.agent.errors import AgentJSONError
from app.agent.graph.base import compile_graph, llm_retry_policy, new_state_graph
from app.agent.parsing.json_repair import invoke_json
from app.agent.state.base import merge_parse_attempts, merge_usage
from app.agent.weekly.contracts import (
    REPORT_MAX_LIST_ITEMS,
    REPORT_SUMMARY_MAX_LEN,
    REPORT_TEXT_MAX_LEN,
    WeeklyContextError,
    WeeklyGiveUpError,
    build_period_facts,
    merge_data_gaps,
    normalize_report_payload,
    resolve_period,
)
from app.agent.weekly.prompts import WEEKLY_PROMPT_VERSION, build_messages
from app.agent.weekly.state import WeeklyState
from app.core.config import settings
from app.db.session import SessionLocal

logger = logging.getLogger("xhs-agent.weekly.graph")

#: 五个节点名（自检会断言节点齐全与边结构）
NODE_LOAD_CONTEXT = "load_context"
NODE_GENERATE = "generate_report"
NODE_VALIDATE = "validate_report"
NODE_PERSIST = "persist_run"
NODE_GIVE_UP = "give_up"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _build_feedback(field_errors: list[str]) -> str:
    """把本地校验硬伤整理成下一轮的重问提示（中文、逐条可执行）。"""
    bullets = "\n".join(
        f"{index}) {item}" for index, item in enumerate(field_errors[:6], start=1)
    )
    return (
        "你上一次的输出存在以下问题，必须逐条修正后重新输出**完整** JSON：\n"
        f"{bullets}\n"
        "注意：只输出 JSON 对象本身；summary 必须非空；"
        "next_week_suggestions 至少要有一条具体可执行的动作（不能为空数组）。"
    )


# ---------------------------------------------------------------------------
# 构图
# ---------------------------------------------------------------------------
def build_weekly_report_graph(
    *,
    model: BaseChatModel,
    use_checkpointer: bool = True,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译「运营周期 → 一次周度复盘」业务图。

    Args:
        model: 已注入的 LangChain ChatModel（真实 ``ChatOpenAI`` 或离线假模型）。
            **经闭包注入节点，绝不放进状态**（状态要序列化进检查点）。
        use_checkpointer: 是否绑定检查点（默认绑定）。
        checkpointer: 显式指定检查点实现；``None`` 表示取全局单例。

    Returns:
        CompiledStateGraph
    """

    # ------------------------------------------------------------ load_context
    def load_context_node(state: WeeklyState) -> dict[str, Any]:
        """校验周期参数 + 载入周期事实（全部本地计算；ORM 行转普通 dict）。"""
        started = time.perf_counter()

        try:
            start, end, span_days = resolve_period(
                period_start=state.get("period_start"),
                period_end=state.get("period_end"),
                days=state.get("days"),
            )
        except WeeklyContextError:
            raise  # 原样抛出：service 统一译 422（响应不含堆栈）

        with SessionLocal() as db:
            facts = build_period_facts(db, period_start=start, period_end=end)

        logger.info(
            "周报上下文就绪：周期=[%s ~ %s] %s 天，周期内发布=%s，缺口=%s",
            start.date().isoformat(),
            end.date().isoformat(),
            span_days,
            facts.get("counts", {}).get("notes_published_in_period"),
            len(facts.get("data_gaps") or []),
        )
        return {
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "period_facts": facts,
            "node_trace": [
                {
                    "node": NODE_LOAD_CONTEXT,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    "period_days": span_days,
                    "published_in_period": facts.get("counts", {}).get(
                        "notes_published_in_period"
                    ),
                    "data_gaps": len(facts.get("data_gaps") or []),
                }
            ],
        }

    # --------------------------------------------------------- generate_report
    def generate_report_node(state: WeeklyState) -> dict[str, Any]:
        """调用模型产出周报 JSON（容错与计数交给 invoke_json）。"""
        started = time.perf_counter()
        retry_count = int(state.get("retry_count") or 0) + 1

        messages = build_messages(
            period_facts=state.get("period_facts") or {},
            max_items=REPORT_MAX_LIST_ITEMS,
            text_max_len=REPORT_TEXT_MAX_LEN,
            summary_max_len=REPORT_SUMMARY_MAX_LEN,
            feedback=state.get("feedback") or None,
        )
        try:
            result = invoke_json(model, messages, want_json=True)
        except AgentJSONError as exc:
            # invoke_json 内部纠错已用尽：交给条件边决定「回炉 / 放弃」
            return {
                "parse_failed": True,
                "raw": {},
                "llm_raw": exc.raw_text or state.get("llm_raw"),
                "retry_count": retry_count,
                "feedback": (
                    "上一次输出无法解析为 JSON。请只输出一个 JSON 对象："
                    "不要代码块围栏、不要解释文字，字符串内不要出现未转义的换行。"
                ),
                "node_trace": [
                    {
                        "node": NODE_GENERATE,
                        "status": "json_failed",
                        "duration_ms": _elapsed_ms(started),
                        "attempt": retry_count,
                    }
                ],
            }

        raw_payload: dict[str, Any] = (
            result.payload if isinstance(result.payload, dict) else {}
        )
        return {
            "raw": raw_payload,
            "llm_raw": result.raw_text,
            "parse_failed": False,
            "retry_count": retry_count,
            "parse_attempts": merge_parse_attempts(
                state.get("parse_attempts"), result.attempts
            ),
            "usage": merge_usage(state.get("usage"), result.usage),
            "node_trace": [
                {
                    "node": NODE_GENERATE,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    "attempt": retry_count,
                    "json_parse_attempts": result.attempts,
                    "json_mode_used": result.json_mode_used,
                }
            ],
        }

    # --------------------------------------------------------- validate_report
    def validate_report_node(state: WeeklyState) -> dict[str, Any]:
        """本地结构校验：summary 必填 / 数组条数长度 / suggestions 非空（硬伤）。"""
        started = time.perf_counter()

        if state.get("parse_failed"):
            return {
                "validation_ok": False,
                "field_errors": ["模型输出无法解析为 JSON"],
                "feedback": (
                    "上一次输出无法解析为 JSON。必须只输出一个 JSON 对象："
                    "不要代码块围栏、不要解释文字。"
                ),
                "node_trace": [
                    {
                        "node": NODE_VALIDATE,
                        "status": "json_failed",
                        "duration_ms": _elapsed_ms(started),
                        "field_errors": 1,
                    }
                ],
            }

        normalized, errors, warnings = normalize_report_payload(
            state.get("raw") or {},
            period_start=str(state.get("period_start") or ""),
            period_end=str(state.get("period_end") or ""),
            max_items=REPORT_MAX_LIST_ITEMS,
            text_max_len=REPORT_TEXT_MAX_LEN,
            summary_max_len=REPORT_SUMMARY_MAX_LEN,
        )
        validation_ok = not errors
        return {
            "validation_ok": validation_ok,
            "field_errors": errors,
            "warnings": warnings,
            "feedback": "" if validation_ok else _build_feedback(errors),
            "report": normalized,
            "model_data_gaps": list(normalized.get("data_gaps") or []),
            "node_trace": [
                {
                    "node": NODE_VALIDATE,
                    "status": "ok" if validation_ok else "invalid",
                    "duration_ms": _elapsed_ms(started),
                    "field_errors": len(errors),
                    "warnings": len(warnings),
                }
            ],
        }

    # ------------------------------------------------------------- persist_run
    def persist_run_node(state: WeeklyState) -> dict[str, Any]:
        """**不写业务表**：把归一后的结构化周报组装进 ``state["output"]``。

        由 service 用 ``run_id`` 把 ``output`` 回填到 ``agent_runs.outputs``
        （``persisted`` 语义 = 审计已就绪，报告本体不落业务表）。
        """
        started = time.perf_counter()

        report = dict(state.get("report") or {})
        facts = state.get("period_facts") or {}
        local_gaps = [str(item) for item in (facts.get("data_gaps") or [])]
        model_gaps = [str(item) for item in (state.get("model_data_gaps") or [])]
        merged_gaps = merge_data_gaps(local_gaps, model_gaps)
        report["data_gaps"] = merged_gaps

        warnings = [str(item) for item in (state.get("warnings") or [])]
        output: dict[str, Any] = {
            "run_id": state.get("run_id"),
            "period_start": report.get("period_start"),
            "period_end": report.get("period_end"),
            "report": report,
            "parse_attempts": int(state.get("parse_attempts") or 0),
            "prompt_version": WEEKLY_PROMPT_VERSION,
            "data_gaps": merged_gaps,
            "warnings": warnings,
            "persisted": True,
        }
        return {
            "output": output,
            "node_trace": [
                {
                    "node": NODE_PERSIST,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    "period_start": report.get("period_start"),
                    "period_end": report.get("period_end"),
                    "data_gaps": len(merged_gaps),
                }
            ],
        }

    # ---------------------------------------------------------------- give_up
    def give_up_node(state: WeeklyState) -> dict[str, Any]:
        """结构校验连续超限：抛周报放弃错误（run 记 failed，service 译 400）。"""
        retry_count = int(state.get("retry_count") or 0)
        field_errors = [str(item) for item in (state.get("field_errors") or [])][:3]
        detail = "；".join(field_errors) or "未知原因"
        raise WeeklyGiveUpError(
            f"周报生成失败：连续 {retry_count} 轮输出均不满足结构要求（{detail}）。"
            "可稍后重试；若反复出现，请确认所配模型稳定支持 JSON 输出后重试。",
            reason=f"retry_count={retry_count}",
        )

    # --------------------------------------------------------------- 条件边
    def route_after_validate(state: WeeklyState) -> str:
        """合规 → persist_run；未超限 → 回炉；超限 → give_up（回路有界）。"""
        if state.get("validation_ok"):
            return NODE_PERSIST
        retry_count = int(state.get("retry_count") or 0)
        if retry_count < max(1, settings.AGENT_JSON_MAX_ATTEMPTS):
            return NODE_GENERATE
        return NODE_GIVE_UP

    # ------------------------------------------------------------------ 装配
    graph = new_state_graph("weekly-report", WeeklyState)
    graph.add_node(NODE_LOAD_CONTEXT, load_context_node)
    # 调用 LLM 的节点统一带重试策略（仅重试 429/5xx/网络等瞬时错误）
    graph.add_node(NODE_GENERATE, generate_report_node, retry_policy=llm_retry_policy())
    graph.add_node(NODE_VALIDATE, validate_report_node)
    graph.add_node(NODE_PERSIST, persist_run_node)
    graph.add_node(NODE_GIVE_UP, give_up_node)

    graph.add_edge(START, NODE_LOAD_CONTEXT)
    graph.add_edge(NODE_LOAD_CONTEXT, NODE_GENERATE)
    graph.add_edge(NODE_GENERATE, NODE_VALIDATE)
    graph.add_conditional_edges(
        NODE_VALIDATE,
        route_after_validate,
        {
            NODE_GENERATE: NODE_GENERATE,
            NODE_PERSIST: NODE_PERSIST,
            NODE_GIVE_UP: NODE_GIVE_UP,
        },
    )
    graph.add_edge(NODE_PERSIST, END)
    graph.add_edge(NODE_GIVE_UP, END)

    return compile_graph(
        graph, checkpointer=checkpointer, use_checkpointer=use_checkpointer
    )


__all__ = [
    "NODE_LOAD_CONTEXT",
    "NODE_GENERATE",
    "NODE_VALIDATE",
    "NODE_PERSIST",
    "NODE_GIVE_UP",
    "build_weekly_report_graph",
]
