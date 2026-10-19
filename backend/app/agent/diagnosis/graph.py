"""单篇诊断图：在 LangGraph harness 上的**第四个业务图**（第 16 步）。

链路（单篇笔记 → 一次诊断结论，**落库**）：

    START
      │
    load_context        校验笔记存在（不存在 → DiagnosisContextError，service 译 404）；
      │                 载入笔记内容 / 人设 / 数据快照时间线 / 是否已有选中封面；
      │                 **本地确定性计算**最新值 / 六率 / 环比 / 覆盖时长 / 类型判定 /
      │                 data_gaps（纯函数，绝不把算术交给模型）
      │
    generate_diagnosis  模型（闭包注入）依据**程序算好的事实**产出诊断 JSON；
      │                 JSON 容错与计数由 invoke_json 负责
      │
    validate_diagnosis  本地结构校验（score 区间 / 四数组条数长度 / suggestions 非空；
      │                 「数据不足型冷启动」强制 score=None 不硬打分）
      │
      ├── 合规 ────────────────────────────────────────────► persist ─► END
      ├── 不合规且 retry_count < AGENT_JSON_MAX_ATTEMPTS ──► generate（有限回路）
      └── 不合规超限 ──────────────────────────────────────► give_up ─► 抛错（run 记 failed）

要点：

- **真 LangGraph**：五节点 + ``add_conditional_edges`` 条件边 + 有界回路 +
  检查点（由 ``compile_graph`` 绑定）；回路的上界是 ``AGENT_JSON_MAX_ATTEMPTS``，
  最坏模型调用次数 = ``AGENT_JSON_MAX_ATTEMPTS × (1 + AGENT_NODE_MAX_RETRIES)``（3×3=9）；
- **模型经闭包注入**，绝不进状态；ORM 行在 ``load_context`` 内转普通 dict
  （``captured_at`` 用 ISO 字符串）后再放进状态；
- **确定性计算全部在本地**：最新值 / 六条互动率 / 环比与增长率 / 覆盖时长 /
  类型判定 / 数据缺口，全部来自 ``contracts`` 纯函数；模型只做「评价与建议」；
- **红线（自检断言）**：诊断 = 只读 + 建议 —— 本图**只写入 diagnostics 一条记录**，
  绝不动 notes / topics / note_metrics；``benchmark`` 只装「该笔记自身相对指标 +
  基准缺失声明」，**绝不杜撰外部行业数字**；数据不足如实声明（``data_gaps``），
  严重不足时 ``score`` 为 ``null``。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START
from sqlalchemy import select

from app.agent.diagnosis.contracts import (
    BENCHMARK_NOTE,
    DiagnosisContextError,
    DiagnosisGiveUpError,
    collect_data_gaps,
    compute_rates,
    compute_trends,
    decide_diagnosis_type,
    merge_data_gaps,
    metric_latest,
    normalize_diagnosis_payload,
)
from app.agent.diagnosis.prompts import DIAG_PROMPT_VERSION, build_messages
from app.agent.diagnosis.state import DiagnosisState
from app.agent.errors import AgentJSONError
from app.agent.graph.base import compile_graph, llm_retry_policy, new_state_graph
from app.agent.parsing.json_repair import invoke_json
from app.agent.state.base import merge_parse_attempts, merge_usage
from app.core.config import settings
from app.crud.diagnostic import crud_diagnostic
from app.db.session import SessionLocal
from app.models.enums import DiagnosisType
from app.models.metric import NoteMetric
from app.models.note import Cover, Note
from app.models.persona import Persona

logger = logging.getLogger("xhs-agent.diagnosis.graph")

#: 五个节点名（自检会断言节点齐全与边结构）
NODE_LOAD_CONTEXT = "load_context"
NODE_GENERATE = "generate_diagnosis"
NODE_VALIDATE = "validate_diagnosis"
NODE_PERSIST = "persist"
NODE_GIVE_UP = "give_up"

#: 正文进入提示词的节选长度（提示词优先装「事实数据」；正文过长只截前段）
_BODY_EXCERPT_LEN = 1200

#: 合法的诊断类型取值（与 ``DiagnosisType`` 枚举一致，勿手写）
_DIAGNOSIS_TYPE_VALUES = frozenset(member.value for member in DiagnosisType)


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
        "注意：只输出 JSON 对象本身；score 必须是 0~100 的数字或 null；"
        "suggestions 至少要有一条具体可执行的动作。"
    )


# ---------------------------------------------------------------------------
# 构图
# ---------------------------------------------------------------------------
def build_diagnosis_graph(
    *,
    model: BaseChatModel,
    use_checkpointer: bool = True,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译「单篇笔记 → 一次诊断结论」业务图。

    Args:
        model: 已注入的 LangChain ChatModel（真实 ``ChatOpenAI`` 或离线假模型）。
            **经闭包注入节点，绝不放进状态**（状态要序列化进检查点）。
        use_checkpointer: 是否绑定检查点（默认绑定）。
        checkpointer: 显式指定检查点实现；``None`` 表示取全局单例。

    Returns:
        CompiledStateGraph
    """

    # ------------------------------------------------------------ load_context
    def load_context_node(state: DiagnosisState) -> dict[str, Any]:
        """校验笔记 + 载入内容 / 人设 / 快照时间线 / 封面标记 + 本地确定性计算。"""
        started = time.perf_counter()

        note_raw = state.get("note_id")
        if not note_raw:
            raise DiagnosisContextError("缺少 note_id，无法诊断")
        try:
            note_uuid = uuid.UUID(str(note_raw))
        except (TypeError, ValueError) as exc:
            raise DiagnosisContextError(f"note_id 不是合法 UUID：{note_raw}") from exc

        # 手动类型校验（防御；路由层 schema 已先行拦截，正常不可达）
        manual_raw = state.get("diagnosis_type")
        manual_type: str | None = None
        if manual_raw:
            candidate = str(manual_raw).strip()
            if candidate and candidate not in _DIAGNOSIS_TYPE_VALUES:
                allowed = " / ".join(sorted(_DIAGNOSIS_TYPE_VALUES))
                raise DiagnosisContextError(
                    f"诊断类型非法「{candidate}」（仅支持 {allowed}）"
                )
            manual_type = candidate or None

        with SessionLocal() as db:
            note = db.get(Note, note_uuid)
            if note is None:
                raise DiagnosisContextError(f"笔记不存在：{note_uuid}")

            persona = db.get(Persona, note.persona_id) if note.persona_id else None

            # 快照时间线：按 captured_at **升序**（最早 → 最新；id 兜底保证顺序稳定）
            rows = list(
                db.execute(
                    select(NoteMetric)
                    .where(NoteMetric.note_id == note.id)
                    .order_by(NoteMetric.captured_at.asc(), NoteMetric.id.asc())
                )
                .scalars()
                .all()
            )
            # ORM 行 → 普通 dict（captured_at 用 ISO 字符串；检查点只序列化小字段）
            snapshots: list[dict[str, Any]] = [
                {
                    "id": str(row.id),
                    "captured_at": row.captured_at.isoformat() if row.captured_at else None,
                    "views": row.views,
                    "likes": row.likes,
                    "collects": row.collects,
                    "comments": row.comments,
                    "shares": row.shares,
                    "new_followers": row.new_followers,
                    "data_source": row.data_source.value if row.data_source else None,
                }
                for row in rows
            ]

            # 是否已有选中封面（应用层保证一个 note 至多一个 true）
            has_cover = (
                db.execute(
                    select(Cover.id)
                    .where(Cover.note_id == note.id, Cover.is_selected.is_(True))
                    .limit(1)
                ).first()
                is not None
            )

            body_text = note.body or ""
            published_at = note.published_at
            note_snapshot = {
                "id": str(note.id),
                "title": note.title,
                "body_length": len(body_text),
                "body_excerpt": body_text[:_BODY_EXCERPT_LEN],
                "tags": list(note.tags or []),
                "status": note.status.value if note.status else None,
                "published_at": published_at.isoformat() if published_at else None,
                "snapshot_count": len(snapshots),
            }
            persona_snapshot: dict[str, Any] | None = None
            if persona is not None:
                persona_snapshot = {
                    "id": str(persona.id),
                    "name": persona.name,
                    "positioning": persona.positioning,
                    "tone": persona.tone,
                }

        # ---- 本地确定性计算（纯函数；绝不把算术交给模型）----
        latest, latest_snapshot_id = metric_latest(snapshots)
        rates = compute_rates(snapshots)
        trends = compute_trends(snapshots, published_at)
        decision = decide_diagnosis_type(
            snapshots,
            manual_type,
            anomaly_drop_ratio=settings.DIAG_ANOMALY_DROP_RATIO,
        )
        data_gaps = collect_data_gaps(
            snapshots=snapshots,
            latest_values=latest,
            rates=rates,
            has_cover=has_cover,
            published_at=published_at,
            body_text=body_text,
            min_snapshots=settings.DIAG_MIN_SNAPSHOTS,
        )
        logger.info(
            "诊断上下文就绪：note=%s 快照=%s 类型=%s(%s) 缺口=%s",
            note_uuid,
            len(snapshots),
            decision["type"],
            decision["source"],
            len(data_gaps),
        )
        return {
            "note_snapshot": note_snapshot,
            "persona_snapshot": persona_snapshot,
            "has_cover": has_cover,
            "snapshot_count": len(snapshots),
            "latest": latest,
            "latest_snapshot_id": latest_snapshot_id,
            "rates": rates,
            "trends": trends,
            "diagnosis_type_resolved": decision["type"],
            "diagnosis_type_source": decision["source"],
            "diagnosis_type_reason": decision["reason"],
            "data_gaps": data_gaps,
            "node_trace": [
                {
                    "node": NODE_LOAD_CONTEXT,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    "snapshot_count": len(snapshots),
                    "diagnosis_type": decision["type"],
                    "diagnosis_type_source": decision["source"],
                    "data_gaps": len(data_gaps),
                }
            ],
        }

    # ------------------------------------------------------ generate_diagnosis
    def generate_diagnosis_node(state: DiagnosisState) -> dict[str, Any]:
        """调用模型产出诊断 JSON（容错与计数交给 invoke_json）。"""
        started = time.perf_counter()
        retry_count = int(state.get("retry_count") or 0) + 1

        messages = build_messages(
            note_snapshot=state.get("note_snapshot") or {},
            persona_snapshot=state.get("persona_snapshot"),
            latest=state.get("latest") or {},
            rates=state.get("rates") or {},
            trends=state.get("trends") or {},
            data_gaps=list(state.get("data_gaps") or []),
            diagnosis_type=state.get("diagnosis_type_resolved") or DiagnosisType.REVIEW.value,
            max_items=settings.DIAG_MAX_LIST_ITEMS,
            text_max_len=settings.DIAG_TEXT_MAX_LEN,
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

        raw_payload: dict[str, Any] = result.payload if isinstance(result.payload, dict) else {}
        return {
            "raw": raw_payload,
            "llm_raw": result.raw_text,
            "parse_failed": False,
            "retry_count": retry_count,
            "parse_attempts": merge_parse_attempts(state.get("parse_attempts"), result.attempts),
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

    # ------------------------------------------------------ validate_diagnosis
    def validate_diagnosis_node(state: DiagnosisState) -> dict[str, Any]:
        """本地结构校验：score 区间 / 数组条数长度 / suggestions 非空；冷启动强制不评分。"""
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

        cold_start = state.get("diagnosis_type_resolved") == DiagnosisType.COLD_START.value
        normalized, errors, warnings = normalize_diagnosis_payload(
            state.get("raw") or {},
            cold_start=cold_start,
            max_items=settings.DIAG_MAX_LIST_ITEMS,
            text_max_len=settings.DIAG_TEXT_MAX_LEN,
            score_min=settings.DIAG_SCORE_MIN,
            score_max=settings.DIAG_SCORE_MAX,
        )
        validation_ok = not errors
        return {
            "validation_ok": validation_ok,
            "field_errors": errors,
            "warnings": warnings,
            "feedback": "" if validation_ok else _build_feedback(errors),
            "score": normalized["score"],
            "strengths": normalized["strengths"],
            "weaknesses": normalized["weaknesses"],
            "reasons": normalized["reasons"],
            "suggestions": normalized["suggestions"],
            "model_data_gaps": normalized["model_data_gaps"],
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

    # ---------------------------------------------------------------- persist
    def persist_node(state: DiagnosisState) -> dict[str, Any]:
        """**单事务**写入一条 diagnostics（含第 4 部分全部字段）；失败整体回滚。"""
        started = time.perf_counter()
        note_raw = state.get("note_id")
        if not note_raw:
            raise DiagnosisContextError("缺少 note_id，无法落库")

        resolved = state.get("diagnosis_type_resolved") or DiagnosisType.REVIEW.value
        try:
            diagnosis_type = DiagnosisType(str(resolved))
        except ValueError:  # 防御：load_context 已保证取值合法
            diagnosis_type = DiagnosisType.REVIEW

        latest_snapshot_id = state.get("latest_snapshot_id")
        based_on = uuid.UUID(str(latest_snapshot_id)) if latest_snapshot_id else None

        local_gaps = [str(item) for item in (state.get("data_gaps") or [])]
        model_gaps = [str(item) for item in (state.get("model_data_gaps") or [])]
        merged_gaps = merge_data_gaps(local_gaps, model_gaps)

        # benchmark：**只装该笔记自身的相对指标 + 基准缺失声明**（绝不杜撰外部数字）
        benchmark = {
            "basis": "self_only",
            "latest_metrics": dict(state.get("latest") or {}),
            "rates": dict(state.get("rates") or {}),
            "trends": dict(state.get("trends") or {}),
            "thresholds": {"anomaly_drop_ratio": settings.DIAG_ANOMALY_DROP_RATIO},
            "external_benchmark": None,
            "benchmark_note": BENCHMARK_NOTE,
        }
        # llm_meta：严格白名单（五项，不多不少；绝不写 llm_raw / key / 提示词全文）
        llm_meta = {
            "prompt_version": DIAG_PROMPT_VERSION,
            "json_parse_attempts": int(state.get("parse_attempts") or 0),
            "diagnosis_type": diagnosis_type.value,
            "diagnosis_type_source": str(state.get("diagnosis_type_source") or "auto"),
            "data_gaps": merged_gaps,
        }

        row: dict[str, Any] = {
            "note_id": uuid.UUID(str(note_raw)),
            "based_on_metric_id": based_on,
            "diagnosis_type": diagnosis_type,
            "score": state.get("score"),
            "strengths": list(state.get("strengths") or []),
            "weaknesses": list(state.get("weaknesses") or []),
            "reasons": list(state.get("reasons") or []),
            "suggestions": list(state.get("suggestions") or []),
            "benchmark": benchmark,
            "llm_model": str(state.get("model") or settings.LLM_MODEL),
            "llm_meta": llm_meta,
        }

        with SessionLocal() as db:
            saved = crud_diagnostic.create_diagnostic(db, row)
            diagnostic_id = str(saved.id)

        warnings = [str(item) for item in (state.get("warnings") or [])]
        output = {
            "diagnostic_id": diagnostic_id,
            "note_id": str(note_raw),
            "diagnosis_type": diagnosis_type.value,
            "diagnosis_type_source": llm_meta["diagnosis_type_source"],
            "score": row["score"],
            "parse_attempts": llm_meta["json_parse_attempts"],
            "data_gaps": merged_gaps,
            "warnings": warnings,
            "persisted": True,
        }
        return {
            "diagnostic_id": diagnostic_id,
            "output": output,
            "node_trace": [
                {
                    "node": NODE_PERSIST,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    "diagnostic_id": diagnostic_id,
                    "diagnosis_type": diagnosis_type.value,
                    "scored": row["score"] is not None,
                }
            ],
        }

    # ---------------------------------------------------------------- give_up
    def give_up_node(state: DiagnosisState) -> dict[str, Any]:
        """结构校验连续超限：抛诊断放弃错误（run 记 failed，service 译 400）。"""
        retry_count = int(state.get("retry_count") or 0)
        field_errors = [str(item) for item in (state.get("field_errors") or [])][:3]
        detail = "；".join(field_errors) or "未知原因"
        raise DiagnosisGiveUpError(
            f"诊断失败：连续 {retry_count} 轮输出均不满足结构要求（{detail}）。"
            "可稍后重试；若反复出现，请确认所配模型稳定支持 JSON 输出后重试。",
            reason=f"retry_count={retry_count}",
        )

    # --------------------------------------------------------------- 条件边
    def route_after_validate(state: DiagnosisState) -> str:
        """合规 → persist；未超限 → 回炉；超限 → give_up（回路有界）。"""
        if state.get("validation_ok"):
            return NODE_PERSIST
        retry_count = int(state.get("retry_count") or 0)
        if retry_count < max(1, settings.AGENT_JSON_MAX_ATTEMPTS):
            return NODE_GENERATE
        return NODE_GIVE_UP

    # ------------------------------------------------------------------ 装配
    graph = new_state_graph("note-diagnosis", DiagnosisState)
    graph.add_node(NODE_LOAD_CONTEXT, load_context_node)
    # 调用 LLM 的节点统一带重试策略（仅重试 429/5xx/网络等瞬时错误）
    graph.add_node(NODE_GENERATE, generate_diagnosis_node, retry_policy=llm_retry_policy())
    graph.add_node(NODE_VALIDATE, validate_diagnosis_node)
    graph.add_node(NODE_PERSIST, persist_node)
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

    return compile_graph(graph, checkpointer=checkpointer, use_checkpointer=use_checkpointer)


__all__ = [
    "NODE_LOAD_CONTEXT",
    "NODE_GENERATE",
    "NODE_VALIDATE",
    "NODE_PERSIST",
    "NODE_GIVE_UP",
    "build_diagnosis_graph",
]
