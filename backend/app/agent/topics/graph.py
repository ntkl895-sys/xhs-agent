"""选题图：在 LangGraph harness 上实现的**第一个业务图**。

链路（单热点 → K 个选题）：

    START
      │
    load_context        读 persona / hotspot，写纯 dict 快照
      │
    generate_topics     模型产出候选（带 RetryPolicy；JSON 容错与计数由 invoke_json 负责）
      │
    validate_dedupe     逐条校验 + 归一化判重（与库内未 discarded 选题比），产出 feedback
      │
      ├── validated 已达 per_hotspot ──────────────► persist ──► END
      ├── 未达且 retry_count < AGENT_JSON_MAX_ATTEMPTS ─► 回 generate_topics（带 feedback，有限回路）
      ├── 未达且已到上限，validated 非空 ──────────► persist ──► END（部分合格 → service 记 partial）
      └── 未达且已到上限，validated 为空 ──────────► give_up ──► 抛错（该热点 failed）

要点：

- **真 LangGraph**：多节点 + 条件边 + 有限回路 + 检查点（由 ``compile_graph`` 绑定）；
- **模型经闭包注入**，不进状态；运行参数从 ``state["input"]`` / ``extra_state`` 读；
- 每个节点**独立** ``SessionLocal()``，不跨线程/节点共享会话；
- 业务图**不改 harness 基座**：审计的自动部分由 ``run_agent`` 写，业务明细由 service 回填。
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START

from app.agent.errors import AgentNonRetryableError
from app.agent.graph.base import compile_graph, llm_retry_policy, new_state_graph
from app.agent.parsing.json_repair import invoke_json
from app.agent.state.base import merge_parse_attempts, merge_usage
from app.agent.topics.contracts import TopicCandidate
from app.agent.topics.prompts import TOPIC_PROMPT_VERSION, build_messages
from app.agent.topics.state import TopicState
from app.agent.topics.tool_calling import (
    NODE_GENERATE_NAME,
    NODE_TOOL_DECIDE,
    NODE_TOOL_EXEC,
    build_tool_loop,
)
from app.core.config import settings
from app.crud.topic import crud_topic, normalize_title
from app.db.session import SessionLocal
from app.models.enums import ChunkType, PersonaStatus, TopicStatus
from app.models.hotspot import Hotspot
from app.models.persona import Persona
from app.services.rag import safe_rag_block

logger = logging.getLogger("xhs-agent.topics.graph")

#: 节点名（自检会断言这四个节点存在）
NODE_LOAD_CONTEXT = "load_context"
NODE_GENERATE = "generate_topics"
NODE_VALIDATE = "validate_dedupe"
NODE_PERSIST = "persist"
NODE_GIVE_UP = "give_up"

_DIFFICULTIES = {"easy", "medium", "hard"}

#: 第 18 步 · RAG 反哺检索的知识块类型（爆款规律 / 选题模板 / 复盘心得）
_TOPIC_RAG_CHUNK_TYPES = (
    ChunkType.HIT_PATTERN.value,
    ChunkType.TOPIC_TEMPLATE.value,
    ChunkType.REVIEW_INSIGHT.value,
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _coerce_per_hotspot(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = settings.TOPIC_DEFAULT_PER_HOTSPOT
    return number if number > 0 else settings.TOPIC_DEFAULT_PER_HOTSPOT


def _as_str_list(value: Any) -> list[str]:
    """把任意值归一成去空白的字符串列表（非 list 返回空表）。"""
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            result.append(str(item))
    return result


def _safe_title(raw: Any) -> str:
    if isinstance(raw, dict):
        title = raw.get("title")
        if isinstance(title, str):
            return title.strip()
    return ""


def _validate_candidate(raw: Any) -> tuple[dict[str, Any] | None, str, str]:
    """校验单条候选。

    Returns:
        (合格记录 | None, 拒绝原因, 拒绝类别) —— 类别为 ``invalid``（不合格）或 ``duplicate``。
    """
    if not isinstance(raw, dict):
        return None, "候选不是 JSON 对象", "invalid"

    try:
        candidate = TopicCandidate.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 —— 统一转成可读原因
        return None, f"字段类型不合法：{type(exc).__name__}", "invalid"

    title = candidate.title.strip()
    if not title:
        return None, "缺少标题", "invalid"
    if len(title) > settings.TOPIC_TITLE_MAX_LEN:
        return None, f"标题超过 {settings.TOPIC_TITLE_MAX_LEN} 字（{len(title)}）", "invalid"

    outline = _as_str_list(candidate.content_outline)
    if not (settings.TOPIC_MIN_OUTLINE <= len(outline) <= settings.TOPIC_MAX_OUTLINE):
        return None, (
            f"内容大纲 {len(outline)} 条，需在 {settings.TOPIC_MIN_OUTLINE}~"
            f"{settings.TOPIC_MAX_OUTLINE} 之间"
        ), "invalid"

    tags = _as_str_list(candidate.tags)
    if not (settings.TOPIC_MIN_TAGS <= len(tags) <= settings.TOPIC_MAX_TAGS):
        return None, (
            f"标签 {len(tags)} 个，需在 {settings.TOPIC_MIN_TAGS}~"
            f"{settings.TOPIC_MAX_TAGS} 之间"
        ), "invalid"

    keywords = _as_str_list(candidate.target_keywords)
    if not keywords:
        return None, "缺少目标关键词", "invalid"

    difficulty = (candidate.difficulty or "").strip().lower()
    if difficulty not in _DIFFICULTIES:
        return None, f"难度取值非法（{candidate.difficulty!r}）", "invalid"

    confidence = candidate.confidence
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return None, "缺少置信度 confidence", "invalid"
    confidence = float(confidence)
    if not (0.0 <= confidence <= 1.0):
        return None, f"置信度越界（{confidence}）", "invalid"

    record: dict[str, Any] = {
        "title": title,
        "summary": (candidate.summary or None),
        "angle": (candidate.angle or None),
        "pillar": (candidate.pillar or None),
        "content_outline": outline,
        "tags": tags,
        "target_keywords": keywords,
        "difficulty": difficulty,
        "confidence": confidence,
    }
    return record, "", "invalid"


def _build_feedback(rejected: list[dict[str, Any]]) -> str:
    """把本轮拒绝原因汇总成一句反馈（只取不合格项，重复项不必反馈）。"""
    lines: list[str] = []
    for item in rejected:
        if item.get("kind") != "invalid":
            continue
        title = item.get("title") or "（未命名）"
        lines.append(f"- 「{title}」：{item.get('reason')}")
    return "\n".join(lines[:8])


# ---------------------------------------------------------------------------
# 构图
# ---------------------------------------------------------------------------
def build_topic_graph(
    *,
    model: BaseChatModel,
    tool_model: BaseChatModel | None = None,
    use_checkpointer: bool = True,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译「单热点 → K 个选题」业务图。

    Args:
        model: 已注入的 LangChain ChatModel（真实 ``ChatOpenAI`` 或离线假模型）。
            **经闭包注入节点，绝不放进状态**（状态要序列化进检查点）。
        tool_model: **不带 response_format** 的模型实例，仅供工具决策节点
            ``bind_tools`` 使用。``TOPIC_TOOLCALLING_ENABLED=false`` 时可为 None；
            打开时必填（带 json_object 的实例绑工具会被 langchain-openai 拒绝）。
        use_checkpointer: 是否绑定检查点（默认绑定，满足「必须带 checkpointer」要求）。
        checkpointer: 显式指定检查点实现；``None`` 表示取全局单例。

    Returns:
        CompiledStateGraph
    """

    # ------------------------------------------------------------ load_context
    def load_context_node(state: TopicState) -> dict[str, Any]:
        """读 persona / hotspot，写纯 dict 快照（独立会话）。"""
        started = time.perf_counter()
        persona_raw = state.get("persona_id")
        hotspot_raw = state.get("hotspot_id")
        with SessionLocal() as db:
            persona = db.get(Persona, uuid.UUID(str(persona_raw))) if persona_raw else None
            if persona is None:
                raise AgentNonRetryableError(f"人设不存在：{persona_raw}")
            if persona.status == PersonaStatus.ARCHIVED:
                raise AgentNonRetryableError(f"人设已归档，不能用于选题：{persona_raw}")

            hotspot = db.get(Hotspot, uuid.UUID(str(hotspot_raw))) if hotspot_raw else None
            if hotspot is None:
                raise AgentNonRetryableError(f"热点不存在：{hotspot_raw}")

            persona_snapshot = {
                "id": str(persona.id),
                "name": persona.name,
                "positioning": persona.positioning,
                "target_audience": persona.target_audience,
                "tone": persona.tone,
                "content_pillars": list(persona.content_pillars or []),
                "account_id": str(persona.account_id) if persona.account_id else None,
            }
            hotspot_snapshot = {
                "id": str(hotspot.id),
                "source": hotspot.source,
                "title": hotspot.title,
                "url": hotspot.url,
                "mobile_url": hotspot.mobile_url,
                "hot_score": hotspot.hot_score,
                "rank_position": hotspot.rank_position,
                "hot_date": hotspot.hot_date.isoformat() if hotspot.hot_date else None,
            }
            # ---- 第 18 步 · RAG 反哺（最小接入）：检索本账号历史经验 ----
            # 第 19 步 · 人设知识：传入 persona_id，把该人设的 active persona_knowledge
            # 一并纳入（读取失败只 WARNING，不影响既有行为）。
            # 只在此处「拼上下文」；节点拓扑 / 条件边 / 状态机 / 校验 / 持久化全未动。
            # 检索失败只 WARNING 降级为「无 RAG」（见 services/rag.safe_rag_block）。
            rag_context = safe_rag_block(
                db,
                query_text=f"{persona.positioning or ''} {hotspot.title or ''}".strip(),
                chunk_types=_TOPIC_RAG_CHUNK_TYPES,
                enabled=bool(settings.TOPIC_RAG_ENABLED),
                source="选题图 load_context",
                persona_id=str(persona.id),
            )
        return {
            "persona_snapshot": persona_snapshot,
            "hotspot_snapshot": hotspot_snapshot,
            "rag_context": rag_context,
            "account_id": persona_snapshot["account_id"],
            "node_trace": [
                {
                    "node": NODE_LOAD_CONTEXT,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                }
            ],
        }

    # --------------------------------------------------------- generate_topics
    def generate_topics_node(state: TopicState) -> dict[str, Any]:
        """调用模型产出候选（JSON 容错 + 解析计数）。"""
        started = time.perf_counter()
        per_hotspot = _coerce_per_hotspot(state.get("per_hotspot"))
        messages = build_messages(
            persona_snapshot=state.get("persona_snapshot") or {},
            hotspot_snapshot=state.get("hotspot_snapshot") or {},
            per_hotspot=per_hotspot,
            title_max_len=settings.TOPIC_TITLE_MAX_LEN,
            min_tags=settings.TOPIC_MIN_TAGS,
            max_tags=settings.TOPIC_MAX_TAGS,
            feedback=state.get("feedback") or None,
            # 第 18 步 · RAG 反哺：load_context 已检索好的「本账号历史经验」
            # （无 RAG / 检索失败时为 None → 提示词与接入前完全一致）
            rag_context=state.get("rag_context") or None,
            # 工具模式：只追加「工具收集到的上下文」；关闭时恒为 None → 提示词逐字节不变
            tool_context=state.get("tool_context") or None,
        )
        result = invoke_json(model, messages, want_json=True)
        payload = result.payload
        raw_items = payload.get("topics") if isinstance(payload, dict) else payload
        candidates = [item for item in raw_items if isinstance(item, dict)] if isinstance(
            raw_items, list
        ) else []
        return {
            "candidates": candidates,
            "llm_raw": result.raw_text,
            "parse_attempts": merge_parse_attempts(state.get("parse_attempts"), result.attempts),
            "usage": merge_usage(state.get("usage"), result.usage),
            "node_trace": [
                {
                    "node": NODE_GENERATE,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    "json_parse_attempts": result.attempts,
                    "json_mode_used": result.json_mode_used,
                }
            ],
        }

    # -------------------------------------------------------- validate_dedupe
    def validate_dedupe_node(state: TopicState) -> dict[str, Any]:
        """逐条校验 + 归一化判重，累积合格项并生成下一轮反馈。"""
        started = time.perf_counter()
        persona_id = state.get("persona_id")
        seen: set[str] = set()
        for item in state.get("validated") or []:
            key = normalize_title(str(item.get("title") or ""))
            if key:
                seen.add(key)

        existing: set[str] = set()
        if persona_id:
            with SessionLocal() as db:
                existing = crud_topic.existing_normalized_titles(db, uuid.UUID(str(persona_id)))

        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for raw in state.get("candidates") or []:
            record, reason, kind = _validate_candidate(raw)
            if record is None:
                rejected.append({"title": _safe_title(raw), "reason": reason, "kind": kind})
                continue
            key = normalize_title(record["title"])
            if key in seen or key in existing:
                rejected.append(
                    {
                        "title": record["title"],
                        "reason": "标题与该人设已有 / 本轮选题重复",
                        "kind": "duplicate",
                    }
                )
                continue
            seen.add(key)
            accepted.append(record)

        retry_count = int(state.get("retry_count") or 0) + 1
        return {
            "validated": accepted,
            "rejected": rejected,
            "feedback": _build_feedback(rejected),
            "retry_count": retry_count,
            "node_trace": [
                {
                    "node": NODE_VALIDATE,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    "accepted": len(accepted),
                    "rejected": len(rejected),
                }
            ],
        }

    # ----------------------------------------------------------------- persist
    def persist_node(state: TopicState) -> dict[str, Any]:
        """在**单事务**内批量插入 topics；失败整体回滚，不留半成品。"""
        started = time.perf_counter()
        per_hotspot = _coerce_per_hotspot(state.get("per_hotspot"))
        validated = state.get("validated") or []
        chosen = validated[:per_hotspot]
        if not chosen:
            raise AgentNonRetryableError("没有合格选题可落库")

        persona_snapshot = state.get("persona_snapshot") or {}
        hotspot_snapshot = state.get("hotspot_snapshot") or {}
        parse_attempts = int(state.get("parse_attempts") or 0)
        llm_model = state.get("model") or settings.LLM_MODEL
        generated_at = datetime.now(timezone.utc)

        source_trace_entry = {
            "source": hotspot_snapshot.get("source"),
            "title": hotspot_snapshot.get("title"),
            "url": hotspot_snapshot.get("url"),
            "mobile_url": hotspot_snapshot.get("mobile_url"),
            "hot_date": hotspot_snapshot.get("hot_date"),
            "hot_score": hotspot_snapshot.get("hot_score"),
            "rank_position": hotspot_snapshot.get("rank_position"),
        }

        rows: list[dict[str, Any]] = []
        for item in chosen:
            confidence = item.get("confidence")
            rows.append(
                {
                    "account_id": persona_snapshot.get("account_id"),
                    "persona_id": state.get("persona_id"),
                    "title": item["title"],
                    "angle": item.get("angle"),
                    "summary": item.get("summary"),
                    "pillar": item.get("pillar"),
                    "content_outline": item.get("content_outline"),
                    "tags": item.get("tags"),
                    "target_keywords": item.get("target_keywords"),
                    "source_hotspot_ids": [state.get("hotspot_id")],
                    "source_trace": [source_trace_entry],
                    "heat_score": hotspot_snapshot.get("hot_score"),
                    "difficulty": item.get("difficulty"),
                    "score": confidence,  # V1：综合排序分 == 模型置信度
                    "llm_model": llm_model,
                    "meta": {
                        "prompt_version": TOPIC_PROMPT_VERSION,
                        "json_parse_attempts": parse_attempts,
                        "confidence": confidence,
                    },
                    "status": TopicStatus.DRAFT,
                    "generated_at": generated_at,
                }
            )

        with SessionLocal() as db:
            objs = crud_topic.insert_many(db, rows)
            topic_ids = [str(obj.id) for obj in objs]

        output = {
            "topic_ids": topic_ids,
            "persisted": len(topic_ids),
            "rejected": list(state.get("rejected") or []),
        }
        return {
            "topic_ids": topic_ids,
            "output": output,
            "node_trace": [
                {
                    "node": NODE_PERSIST,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    "persisted": len(topic_ids),
                }
            ],
        }

    # ----------------------------------------------------------------- give_up
    def give_up_node(state: TopicState) -> dict[str, Any]:
        """重试耗尽仍无合格选题：抛不可重试错误，让 run_agent 记 failed。"""
        retry_count = int(state.get("retry_count") or 0)
        raise AgentNonRetryableError(
            f"重试 {retry_count} 轮后仍无合格选题，该热点放弃"
        )

    # --------------------------------------------------------------- 条件边
    def route_after_validate(state: TopicState) -> str:
        per_hotspot = _coerce_per_hotspot(state.get("per_hotspot"))
        validated = state.get("validated") or []
        retry_count = int(state.get("retry_count") or 0)
        if len(validated) >= per_hotspot:
            return NODE_PERSIST
        if retry_count >= max(1, settings.AGENT_JSON_MAX_ATTEMPTS):
            return NODE_PERSIST if validated else NODE_GIVE_UP
        return NODE_GENERATE

    # ------------------------------------------------------------------ 装配
    graph = new_state_graph("topic-generation", TopicState)
    graph.add_node(NODE_LOAD_CONTEXT, load_context_node)
    # 调用 LLM 的节点统一带重试策略（只重试瞬时错误）
    graph.add_node(NODE_GENERATE, generate_topics_node, retry_policy=llm_retry_policy())
    graph.add_node(NODE_VALIDATE, validate_dedupe_node)
    graph.add_node(NODE_PERSIST, persist_node)
    graph.add_node(NODE_GIVE_UP, give_up_node)

    graph.add_edge(START, NODE_LOAD_CONTEXT)
    if settings.TOPIC_TOOLCALLING_ENABLED:
        # 可选分支：load_context → [tool_decide ⇄ tool_exec] → generate_topics
        if tool_model is None:
            raise AgentNonRetryableError(
                "TOPIC_TOOLCALLING_ENABLED=true，但未注入 tool_model"
                "（需要不带 response_format 的模型实例才能 bind_tools）"
            )
        if NODE_GENERATE_NAME != NODE_GENERATE:  # 防止两处常量漂移
            raise AgentNonRetryableError(
                f"工具回路节点名不一致：{NODE_GENERATE_NAME!r} != {NODE_GENERATE!r}"
            )
        loop = build_tool_loop(
            model=tool_model,
            max_rounds=settings.TOPIC_TOOLCALLING_MAX_ROUNDS,
        )
        graph.add_node(NODE_TOOL_DECIDE, loop["decide"], retry_policy=llm_retry_policy())
        graph.add_node(NODE_TOOL_EXEC, loop["exec"])
        graph.add_edge(NODE_LOAD_CONTEXT, NODE_TOOL_DECIDE)
        graph.add_conditional_edges(
            NODE_TOOL_DECIDE,
            loop["route_after_decide"],
            {NODE_TOOL_EXEC: NODE_TOOL_EXEC, NODE_GENERATE: NODE_GENERATE},
        )
        graph.add_conditional_edges(
            NODE_TOOL_EXEC,
            loop["route_after_exec"],
            {NODE_TOOL_DECIDE: NODE_TOOL_DECIDE, NODE_GENERATE: NODE_GENERATE},
        )
    else:
        # 关闭时节点 / 边与接入前完全一致（不新增任何节点）
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
    "NODE_TOOL_DECIDE",
    "NODE_TOOL_EXEC",
    "build_topic_graph",
]
