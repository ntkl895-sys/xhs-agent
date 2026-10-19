"""规律提炼图：在 LangGraph harness 上的**第六个业务图**（第 18 步）。

链路（本账号已发布笔记 → 可复用爆款规律 → 向量化落库）：

    START
      │
    load_context          统计 published 数（样本门槛）；调 build_insight_facts
      │                   载入聚合事实（ORM → 普通 dict）——**零模型调用**
      │
      ├── 样本充足 ────────────────────────────────────────────► generate_patterns
      └── 样本不足 ────────────────────────────────────────────► insufficient_sample ─► END
          （不硬造规律：零模型调用、零写入，output.insufficient_sample=True，
            service 据此把 run 补记 partial）

    generate_patterns     模型（闭包注入）依据**程序算好的事实**产出规律 JSON；
      │                   JSON 容错与计数由 invoke_json 负责
    validate_patterns     本地结构校验（规律非空 / 每条有来源与证据；
      │                   逐条剔除不合格项，「全军覆没」才算硬伤）
      │
      ├── 合规 ────────────────────────────────────────────────► embed_and_persist ─► END
      ├── 不合规且 retry_count < AGENT_JSON_MAX_ATTEMPTS ──────► generate_patterns（有限回路）
      └── 不合规超限 ──────────────────────────────────────────► give_up ─► 抛错（run 记 failed）

要点：

- **真 LangGraph**：六节点 + ``add_conditional_edges`` 两条条件边 + 有界回路 +
  检查点（由 ``compile_graph`` 绑定）；回路的上界是 ``AGENT_JSON_MAX_ATTEMPTS``，
  最坏模型调用次数 = ``AGENT_JSON_MAX_ATTEMPTS × (1 + AGENT_NODE_MAX_RETRIES)``（3×3=9）；
- **模型与 embedder 经闭包注入**，绝不进状态；**向量也绝不进状态**（只在
  ``embed_and_persist`` 内用于落库）；
- **幂等**：按 ``(chunk_type, meta.content_hash)`` 查 active 记录 —— 已存在则跳过
  （reused），批内重复跳过（skipped），**绝不重复插入**；
- **红线（自检断言）**：规律只来自本账号本地数据（提示词明文禁止外部数字）；
  样本不足不硬造规律；只写 ``knowledge_snippets``，不改 notes / topics /
  note_metrics / diagnostics。
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START

from app.agent.embedding.base import Embedder
from app.agent.embedding.service import embed_texts
from app.agent.errors import AgentJSONError
from app.agent.graph.base import compile_graph, llm_retry_policy, new_state_graph
from app.agent.insights.contracts import (
    InsightsGiveUpError,
    build_insight_facts,
    normalize_patterns,
    scan_evidence_metrics,
)
from app.agent.insights.prompts import INSIGHTS_PROMPT_VERSION, build_messages
from app.agent.insights.state import InsightsState
from app.agent.parsing.json_repair import invoke_json
from app.agent.state.base import merge_parse_attempts, merge_usage
from app.core.config import settings
from app.crud.knowledge import crud_knowledge
from app.db.session import SessionLocal
from app.models.enums import ChunkType

logger = logging.getLogger("xhs-agent.insights.graph")

#: 六个节点名（自检会断言节点齐全与边结构）
NODE_LOAD_CONTEXT = "load_context"
NODE_GENERATE = "generate_patterns"
NODE_VALIDATE = "validate_patterns"
NODE_PERSIST = "embed_and_persist"
NODE_GIVE_UP = "give_up"
NODE_INSUFFICIENT = "insufficient_sample"

#: 规律文本前 N 字作为知识块 title（DB 列 String(200)，50 字安全）
_TITLE_MAX_LEN = 50


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
        "注意：只输出 JSON 对象本身；每条规律必须带 pattern / scenario / "
        "source_note_ids（只能引用给定笔记 id）/ evidence（引用给定数字）/ confidence。"
    )


def _pattern_hash(text: str) -> str:
    """规律文本的 content_hash（sha256，十六进制）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 构图
# ---------------------------------------------------------------------------
def build_insights_graph(
    *,
    model: BaseChatModel,
    embedder: Embedder | None = None,
    use_checkpointer: bool = True,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译「本账号已发布笔记 → 可复用爆款规律」业务图。

    Args:
        model: 已注入的 LangChain ChatModel（真实 ``ChatOpenAI`` 或离线假模型）。
            **经闭包注入节点，绝不放进状态**（状态要序列化进检查点）。
        embedder: 已注入的 embedder（离线自检用假实现）；``None`` 时节点内取
            :func:`~app.agent.embedding.service.get_embedder` 单例（懒加载）。
            **同样绝不进状态**。
        use_checkpointer: 是否绑定检查点（默认绑定）。
        checkpointer: 显式指定检查点实现；``None`` 表示取全局单例。

    Returns:
        CompiledStateGraph
    """

    # ------------------------------------------------------------ load_context
    def load_context_node(state: InsightsState) -> dict[str, Any]:
        """统计样本 + 载入提炼事实（全部本地计算；ORM 行转普通 dict）。"""
        started = time.perf_counter()
        with SessionLocal() as db:
            facts = build_insight_facts(
                db,
                min_published=int(settings.INSIGHTS_MIN_PUBLISHED),
            )
        sufficient = bool(facts.get("sufficient"))
        logger.info(
            "提炼上下文就绪：published=%s（门槛 %s，%s），数据缺口=%s",
            facts.get("sample_size"),
            facts.get("min_required"),
            "充足" if sufficient else "不足",
            len(facts.get("data_gaps") or []),
        )
        return {
            "facts": facts,
            "sample_sufficient": sufficient,
            "node_trace": [
                {
                    "node": NODE_LOAD_CONTEXT,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    "sample_size": facts.get("sample_size"),
                    "sufficient": sufficient,
                    "data_gaps": len(facts.get("data_gaps") or []),
                }
            ],
        }

    # --------------------------------------------------- insufficient_sample
    def insufficient_sample_node(state: InsightsState) -> dict[str, Any]:
        """样本不足：不硬造规律（零模型调用、零写入），组装 output 走收尾。

        ``output.insufficient_sample=True`` —— service 据此把 run 补记 partial，
        并把 ``data_gaps``（含「至少发布 N 篇并回填数据后再提炼」）透传给调用方。
        """
        started = time.perf_counter()
        facts = state.get("facts") or {}
        gaps = [str(item) for item in (facts.get("data_gaps") or [])]
        output: dict[str, Any] = {
            "run_id": state.get("run_id"),
            "generated": 0,
            "persisted": 0,
            "reused": 0,
            "skipped": 0,
            "snippet_ids": [],
            "sample_size": facts.get("sample_size"),
            "min_required": facts.get("min_required"),
            "prompt_version": INSIGHTS_PROMPT_VERSION,
            "rejected": 0,
            "data_gaps": gaps,
            "parse_attempts": 0,
            "insufficient_sample": True,
        }
        return {
            "output": output,
            "node_trace": [
                {
                    "node": NODE_INSUFFICIENT,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    "sample_size": facts.get("sample_size"),
                    "min_required": facts.get("min_required"),
                }
            ],
        }

    # ------------------------------------------------------- generate_patterns
    def generate_patterns_node(state: InsightsState) -> dict[str, Any]:
        """调用模型产出规律 JSON（容错与计数交给 invoke_json）。"""
        started = time.perf_counter()
        retry_count = int(state.get("retry_count") or 0) + 1

        messages = build_messages(
            facts=state.get("facts") or {},
            max_items=int(settings.INSIGHTS_PATTERN_MAX),
            text_max_len=int(settings.INSIGHTS_TEXT_MAX_LEN),
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

    # ------------------------------------------------------- validate_patterns
    def validate_patterns_node(state: InsightsState) -> dict[str, Any]:
        """本地结构校验：逐条剔除不合格项；「全军覆没」才算硬伤（回炉）。"""
        started = time.perf_counter()

        if state.get("parse_failed"):
            return {
                "validation_ok": False,
                "patterns": [],
                "rejected_patterns": [],
                "field_errors": ["模型输出无法解析为 JSON"],
                "warnings": [],
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

        facts = state.get("facts") or {}
        valid_ids = {
            str(note.get("note_id"))
            for note in (facts.get("notes") or [])
            if note.get("note_id")
        }
        normalized, errors, warnings = normalize_patterns(
            state.get("raw") or {},
            valid_note_ids=valid_ids,
            max_items=int(settings.INSIGHTS_PATTERN_MAX),
            text_max_len=int(settings.INSIGHTS_TEXT_MAX_LEN),
        )
        validation_ok = not errors
        return {
            "validation_ok": validation_ok,
            "patterns": list(normalized.get("patterns") or []),
            "rejected_patterns": list(normalized.get("rejected") or []),
            "field_errors": errors,
            "warnings": warnings,
            "feedback": "" if validation_ok else _build_feedback(errors),
            "node_trace": [
                {
                    "node": NODE_VALIDATE,
                    "status": "ok" if validation_ok else "invalid",
                    "duration_ms": _elapsed_ms(started),
                    "accepted": len(normalized.get("patterns") or []),
                    "rejected": len(normalized.get("rejected") or []),
                    "field_errors": len(errors),
                }
            ],
        }

    # ------------------------------------------------------ embed_and_persist
    def embed_and_persist_node(state: InsightsState) -> dict[str, Any]:
        """向量化 + 幂等落库（**单事务**插入；向量不进状态）。

        幂等口径：``(chunk_type, meta.content_hash)`` 命中 **active** 记录 → 跳过
        （reused）；同一批内重复 → 跳过（skipped）；**绝不重复插入、绝不更新**。
        """
        started = time.perf_counter()
        patterns = state.get("patterns") or []
        if not patterns:
            raise InsightsGiveUpError(
                "没有合格规律可落库。",
                reason="empty_patterns",
            )

        facts = state.get("facts") or {}
        sample_size = int(facts.get("sample_size") or 0)
        data_range = facts.get("data_range") or {}

        # ---- 1) 查重（只读；得到待嵌入清单）----
        plans: list[tuple[dict[str, Any], str]] = []
        reused = 0
        skipped = 0
        seen_hashes: set[str] = set()
        with SessionLocal() as db:
            for item in patterns:
                text = str(item.get("pattern") or "")
                content_hash = _pattern_hash(text)
                if content_hash in seen_hashes:
                    skipped += 1  # 同一批内重复：只写一条
                    continue
                seen_hashes.add(content_hash)
                existing = crud_knowledge.find_active_by_hash(
                    db,
                    chunk_type=ChunkType(str(item.get("pattern_type"))),
                    content_hash=content_hash,
                )
                if existing is not None:
                    reused += 1  # 已提炼过（幂等）：跳过，不重复插入
                    continue
                plans.append((item, content_hash))

        # ---- 2) 向量化（会话外；可能触发本地模型首次加载）----
        vectors: list[list[float]] = []
        if plans:
            vectors = embed_texts(
                [str(item.get("pattern") or "") for item, _ in plans],
                embedder=embedder,
            )
            if len(vectors) != len(plans):
                raise InsightsGiveUpError(
                    "向量化结果条数与规律条数不符，已放弃落库。",
                    reason="vector_count_mismatch",
                )

        # ---- 3) 单事务插入（失败整体回滚，不留半截记录）----
        snippet_ids: list[str] = []
        if plans:
            rows: list[dict[str, Any]] = []
            for (item, content_hash), vector in zip(plans, vectors):
                text = str(item.get("pattern") or "")
                rows.append(
                    {
                        "chunk_type": ChunkType(str(item.get("pattern_type"))),
                        "ref_type": None,  # 规律为多来源：来源放 meta.source_note_ids
                        "ref_id": None,
                        "title": text[:_TITLE_MAX_LEN] or None,
                        "content": text,
                        "embedding": vector,
                        "meta": {
                            "source_note_ids": list(item.get("source_note_ids") or []),
                            "evidence": {
                                "summary": item.get("evidence"),
                                "metrics": scan_evidence_metrics(
                                    str(item.get("evidence") or "")
                                ),
                            },
                            "scenario": item.get("scenario"),
                            "sample_size": sample_size,
                            "data_range": data_range,
                            "confidence": item.get("confidence"),
                            "content_hash": content_hash,
                            "prompt_version": INSIGHTS_PROMPT_VERSION,
                        },
                    }
                )
            with SessionLocal() as db:
                objs = crud_knowledge.insert_many(db, rows)
                snippet_ids = [str(obj.id) for obj in objs]

        persist_result = {
            "generated": len(patterns),
            "persisted": len(snippet_ids),
            "reused": reused,
            "skipped": skipped,
            "snippet_ids": snippet_ids,
        }
        output: dict[str, Any] = {
            "run_id": state.get("run_id"),
            **persist_result,
            "sample_size": sample_size,
            "min_required": facts.get("min_required"),
            "prompt_version": INSIGHTS_PROMPT_VERSION,
            "rejected": len(state.get("rejected_patterns") or []),
            "data_gaps": [
                str(item) for item in (facts.get("data_gaps") or [])
            ],
            "warnings": [str(item) for item in (state.get("warnings") or [])],
            "parse_attempts": int(state.get("parse_attempts") or 0),
            "insufficient_sample": False,
        }
        logger.info(
            "规律落库：generated=%s persisted=%s reused=%s skipped=%s",
            persist_result["generated"],
            persist_result["persisted"],
            persist_result["reused"],
            persist_result["skipped"],
        )
        return {
            "persist_result": persist_result,
            "output": output,
            "node_trace": [
                {
                    "node": NODE_PERSIST,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    **persist_result,
                }
            ],
        }

    # ---------------------------------------------------------------- give_up
    def give_up_node(state: InsightsState) -> dict[str, Any]:
        """结构校验连续超限：抛提炼放弃错误（run 记 failed，service 译 400）。"""
        retry_count = int(state.get("retry_count") or 0)
        field_errors = [str(item) for item in (state.get("field_errors") or [])][:3]
        detail = "；".join(field_errors) or "未知原因"
        raise InsightsGiveUpError(
            f"规律提炼失败：连续 {retry_count} 轮输出均不满足结构要求（{detail}）。"
            "可稍后重试；若反复出现，请确认所配模型稳定支持 JSON 输出后重试。",
            reason=f"retry_count={retry_count}",
        )

    # --------------------------------------------------------------- 条件边
    def route_after_load(state: InsightsState) -> str:
        """样本充足 → 生成；不足 → 特定收尾（零模型调用，partial）。"""
        if state.get("sample_sufficient"):
            return NODE_GENERATE
        return NODE_INSUFFICIENT

    def route_after_validate(state: InsightsState) -> str:
        """合规 → 落库；未超限 → 回炉；超限 → give_up（回路有界）。"""
        if state.get("validation_ok"):
            return NODE_PERSIST
        retry_count = int(state.get("retry_count") or 0)
        if retry_count < max(1, settings.AGENT_JSON_MAX_ATTEMPTS):
            return NODE_GENERATE
        return NODE_GIVE_UP

    # ------------------------------------------------------------------ 装配
    graph = new_state_graph("insights-extraction", InsightsState)
    graph.add_node(NODE_LOAD_CONTEXT, load_context_node)
    graph.add_node(
        NODE_GENERATE, generate_patterns_node, retry_policy=llm_retry_policy()
    )
    graph.add_node(NODE_VALIDATE, validate_patterns_node)
    graph.add_node(NODE_PERSIST, embed_and_persist_node)
    graph.add_node(NODE_GIVE_UP, give_up_node)
    graph.add_node(NODE_INSUFFICIENT, insufficient_sample_node)

    graph.add_edge(START, NODE_LOAD_CONTEXT)
    graph.add_conditional_edges(
        NODE_LOAD_CONTEXT,
        route_after_load,
        {
            NODE_GENERATE: NODE_GENERATE,
            NODE_INSUFFICIENT: NODE_INSUFFICIENT,
        },
    )
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
    graph.add_edge(NODE_INSUFFICIENT, END)

    return compile_graph(
        graph, checkpointer=checkpointer, use_checkpointer=use_checkpointer
    )


__all__ = [
    "NODE_GENERATE",
    "NODE_GIVE_UP",
    "NODE_INSUFFICIENT",
    "NODE_LOAD_CONTEXT",
    "NODE_PERSIST",
    "NODE_VALIDATE",
    "build_insights_graph",
]
