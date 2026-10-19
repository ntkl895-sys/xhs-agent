"""多版本创作图：在 LangGraph harness 上实现的**第七个业务图**（第 23 步 · V2 增强（二））。

链路（单个已通过选题 → N 个可横向对比的版本）：

    START
      │
    load_context         复用第 12 步的取数（load_creation_context，同一份逻辑）
      │
    generate_variants    单次调用产出 N 版 JSON 数组（invoke_json 三层兜底）
      │
    review_variants      逐版判定：rejected（缺标题/正文）/ blocked（违禁命中）/ qualified
      │                  （违禁扫描复用 services.forbidden_source，知识库词表优先）
      ├── 存在 rejected 且未到上限 ──► 回 generate_variants（带 feedback，有限回路）
      ├── 有可用版本（usable>0）───► persist_variants ──► END
      └── 全部不合格且到上限 ──────► give_up ──► 抛错（该次 run 记 failed）

    persist_variants：**单事务**插入「合格版本」多条 notes（同 variant_group_id =
    run_id，初始 draft、未采用）+ 把来源选题由 approved 置为 writing（只过渡一次）；
    blocked / rejected 版本**不落库**（计数与明细进 outputs，随生成响应返回）。

要点：

- **真 LangGraph**：多节点 + 条件边 + 有限回路 + 检查点（由 ``compile_graph`` 绑定）；
- **模型经闭包注入**，不进状态；运行参数（topic_id / n / angles）从 ``state`` 读
  （service 经 ``extra_state`` 注入）；
- 每个节点**独立** ``SessionLocal()``，不跨节点共享会话；
- 违禁扫描与第 12 步**同一实现**（``services.forbidden_source.scan_with_knowledge``，
  知识库词表优先）；语义差异只在本图：**命中即拦截该版本（blocked）、不落库**，
  不阻断其他版本（partial 语义）——第 12 步的「命中只提示、草稿保留」不在本图适用；
- blocked / rejected 不落库 ⇒ 作品库中不存在不可采用的半合规内容（不落脏数据）。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START

from app.agent.content.forbidden_words import build_content_check
from app.agent.content.graph import load_creation_context
from app.agent.content_variants.contracts import (
    DEFAULT_VARIANTS,
    OUTCOME_BLOCKED,
    OUTCOME_QUALIFIED,
    OUTCOME_REJECTED,
    VariantDraft,
)
from app.agent.content_variants.prompts import (
    CONTENT_VARIANTS_PROMPT_VERSION,
    build_variants_messages,
)
from app.agent.content_variants.state import ContentVariantsState
from app.agent.errors import AgentNonRetryableError
from app.agent.graph.base import compile_graph, llm_retry_policy, new_state_graph
from app.agent.parsing.json_repair import invoke_json
from app.agent.state.base import merge_parse_attempts, merge_usage
from app.core.config import settings
from app.crud.note import crud_note
from app.db.session import SessionLocal
from app.models.enums import ComplianceStatus, NoteStatus
from app.services.forbidden_source import scan_with_knowledge

logger = logging.getLogger("xhs-agent.content_variants.graph")

#: 节点名（自检会断言五个节点齐全）
NODE_LOAD_CONTEXT = "load_context"
NODE_GENERATE = "generate_variants"
NODE_REVIEW = "review_variants"
NODE_PERSIST = "persist_variants"
NODE_GIVE_UP = "give_up"

#: 模型可能把版本数组包在这些键里（宽容解包，只解一层）
_WRAPPER_KEYS = ("variants", "versions", "items", "data", "result")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def unwrap_variants(payload: Any) -> list[Any] | None:
    """宽容解包：顶层数组直接用；对象则尝试常见包装键（只解一层）。

    解不出数组 → ``None``（由 review 判为「无任何版本」并触发有限回路）。
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in _WRAPPER_KEYS:
            inner = payload.get(key)
            if isinstance(inner, list):
                return inner
    return None


def normalize_variants(raw_list: list[Any] | None, n: int) -> tuple[list[dict[str, Any]], int]:
    """把任意模型输出归一成 ``[{angle, title, body, tags, type_errors}]``（纯 dict）。

    - 只考虑前 ``n`` 条；超出的条数以返回值第二项计数（``extra_ignored``）；
    - 单条类型非法 → 记 ``type_errors``（review 判 rejected），**不**丢弃整批；
    - 结构层面不做「空标题 / 空正文」判定 —— 那是 review 的职责。
    """
    if not raw_list:
        return [], 0
    considered = raw_list[: max(0, n)]
    extra_ignored = max(0, len(raw_list) - len(considered))

    drafts: list[dict[str, Any]] = []
    for item in considered:
        if not isinstance(item, dict):
            drafts.append(
                {
                    "angle": "",
                    "title": "",
                    "body": "",
                    "tags": [],
                    "type_errors": ["数组元素不是 JSON 对象"],
                }
            )
            continue
        try:
            draft = VariantDraft.model_validate(item)
        except Exception as exc:  # noqa: BLE001 —— 统一转成可读原因
            drafts.append(
                {
                    "angle": "",
                    "title": "",
                    "body": "",
                    "tags": [],
                    "type_errors": [f"字段类型不合法：{type(exc).__name__}"],
                }
            )
            continue
        drafts.append(
            {
                "angle": (draft.angle or "").strip(),
                "title": (draft.title or "").strip(),
                "body": (draft.body or "").strip(),
                "tags": list(draft.tags),
                "type_errors": [],
            }
        )
    return drafts, extra_ignored


def _reject_reason(title: str, body: str) -> str:
    """缺标题 / 缺正文的可读原因。"""
    if not title and not body:
        return "标题与正文均为空"
    if not title:
        return "缺少标题"
    return "缺少正文"


def _blocked_reason(hits: list[dict[str, Any]]) -> str:
    """违禁命中的可读原因（词表 + 字段去重，截断防超长）。"""
    words: list[str] = []
    for item in hits:
        word = str(item.get("word") or "")
        if word and word not in words:
            words.append(word)
    preview = "、".join(words[:6])
    return f"命中风险词：{preview}" + ("…" if len(words) > 6 else "")


def _build_feedback(results: list[dict[str, Any]], n: int) -> str:
    """把本轮不合格 / 被拦截的版本汇总成下一轮反馈（仅在需要回炉时调用）。"""
    lines: list[str] = []
    if not results:
        lines.append(
            f"- 上一次输出没有解析出任何版本（应为包含 {n} 个对象的 JSON 数组）。"
        )
    if len(results) < n:
        lines.append(f"- 本次只返回了 {len(results)} 版，应返回 {n} 版。")
    for item in results:
        if item.get("outcome") == OUTCOME_REJECTED:
            lines.append(f"- 第 {int(item.get('index') or 0) + 1} 版不合格：{item.get('reason')}")
    for item in results:
        if item.get("outcome") == OUTCOME_BLOCKED:
            preview = "、".join(
                str(hit.get("word")) for hit in (item.get("hits") or [])[:6]
            )
            lines.append(
                f"- 第 {int(item.get('index') or 0) + 1} 版命中风险词（{preview}），"
                "请改写为不依赖这些表述的说法（从源头规避，而非仅替换同义词）"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 构图
# ---------------------------------------------------------------------------
def build_content_variants_graph(
    *,
    model: BaseChatModel,
    use_checkpointer: bool = True,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译「单选题 → N 个可对比版本」业务图。

    Args:
        model: 已注入的 LangChain ChatModel（真实 ``ChatOpenAI`` 或离线假模型）。
            **经闭包注入节点，绝不放进状态**（状态要序列化进检查点）。
        use_checkpointer: 是否绑定检查点（默认绑定）。
        checkpointer: 显式指定检查点实现；``None`` 表示取全局单例。

    Returns:
        CompiledStateGraph
    """

    # ------------------------------------------------------------ load_context
    def load_context_node(state: ContentVariantsState) -> dict[str, Any]:
        """读 topic / persona / account，写纯 dict 快照（复用第 12 步同一份逻辑）。"""
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

    # ------------------------------------------------------- generate_variants
    def generate_variants_node(state: ContentVariantsState) -> dict[str, Any]:
        """调用模型产出 N 版数组（JSON 容错 + 解析计数）。"""
        started = time.perf_counter()
        n = int(state.get("variant_n") or DEFAULT_VARIANTS)
        messages = build_variants_messages(
            persona_snapshot=state.get("persona_snapshot") or {},
            topic_snapshot=state.get("topic_snapshot") or {},
            n=n,
            angles=state.get("variant_angles") or None,
            title_max_len=settings.NOTE_TITLE_MAX_LEN,
            body_min_len=settings.NOTE_BODY_MIN_LEN,
            body_max_len=settings.NOTE_BODY_MAX_LEN,
            min_tags=settings.NOTE_MIN_TAGS,
            max_tags=settings.NOTE_MAX_TAGS,
            feedback=state.get("variant_feedback") or None,
            rag_context=state.get("rag_context") or None,
        )
        result = invoke_json(model, messages, want_json=True)
        drafts, extra_ignored = normalize_variants(unwrap_variants(result.payload), n)
        return {
            "variant_drafts": drafts,
            "variant_extra_ignored": extra_ignored,
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
                    "drafts": len(drafts),
                }
            ],
        }

    # --------------------------------------------------------- review_variants
    def review_variants_node(state: ContentVariantsState) -> dict[str, Any]:
        """逐版判定：rejected（缺标题/正文）/ blocked（违禁命中）/ qualified。

        - 违禁扫描复用第 12 步同一实现（``scan_with_knowledge``，知识库词表优先）；
        - 本图语义：**命中即拦截该版本**（不落库），不阻断其他版本（partial 语义）；
        - 仅在存在 rejected（含解析不到任何版本）时给下一轮反馈，回路有界。
        """
        started = time.perf_counter()
        drafts = list(state.get("variant_drafts") or [])
        results: list[dict[str, Any]] = []

        for index, draft in enumerate(drafts):
            angle = str(draft.get("angle") or "").strip()
            title = str(draft.get("title") or "").strip()
            body = str(draft.get("body") or "").strip()
            tags = [str(item) for item in (draft.get("tags") or [])]
            entry: dict[str, Any] = {
                "index": index,
                "angle": angle,
                "title": title,
                "body": body,
                "tags": tags,
            }
            type_errors = [str(item) for item in (draft.get("type_errors") or [])]
            if type_errors:
                entry.update(
                    outcome=OUTCOME_REJECTED,
                    reason="；".join(type_errors),
                    hits=None,
                )
            elif not title or not body:
                entry.update(
                    outcome=OUTCOME_REJECTED,
                    reason=_reject_reason(title, body),
                    hits=None,
                )
            else:
                hits = scan_with_knowledge(title=title, body=body, tags=tags)
                if hits:
                    entry.update(
                        outcome=OUTCOME_BLOCKED,
                        reason=_blocked_reason(hits),
                        hits=hits,
                    )
                else:
                    entry.update(outcome=OUTCOME_QUALIFIED, reason=None, hits=None)
            results.append(entry)

        rejected = sum(1 for item in results if item["outcome"] == OUTCOME_REJECTED)
        blocked = sum(1 for item in results if item["outcome"] == OUTCOME_BLOCKED)
        qualified = sum(1 for item in results if item["outcome"] == OUTCOME_QUALIFIED)
        feedback = _build_feedback(results, int(state.get("variant_n") or DEFAULT_VARIANTS)) if rejected or not results else ""
        retry_count = int(state.get("retry_count") or 0) + 1

        return {
            "variant_results": results,
            "variant_feedback": feedback,
            "retry_count": retry_count,
            "node_trace": [
                {
                    "node": NODE_REVIEW,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    "qualified": qualified,
                    "blocked": blocked,
                    "rejected": rejected,
                }
            ],
        }

    # -------------------------------------------------------- persist_variants
    def persist_variants_node(state: ContentVariantsState) -> dict[str, Any]:
        """**单事务**写入「合格版本」notes + 把 topic 由 approved 置为 writing；失败整体回滚。

        blocked / rejected 版本不落库；无合格版本时不写任何行、**不动选题状态**。
        """
        started = time.perf_counter()
        results = list(state.get("variant_results") or [])
        qualified = [item for item in results if item.get("outcome") == OUTCOME_QUALIFIED]

        topic_id_raw = state.get("topic_id")
        account_id_raw = state.get("account_id")
        persona_id_raw = state.get("persona_id")
        if not topic_id_raw or not account_id_raw:
            raise AgentNonRetryableError("缺少 topic_id 或 account_id，无法落库")

        # 批次号 = 本次运行的 run_id（审计可直连；接口不返回 llm_raw，仅返回该分组号）
        group_id = str(state.get("run_id") or uuid.uuid4())
        parse_attempts = int(state.get("parse_attempts") or 0)

        rows: list[dict[str, Any]] = []
        for item in qualified:
            rows.append(
                {
                    "account_id": uuid.UUID(str(account_id_raw)),
                    "persona_id": uuid.UUID(str(persona_id_raw)) if persona_id_raw else None,
                    "topic_id": uuid.UUID(str(topic_id_raw)),
                    "title": str(item.get("title") or "").strip()[: settings.NOTE_TITLE_MAX_LEN],
                    "body": str(item.get("body") or ""),
                    "tags": [str(tag) for tag in (item.get("tags") or [])],
                    # qualified 的定义即「0 命中」，content_check 与第 12 步结构一致
                    "content_check": build_content_check([]),
                    "compliance_status": ComplianceStatus.PASSED,
                    "status": NoteStatus.DRAFT,  # 即使合格也不自动 ready，必须人工审核
                    "version": 1,
                    "llm_meta": {
                        "prompt_version": CONTENT_VARIANTS_PROMPT_VERSION,
                        "json_parse_attempts": parse_attempts,
                        "model": state.get("model") or settings.LLM_MODEL,
                        # 第 23 步 · V2 增强（二）：多版本批次信息（零迁移，存现有 JSONB）
                        "variant": {
                            "group_id": group_id,
                            "angle": str(item.get("angle") or ""),
                            "index": int(item.get("index") or 0),
                            "adopted": False,
                        },
                    },
                }
            )

        note_ids: list[str] = []
        if rows:
            try:
                with SessionLocal() as db:
                    notes = crud_note.create_batch_with_topic_transition(
                        db, rows, topic_id=uuid.UUID(str(topic_id_raw))
                    )
                    note_ids = [str(note.id) for note in notes]
            except ValueError as exc:  # 选题状态 / 存在性问题（防御性，正常已被 load_context 拦住）
                raise AgentNonRetryableError(str(exc)) from exc

        blocked_items = [item for item in results if item.get("outcome") == OUTCOME_BLOCKED]
        rejected_items = [item for item in results if item.get("outcome") == OUTCOME_REJECTED]
        output = {
            "variant_group_id": group_id,
            "requested": int(state.get("variant_n") or 0),
            "generated": len(results),
            "persisted": len(note_ids),
            "blocked": len(blocked_items),
            "rejected": len(rejected_items),
            "extra_ignored": int(state.get("variant_extra_ignored") or 0),
            "note_ids": note_ids,
            "angles": [str(item.get("angle") or "") for item in results],
            "blocked_details": [
                {
                    "angle": str(item.get("angle") or ""),
                    "title": str(item.get("title") or ""),
                    "words": [
                        str(hit.get("word")) for hit in (item.get("hits") or [])[:10]
                    ],
                }
                for item in blocked_items
            ],
            "rejected_details": [
                {"index": int(item.get("index") or 0), "reason": item.get("reason")}
                for item in rejected_items
            ],
        }
        return {
            "note_ids": note_ids,
            "output": output,
            "node_trace": [
                {
                    "node": NODE_PERSIST,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    "note_ids": note_ids,
                    "persisted": len(note_ids),
                }
            ],
        }

    # ----------------------------------------------------------------- give_up
    def give_up_node(state: ContentVariantsState) -> dict[str, Any]:
        """重试耗尽且无任何可用版本：抛不可重试错误，让 ``run_agent`` 记 failed。"""
        retry_count = int(state.get("retry_count") or 0)
        reasons = "；".join(
            str(item.get("reason"))
            for item in (state.get("variant_results") or [])
            if item.get("outcome") == OUTCOME_REJECTED
        )[:200]
        raise AgentNonRetryableError(
            f"重试 {retry_count} 轮后仍未产出任何合格版本（{reasons or '全部版本不合格'}），放弃"
        )

    # --------------------------------------------------------------- 条件边
    def route_after_review(state: ContentVariantsState) -> str:
        """存在 rejected 且未到上限 → 回炉；有可用版本 → 落库；否则放弃。

        口径（与第 12 步的条件结构同构）：

        - ``rejected``（含「解析不到任何版本」）触发有限回路，且**只**在存在 rejected 时回炉；
        - ``blocked`` 不触发回炉（与第 12 步「命中风险词不单独强制回炉」一致）；
        - 到上限后：有可用版本（usable>0）→ 落库（可能含 blocked，落库的只有 qualified）；
          无可用版本 → give_up。
        """
        results = state.get("variant_results") or []
        rejected = sum(1 for item in results if item.get("outcome") == OUTCOME_REJECTED)
        usable = len(results) - rejected
        retry_count = int(state.get("retry_count") or 0)
        need_regen = (not results) or rejected > 0
        if need_regen and retry_count < max(1, settings.AGENT_JSON_MAX_ATTEMPTS):
            return NODE_GENERATE
        if usable > 0:
            return NODE_PERSIST
        return NODE_GIVE_UP

    # ------------------------------------------------------------------ 装配
    graph = new_state_graph("content-variants", ContentVariantsState)
    graph.add_node(NODE_LOAD_CONTEXT, load_context_node)
    # 调用 LLM 的节点统一带重试策略（只重试瞬时错误）
    graph.add_node(NODE_GENERATE, generate_variants_node, retry_policy=llm_retry_policy())
    graph.add_node(NODE_REVIEW, review_variants_node)
    graph.add_node(NODE_PERSIST, persist_variants_node)
    graph.add_node(NODE_GIVE_UP, give_up_node)

    graph.add_edge(START, NODE_LOAD_CONTEXT)
    graph.add_edge(NODE_LOAD_CONTEXT, NODE_GENERATE)
    graph.add_edge(NODE_GENERATE, NODE_REVIEW)
    graph.add_conditional_edges(
        NODE_REVIEW,
        route_after_review,
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
    "NODE_REVIEW",
    "NODE_PERSIST",
    "NODE_GIVE_UP",
    "build_content_variants_graph",
    "normalize_variants",
    "unwrap_variants",
]
