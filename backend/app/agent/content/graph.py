"""创作图：在 LangGraph harness 上实现的**第二个业务图**（第 12 步）。

链路（单个已通过选题 → 一篇笔记草稿）：

    START
      │
    load_context          读 topic / persona / account，写纯 dict 快照
      │
    generate_content      模型产出 {title, body, tags}（带 RetryPolicy；
      │                   JSON 容错与计数由 invoke_json 负责）
    validate_compliance   ① 字段硬伤校验（长度 / 标签数量 / 类型）
      │                   ② 本地确定性违禁词扫描 → content_check / compliance_status
      │
      ├── 字段合格 ─────────────────────────────────► persist ──► END
      ├── 字段不合格且 retry_count < AGENT_JSON_MAX_ATTEMPTS ► 回 generate_content
      │                                             （带 feedback：字段问题 + 本轮命中）
      ├── 已到上限且**有可用正文** ─────────────────► persist ──► END（降级落库 → service 记 partial）
      └── 已到上限且无可用正文 ─────────────────────► give_up ──► 抛错（该次 run 记 failed）

要点：

- **真 LangGraph**：多节点 + 条件边 + 有限回路 + 检查点（由 ``compile_graph`` 绑定）；
- **模型经闭包注入**，不进状态；运行参数从 ``state["input"]`` / ``extra_state`` 读；
- 每个节点**独立** ``SessionLocal()``，不跨节点共享会话；
- **违禁词词表来源（第 19 步）**：``validate_compliance`` 经
  ``services/forbidden_source.scan_with_knowledge`` 扫描 —— 知识库
  （``chunk_type='forbidden_word'`` 的 active 全量）优先，读取异常 / 词表为空 /
  扫描出错时**回退内置词表**（保守兜底 + WARNING），任何情况下不跳过检查；
- **违禁词命中不单独强制回炉**：仅写入 ``content_check``（warning）与 feedback，
  使「因字段硬伤回炉」时一并要求规避；不动正文、不丢草稿、不阻断落库。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START

from app.agent.content.contracts import ContentDraft
from app.agent.content.forbidden_words import (
    CATEGORY_LABELS,
    build_content_check,
    compliance_of,
)
from app.agent.content.prompts import CONTENT_PROMPT_VERSION, build_messages
from app.agent.content.state import ContentState
from app.agent.errors import AgentNonRetryableError
from app.agent.graph.base import compile_graph, llm_retry_policy, new_state_graph
from app.agent.parsing.json_repair import invoke_json
from app.agent.state.base import merge_parse_attempts, merge_usage
from app.core.config import settings
from app.crud.note import crud_note
from app.db.session import SessionLocal
from app.models.account import Account
from app.models.enums import ChunkType, ComplianceStatus, NoteStatus, PersonaStatus, TopicStatus
from app.models.persona import Persona
from app.models.topic import Topic
from app.services.forbidden_source import scan_with_knowledge
from app.services.rag import safe_rag_block

logger = logging.getLogger("xhs-agent.content.graph")

#: 节点名（自检会断言五个节点齐全）
NODE_LOAD_CONTEXT = "load_context"
NODE_GENERATE = "generate_content"
NODE_VALIDATE = "validate_compliance"
NODE_PERSIST = "persist"
NODE_GIVE_UP = "give_up"

#: 模型可能把草稿包在这些键里（宽容解包，只解一层）
_WRAPPER_KEYS = ("note", "data", "draft", "content", "result")

#: 第 18 步 · RAG 反哺检索的知识块类型（爆款规律 / 复盘心得）
_CONTENT_RAG_CHUNK_TYPES = (
    ChunkType.HIT_PATTERN.value,
    ChunkType.REVIEW_INSIGHT.value,
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _unwrap_payload(payload: Any) -> Any:
    """宽容解包：模型偶发把对象包在 note / data / content 等键里。"""
    if isinstance(payload, dict):
        for key in _WRAPPER_KEYS:
            inner = payload.get(key)
            if isinstance(inner, dict):
                return inner
    return payload


def _normalize_draft(raw: Any) -> dict[str, Any]:
    """把任意模型输出归一成 ``{title, body, tags, type_errors}``（纯 dict，可序列化）。"""
    if not isinstance(raw, dict):
        return {"title": "", "body": "", "tags": [], "type_errors": ["模型输出不是 JSON 对象"]}
    try:
        draft = ContentDraft.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 —— 统一转成可读原因
        return {
            "title": "",
            "body": "",
            "tags": [],
            "type_errors": [f"字段类型不合法：{type(exc).__name__}"],
        }
    return {
        "title": (draft.title or "").strip(),
        "body": (draft.body or "").strip(),
        "tags": list(draft.tags),
        "type_errors": [],
    }


def _normalize_tags(raw: Any) -> list[str]:
    """标签归一：剥前导 ``#``、去空白 / 空项、去重（保留顺序）。"""
    if not isinstance(raw, list):
        return []
    tags: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        token = item.strip().lstrip("#").strip()
        if token and token not in tags:
            tags.append(token)
    return tags


def _has_usable_draft(draft: dict[str, Any]) -> bool:
    """「有可用正文」口径：标题非空 **且** 正文非空（足够落一条可人工修订的草稿）。"""
    title = draft.get("title")
    body = draft.get("body")
    return bool(isinstance(title, str) and title.strip()) and bool(
        isinstance(body, str) and body.strip()
    )


def _build_feedback(field_errors: list[str], hits: list[dict[str, Any]]) -> str:
    """把字段问题与违禁词命中汇总成下一轮反馈。"""
    lines: list[str] = []
    for error in field_errors:
        lines.append(f"- 字段问题：{error}")
    if hits:
        sample = "、".join(
            f"「{item['word']}」（{CATEGORY_LABELS.get(item.get('category', ''), item.get('category'))}）"
            for item in hits[:6]
        )
        lines.append(
            f"- 命中风险词：{sample}。请改写为不依赖这些表述的说法（从源头规避，而非仅替换同义词）"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 上下文装载（第 23 步 · V2 增强（二）起被「多版本创作图」共用）
# ---------------------------------------------------------------------------
def load_creation_context(topic_raw: Any) -> dict[str, Any]:
    """读 topic / persona / account + RAG 反哺块，返回**纯 dict 快照**。

    第 23 步 · V2 增强（二）抽取为模块级函数，供 ``content`` 图与
    ``content_variants`` 图**共用同一份取数逻辑**（禁止复制第二套）；
    行为与第 12 步的实现逐字一致：

    - 选题必须存在且 ``status=approved``（否则 ``AgentNonRetryableError``；
      服务层已先给出业务错误，这里是图内防御性复核）；
    - 归属账号以 ``topic.account_id`` 为准，缺省回退 ``persona.account_id``；
    - 第 18 步 RAG / 第 19 步人设知识：检索失败只 WARNING 降级为「无 RAG」。

    Returns:
        ``{topic_snapshot, persona_snapshot, account_snapshot, rag_context,
        account_id, persona_id}``（全部可 JSON 序列化）。
    """
    with SessionLocal() as db:
        topic = db.get(Topic, uuid.UUID(str(topic_raw))) if topic_raw else None
        if topic is None:
            raise AgentNonRetryableError(f"选题不存在：{topic_raw}")
        if topic.status != TopicStatus.APPROVED:
            raise AgentNonRetryableError(
                f"选题状态为 {topic.status.value}，只有 approved 才能创作"
            )

        persona = db.get(Persona, topic.persona_id) if topic.persona_id else None
        if persona is None:
            raise AgentNonRetryableError("选题未关联人设，无法创作")
        if persona.status == PersonaStatus.ARCHIVED:
            raise AgentNonRetryableError(f"人设已归档，不能用于创作：{persona.id}")

        # 归属账号**以 topic 关联为准**；topic 未关联时回退 persona 的账号
        account_id = topic.account_id or persona.account_id
        account = db.get(Account, account_id) if account_id else None
        if account is None:
            raise AgentNonRetryableError(
                "无法确定归属账号（topic.account_id 与 persona.account_id 均为空）"
            )

        topic_snapshot = {
            "id": str(topic.id),
            "title": topic.title,
            "angle": topic.angle,
            "summary": topic.summary,
            "pillar": topic.pillar,
            "content_outline": list(topic.content_outline or []),
            "target_keywords": list(topic.target_keywords or []),
            "difficulty": topic.difficulty.value if topic.difficulty else None,
            "persona_id": str(topic.persona_id) if topic.persona_id else None,
        }
        persona_snapshot = {
            "id": str(persona.id),
            "name": persona.name,
            "positioning": persona.positioning,
            "target_audience": persona.target_audience,
            "tone": persona.tone,
            "content_pillars": list(persona.content_pillars or []),
            "taboo": list(persona.taboo or []),
        }
        account_snapshot = {"id": str(account.id), "name": account.name}

        # ---- 第 18 步 · RAG 反哺（最小接入）：用选题标题/方向检索本账号规律 ----
        # 第 19 步 · 人设知识：传入 persona_id，把该人设的 active persona_knowledge
        # 一并纳入（读取失败只 WARNING，不影响既有行为）。
        # 检索失败只 WARNING 降级为「无 RAG」（见 services/rag.safe_rag_block）。
        rag_context = safe_rag_block(
            db,
            query_text=f"{topic.title or ''} {topic.angle or ''}".strip(),
            chunk_types=_CONTENT_RAG_CHUNK_TYPES,
            enabled=bool(settings.CONTENT_RAG_ENABLED),
            source="创作图 load_context",
            persona_id=str(persona.id),
        )

    return {
        "topic_snapshot": topic_snapshot,
        "persona_snapshot": persona_snapshot,
        "account_snapshot": account_snapshot,
        "rag_context": rag_context,
        "account_id": account_snapshot["id"],
        "persona_id": persona_snapshot["id"],
    }


# ---------------------------------------------------------------------------
# 构图
# ---------------------------------------------------------------------------
def build_content_graph(
    *,
    model: BaseChatModel,
    use_checkpointer: bool = True,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译「单选题 → 一篇笔记草稿」业务图。

    Args:
        model: 已注入的 LangChain ChatModel（真实 ``ChatOpenAI`` 或离线假模型）。
            **经闭包注入节点，绝不放进状态**（状态要序列化进检查点）。
        use_checkpointer: 是否绑定检查点（默认绑定）。
        checkpointer: 显式指定检查点实现；``None`` 表示取全局单例。

    Returns:
        CompiledStateGraph
    """

    # ------------------------------------------------------------ load_context
    def load_context_node(state: ContentState) -> dict[str, Any]:
        """读 topic / persona / account，写纯 dict 快照（独立会话）。

        第 23 步 · V2 增强（二）起复用模块级 :func:`load_creation_context`
        （与 content_variants 图共用同一份取数逻辑；本节点行为逐字不变）。
        """
        started = time.perf_counter()
        context = load_creation_context(state.get("topic_id"))
        return {
            **context,
            "node_trace": [
                {
                    "node": NODE_LOAD_CONTEXT,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                }
            ],
        }

    # -------------------------------------------------------- generate_content
    def generate_content_node(state: ContentState) -> dict[str, Any]:
        """调用模型产出草稿（JSON 容错 + 解析计数）。"""
        started = time.perf_counter()
        messages = build_messages(
            persona_snapshot=state.get("persona_snapshot") or {},
            topic_snapshot=state.get("topic_snapshot") or {},
            title_max_len=settings.NOTE_TITLE_MAX_LEN,
            body_min_len=settings.NOTE_BODY_MIN_LEN,
            body_max_len=settings.NOTE_BODY_MAX_LEN,
            min_tags=settings.NOTE_MIN_TAGS,
            max_tags=settings.NOTE_MAX_TAGS,
            feedback=state.get("feedback") or None,
            # 第 18 步 · RAG 反哺：load_context 已检索好的「本账号历史经验」
            # （无 RAG / 检索失败时为 None → 提示词与接入前完全一致）
            rag_context=state.get("rag_context") or None,
        )
        result = invoke_json(model, messages, want_json=True)
        draft = _normalize_draft(_unwrap_payload(result.payload))
        return {
            "draft": draft,
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

    # ----------------------------------------------------- validate_compliance
    def validate_compliance_node(state: ContentState) -> dict[str, Any]:
        """字段硬伤校验 + 本地违禁词扫描，产出 content_check / compliance_status / feedback。"""
        started = time.perf_counter()
        draft = state.get("draft") or {}
        field_errors: list[str] = [str(item) for item in (draft.get("type_errors") or [])]

        title_raw = draft.get("title")
        title = title_raw.strip() if isinstance(title_raw, str) else ""
        if not title:
            field_errors.append("标题为空")
        elif len(title) > settings.NOTE_TITLE_MAX_LEN:
            field_errors.append(
                f"标题超过 {settings.NOTE_TITLE_MAX_LEN} 字（当前 {len(title)}）"
            )

        body_raw = draft.get("body")
        body = body_raw.strip() if isinstance(body_raw, str) else ""
        if not body:
            field_errors.append("正文为空")
        elif len(body) < settings.NOTE_BODY_MIN_LEN:
            field_errors.append(
                f"正文少于 {settings.NOTE_BODY_MIN_LEN} 字（当前 {len(body)}）"
            )

        tags = _normalize_tags(draft.get("tags"))
        if not (settings.NOTE_MIN_TAGS <= len(tags) <= settings.NOTE_MAX_TAGS):
            field_errors.append(
                f"标签 {len(tags)} 个，需在 {settings.NOTE_MIN_TAGS}~{settings.NOTE_MAX_TAGS} 之间"
            )

        # ---- 违禁词扫描（第 19 步：知识库词表优先，异常 / 空表回退内置）----
        # content_check / compliance_status 结构与第 12 步一致；命中不阻断落库。
        hits = scan_with_knowledge(title=title, body=body, tags=tags)
        compliance = compliance_of(hits)
        content_check = build_content_check(hits)
        retry_count = int(state.get("retry_count") or 0) + 1

        return {
            # 回写归一后的草稿（剥 # 的标签、去空白的标题 / 正文），persist 直接用
            "draft": {"title": title, "body": body, "tags": tags, "type_errors": []},
            "field_errors": field_errors,
            "forbidden_hits": hits,
            "content_check": content_check,
            "compliance_status": compliance.value,
            "feedback": _build_feedback(field_errors, hits),
            "retry_count": retry_count,
            "node_trace": [
                {
                    "node": NODE_VALIDATE,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    "field_errors": len(field_errors),
                    "forbidden_hits": len(hits),
                }
            ],
        }

    # ----------------------------------------------------------------- persist
    def persist_node(state: ContentState) -> dict[str, Any]:
        """**单事务**写入 notes + 把 topic 由 approved 置为 writing；失败整体回滚。"""
        started = time.perf_counter()
        draft = state.get("draft") or {}
        if not _has_usable_draft(draft):
            raise AgentNonRetryableError("没有可用的标题 / 正文，无法落库")

        topic_id_raw = state.get("topic_id")
        account_id_raw = state.get("account_id")
        persona_id_raw = state.get("persona_id")
        if not topic_id_raw or not account_id_raw:
            raise AgentNonRetryableError("缺少 topic_id 或 account_id，无法落库")

        tags = _normalize_tags(draft.get("tags"))
        compliance_value = state.get("compliance_status") or ComplianceStatus.PASSED.value
        row: dict[str, Any] = {
            "account_id": uuid.UUID(str(account_id_raw)),
            "persona_id": uuid.UUID(str(persona_id_raw)) if persona_id_raw else None,
            "topic_id": uuid.UUID(str(topic_id_raw)),
            "title": str(draft.get("title") or "").strip()[: settings.NOTE_TITLE_MAX_LEN],
            "body": str(draft.get("body") or ""),
            "tags": tags,
            "content_check": state.get("content_check"),
            "compliance_status": ComplianceStatus(str(compliance_value)),
            "status": NoteStatus.DRAFT,  # 即使 passed 也不自动 ready，必须人工审核
            "version": 1,
            "llm_meta": {
                "prompt_version": CONTENT_PROMPT_VERSION,
                "json_parse_attempts": int(state.get("parse_attempts") or 0),
                "model": state.get("model") or settings.LLM_MODEL,
            },
        }

        try:
            with SessionLocal() as db:
                note = crud_note.create_with_topic_transition(
                    db, row, topic_id=uuid.UUID(str(topic_id_raw))
                )
                note_id = str(note.id)
        except ValueError as exc:  # 选题状态 / 存在性问题（防御性，正常已被 load_context 拦住）
            raise AgentNonRetryableError(str(exc)) from exc

        field_errors = list(state.get("field_errors") or [])
        hits = list(state.get("forbidden_hits") or [])
        output = {
            "note_id": note_id,
            "generated": 1,
            "compliance_status": row["compliance_status"].value,
            "forbidden_hits": len(hits),
            "tags_count": len(tags),
            "title": row["title"],
            # 重试耗尽仍降级落库时为 True（service 据此把 run 补记 partial）
            "degraded": bool(field_errors),
            "field_errors": field_errors,
        }
        return {
            "note_id": note_id,
            "output": output,
            "node_trace": [
                {
                    "node": NODE_PERSIST,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    "note_id": note_id,
                    "degraded": bool(field_errors),
                }
            ],
        }

    # ----------------------------------------------------------------- give_up
    def give_up_node(state: ContentState) -> dict[str, Any]:
        """重试耗尽且无可用草稿：抛不可重试错误，让 ``run_agent`` 记 failed。"""
        retry_count = int(state.get("retry_count") or 0)
        errors = "；".join(str(item) for item in (state.get("field_errors") or [])[:3])
        raise AgentNonRetryableError(
            f"重试 {retry_count} 轮后仍无可用草稿（{errors or '未知原因'}），放弃"
        )

    # --------------------------------------------------------------- 条件边
    def route_after_validate(state: ContentState) -> str:
        """字段合格 → persist；未超限 → 回炉；超限 → 有可用正文则降级落库，否则放弃。"""
        field_errors = state.get("field_errors") or []
        if not field_errors:
            return NODE_PERSIST
        retry_count = int(state.get("retry_count") or 0)
        if retry_count < max(1, settings.AGENT_JSON_MAX_ATTEMPTS):
            return NODE_GENERATE
        if _has_usable_draft(state.get("draft") or {}):
            return NODE_PERSIST
        return NODE_GIVE_UP

    # ------------------------------------------------------------------ 装配
    graph = new_state_graph("content-creation", ContentState)
    graph.add_node(NODE_LOAD_CONTEXT, load_context_node)
    # 调用 LLM 的节点统一带重试策略（只重试瞬时错误）
    graph.add_node(NODE_GENERATE, generate_content_node, retry_policy=llm_retry_policy())
    graph.add_node(NODE_VALIDATE, validate_compliance_node)
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
    "build_content_graph",
    "load_creation_context",
]
