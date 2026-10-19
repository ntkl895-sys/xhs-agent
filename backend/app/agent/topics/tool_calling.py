"""选题图的可选「LLM 自主选工具」回路（tool calling，第 25 步）。

设计边界（红线）：

- **只读**：四个工具全部是查询（热点 / 知识块 / 历史选题 / 人设），V1 不暴露任何
  写操作（生成、发布、删除、落库），也不碰任何外部平台域名；
- **白名单**：工具名写死在 :data:`TOOL_NAMES`；模型请求白名单外的名字时**丢弃该调用**
  并记录 ``hallucinated_tools``，绝不执行、绝不因为幻觉工具而重试到上限；
- **作用域由闭包注入**：``persona_id`` 不作为工具参数暴露给模型，模型无法跨人设读数据；
- **收敛**：工具轮数硬上限（``TOPIC_TOOLCALLING_MAX_ROUNDS``），到顶直接进生成节点；
- **降级**：工具异常由 ``ToolNode(handle_tool_errors=True)`` 转成错误 ToolMessage，
  只影响上下文、不阻断生成，也不诱导模型反复重试。

拓扑（仅在开关打开时挂进选题图）：

    load_context → tool_decide ──(有合法 tool_calls 且未到上限)──► tool_exec
                        │                                              │
                        │◄──────────────(未到上限)────────────────────┘
                        └──(无调用 / 到上限)──► generate_topics → validate_dedupe → …
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

from app.agent.errors import AgentNonRetryableError
from app.agent.state.base import merge_usage
from app.agent.topics.prompts import build_messages
from app.core.config import settings
from app.crud.hotspot import crud_hotspot
from app.crud.knowledge import crud_knowledge
from app.crud.topic import crud_topic
from app.db.session import SessionLocal
from app.models.persona import Persona

logger = logging.getLogger("xhs-agent.topics.tool_calling")

NODE_TOOL_DECIDE = "tool_decide"
NODE_TOOL_EXEC = "tool_exec"
#: 生成节点名（与 graph.py 的 ``NODE_GENERATE`` 同值；此处不 import graph，避免成环）
NODE_GENERATE_NAME = "generate_topics"

#: 白名单（顺序即提示词中的展示顺序；新增工具必须同步加进这里）
TOOL_NAMES: tuple[str, ...] = (
    "search_hotspots",
    "search_knowledge",
    "list_topics",
    "get_persona",
)

#: 知识检索允许的 chunk_type（与选题图的 RAG 口径一致，另加人设知识）
_ALLOWED_CHUNK_TYPES: tuple[str, ...] = (
    "hit_pattern",
    "topic_template",
    "review_insight",
    "persona_knowledge",
)

#: 单条工具结果渲染进提示词的最大字符数 / 上下文字符总上限
_RESULT_MAX_CHARS = 600
_CONTEXT_MAX_CHARS = 4000

TOOL_INSTRUCTION_TEMPLATE = """【工具使用规则（只读）】
你可以在生成选题前调用工具补充上下文。可用工具：{tool_list}。
- 只允许调用上面列出的工具；**不要编造工具名或参数**，白名单外的调用会被拒绝。
- **只调用当前任务真正需要的工具**；不要为了"更完整"把所有工具都调一遍，
  多余的调用会降低选择精确率。
- 最多 {max_rounds} 轮工具调用；信息足够就立即停止，不要为了调用而调用。
- 工具报错时按「没有这条信息」继续，**不要重复重试**同一个工具。
- 工具只是补充上下文；最终仍要按系统提示的 JSON 结构输出选题。"""


# ---------------------------------------------------------------------------
# 入参 schema（pydantic：既给模型看，也做执行前校验）
# ---------------------------------------------------------------------------
class SearchHotspotsArgs(BaseModel):
    """热点检索入参。"""

    q: str = Field(default="", description="标题关键词（模糊匹配）；留空=按热度取近期条目")
    limit: int = Field(default=5, ge=1, le=10, description="返回条数 1~10")


class SearchKnowledgeArgs(BaseModel):
    """爆款规律 / 选题模板检索入参。"""

    query: str = Field(min_length=1, description="检索语句（自然语言，中文）")
    top_k: int = Field(default=3, ge=1, le=5, description="返回条数 1~5")
    chunk_types: list[str] | None = Field(
        default=None,
        description=(
            "可选的知识块类型过滤；允许 hit_pattern / topic_template / "
            "review_insight / persona_knowledge，缺省=前三种"
        ),
    )


class ListTopicsArgs(BaseModel):
    """历史选题查询入参（自动限定当前人设）。"""

    q: str = Field(default="", description="标题关键词；留空=最近选题")
    limit: int = Field(default=10, ge=1, le=20, description="返回条数 1~20")


class GetPersonaArgs(BaseModel):
    """人设详情（无参数）。"""

    pass


# ---------------------------------------------------------------------------
# 工具实现（薄封装现有 CRUD，全部只读）
# ---------------------------------------------------------------------------
def _build_tools(persona_id: str) -> list[BaseTool]:
    """按当前人设构造工具（作用域由闭包注入，模型无法指定别人的 persona_id）。"""
    persona_uuid = uuid.UUID(persona_id)

    @tool("search_hotspots", args_schema=SearchHotspotsArgs)
    def search_hotspots(q: str = "", limit: int = 5) -> str:
        """按关键词检索公开热榜条目（只读），返回标题 / 来源 / 热度 / 日期。"""
        with SessionLocal() as db:
            criteria = crud_hotspot.build_criteria(q=q.strip() or None, include_all_dates=True)
            rows = crud_hotspot.get_multi_sorted(
                db, criteria=criteria, sort="hot_score", limit=int(limit)
            )
            payload = [
                {
                    "title": row.title,
                    "source": row.source,
                    "hot_score": row.hot_score,
                    "hot_date": row.hot_date.isoformat() if row.hot_date else None,
                }
                for row in rows
            ]
        return json.dumps({"total": len(payload), "items": payload}, ensure_ascii=False)

    @tool("search_knowledge", args_schema=SearchKnowledgeArgs)
    def search_knowledge(
        query: str, top_k: int = 3, chunk_types: list[str] | None = None
    ) -> str:
        """检索本账号沉淀的爆款规律 / 选题模板 / 复盘心得（只读向量检索）。"""
        requested = [item for item in (chunk_types or []) if item in _ALLOWED_CHUNK_TYPES]
        types = tuple(requested) if requested else _ALLOWED_CHUNK_TYPES[:3]
        with SessionLocal() as db:
            rows = crud_knowledge.search_snippets(
                db, query_text=str(query), chunk_types=types, k=int(top_k)
            )
            payload = [
                {
                    "chunk_type": row.get("chunk_type"),
                    "title": row.get("title"),
                    "content": str(row.get("content") or "")[:300],
                    "similarity": row.get("similarity"),
                }
                for row in rows
            ]
        return json.dumps({"total": len(payload), "items": payload}, ensure_ascii=False)

    @tool("list_topics", args_schema=ListTopicsArgs)
    def list_topics(q: str = "", limit: int = 10) -> str:
        """查询当前人设的历史选题（只读），用于避免与已有选题重复。"""
        with SessionLocal() as db:
            criteria = crud_topic.build_criteria(persona_id=persona_uuid, q=q.strip() or None)
            rows = crud_topic.get_multi_with_names(db, criteria=criteria, limit=int(limit))
            payload = [
                {
                    "title": topic.title,
                    "angle": topic.angle,
                    "status": getattr(topic.status, "value", str(topic.status)),
                    "generated_at": topic.generated_at.isoformat()
                    if topic.generated_at
                    else None,
                }
                for topic, _persona_name, _account_name in rows
            ]
        return json.dumps({"total": len(payload), "items": payload}, ensure_ascii=False)

    @tool("get_persona", args_schema=GetPersonaArgs)
    def get_persona() -> str:
        """读取当前人设的完整设定（含禁忌与口头禅，提示词里未完整给出）。"""
        with SessionLocal() as db:
            persona = db.get(Persona, persona_uuid)
            if persona is None:
                return json.dumps({"found": False}, ensure_ascii=False)
            payload = {
                "found": True,
                "name": persona.name,
                "positioning": persona.positioning,
                "target_audience": persona.target_audience,
                "tone": persona.tone,
                "content_pillars": list(persona.content_pillars or []),
                "taboo": list(persona.taboo or []),
                "signature_style": persona.signature_style,
            }
        return json.dumps(payload, ensure_ascii=False)

    return [search_hotspots, search_knowledge, list_topics, get_persona]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _usage_of(response: Any) -> dict[str, int | None] | None:
    """从响应里取 token 用量（与 harness 的 ``_extract_usage`` 同口径）。"""
    usage = getattr(response, "usage_metadata", None)
    if not isinstance(usage, dict):
        return None
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def _summarize_payload(content: Any, *, limit: int = _RESULT_MAX_CHARS) -> str:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text[:limit]


def _tool_instruction(tools: list[BaseTool], max_rounds: int) -> str:
    return TOOL_INSTRUCTION_TEMPLATE.format(
        tool_list="、".join(f"{item.name}（{item.description}）" for item in tools),
        max_rounds=max_rounds,
    )


# ---------------------------------------------------------------------------
# 回路装配
# ---------------------------------------------------------------------------
def build_tool_loop(
    *,
    model: BaseChatModel,
    max_rounds: int,
) -> dict[str, Any]:
    """构造工具决策 / 执行节点与两个路由函数。

    Args:
        model: **不带 response_format** 的纯文本 ChatModel（带 json_object 与 tools
            在当前 langchain-openai 版本会要求 strict 工具，探针已实测冲突）。
        max_rounds: 工具执行轮数硬上限（>=1）。

    Returns:
        ``{"decide": callable, "exec": callable, "route_after_decide": callable,
        "route_after_exec": callable, "tools_factory": callable}``
    """
    limit = max(1, int(max_rounds))
    # persona 作用域按运行期状态解析；同一张图（= 单热点一次运行）内复用同一批工具
    cache: dict[str, tuple[list[BaseTool], dict[str, BaseTool]]] = {}

    def _scoped(state: dict[str, Any]) -> tuple[list[BaseTool], dict[str, BaseTool]]:
        persona_id = str(state.get("persona_id") or "").strip()
        if not persona_id:
            raise AgentNonRetryableError("工具模式缺少 persona_id，无法限定工具作用域")
        if persona_id not in cache:
            tools = _build_tools(persona_id)
            cache[persona_id] = (tools, {item.name: item for item in tools})
        return cache[persona_id]

    def _first_messages(state: dict[str, Any], tools: list[BaseTool]) -> list[BaseMessage]:
        """首轮消息：沿用现有提示词，只额外追加工具规则（不改模板正文）。"""
        return build_messages(
            persona_snapshot=state.get("persona_snapshot") or {},
            hotspot_snapshot=state.get("hotspot_snapshot") or {},
            per_hotspot=_coerce_per_hotspot(state.get("per_hotspot")),
            title_max_len=settings.TOPIC_TITLE_MAX_LEN,
            min_tags=settings.TOPIC_MIN_TAGS,
            max_tags=settings.TOPIC_MAX_TAGS,
            feedback=state.get("feedback") or None,
            rag_context=state.get("rag_context") or None,
            tool_instruction=_tool_instruction(tools, limit),
        )

    # ---------------------------------------------------------------- 决策节点
    def tool_decide_node(state: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        tools, tools_by_name = _scoped(state)
        messages: list[BaseMessage] = list(state.get("messages") or [])
        if not messages:
            messages = _first_messages(state, tools)

        bound = model.bind_tools(tools)
        response = bound.invoke(messages)

        raw_calls = list(getattr(response, "tool_calls", None) or [])
        invalid_calls = list(getattr(response, "invalid_tool_calls", None) or [])
        valid: list[dict[str, Any]] = []
        unknown: list[str] = []
        for call in raw_calls:
            name = str((call or {}).get("name") or "")
            if name in tools_by_name:
                valid.append(call)
            else:
                unknown.append(name or "（空工具名）")

        # 白名单外的调用：丢弃，不执行、不进入 ToolNode（避免它抛错并诱发重试）
        if unknown or invalid_calls:
            response = response.model_copy(update={"tool_calls": valid})

        rounds = int(state.get("tool_rounds") or 0)
        trace: dict[str, Any] = {
            "node": NODE_TOOL_DECIDE,
            "status": "ok",
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "round": rounds + 1,
            "tool_calls": [str(item.get("name")) for item in valid],
            "hallucinated": unknown,
            "invalid_args": len(invalid_calls),
            "json_parse_attempts": 1 if valid else 0,
        }
        update: dict[str, Any] = {
            "messages": [response],
            "usage": merge_usage(state.get("usage"), _usage_of(response)),
            "node_trace": [trace],
        }
        if unknown:
            update["hallucinated_tools"] = unknown
            logger.warning("工具模式：模型请求了白名单外的工具 %s（已丢弃）", unknown)
        return update

    # ---------------------------------------------------------------- 执行节点
    def tool_exec_node(state: dict[str, Any], config: RunnableConfig | None = None) -> dict[str, Any]:
        """执行上一轮 AIMessage 里的工具调用。

        ``config`` 由 LangGraph 注入：本版 ToolNode 需要图中的 runtime 配置，
        脱离图直接调用会报 ``Missing required config key``（自检里用一张最小图验证）。
        """
        started = time.perf_counter()
        tools, _tools_by_name = _scoped(state)
        node = ToolNode(tools, handle_tool_errors=True)
        messages: list[BaseMessage] = list(state.get("messages") or [])
        last = messages[-1] if messages else None
        calls = list(getattr(last, "tool_calls", None) or [])
        if not calls:
            return {"tool_rounds": int(state.get("tool_rounds") or 0)}

        produced: list[BaseMessage] = []
        events: list[dict[str, Any]] = []
        context = str(state.get("tool_context") or "")

        for call in calls:
            name = str(call.get("name"))
            call_started = time.perf_counter()
            ok = True
            error: str | None = None
            summary = ""
            try:
                # 逐个执行：拿到单次耗时，并让单个工具失败不影响同批其他工具
                result = node.invoke(
                    {"messages": [AIMessage(content="", tool_calls=[call])]}, config=config
                )
                batch = list(result.get("messages") or [])
                produced.extend(batch)
                for message in batch:
                    if isinstance(message, ToolMessage):
                        summary = _summarize_payload(message.content)
                        if getattr(message, "status", "success") == "error":
                            ok = False
                            error = summary[:200]
            except Exception as exc:  # noqa: BLE001 —— 工具异常只降级，不阻断
                ok = False
                error = f"{type(exc).__name__}: {exc}"
                produced.append(
                    ToolMessage(
                        content=f"工具执行失败：{type(exc).__name__}",
                        tool_call_id=str(call.get("id") or ""),
                        name=name,
                        status="error",
                    )
                )
            events.append(
                {
                    "tool": name,
                    "args": call.get("args") or {},
                    "ok": ok,
                    "result_summary": summary[:300],
                    "error": error,
                    "duration_ms": round((time.perf_counter() - call_started) * 1000, 3),
                }
            )
            if summary:
                context = f"{context}\n- [{name}] {summary}".strip()
                if len(context) > _CONTEXT_MAX_CHARS:
                    context = context[-_CONTEXT_MAX_CHARS:]

        rounds = int(state.get("tool_rounds") or 0) + 1
        return {
            "messages": produced,
            "tool_rounds": rounds,
            "tool_events": events,
            "tool_context": context,
            "node_trace": [
                {
                    "node": NODE_TOOL_EXEC,
                    "status": "ok" if all(item["ok"] for item in events) else "partial",
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "round": rounds,
                    "calls": [
                        {"tool": item["tool"], "ok": item["ok"], "duration_ms": item["duration_ms"]}
                        for item in events
                    ],
                }
            ],
        }

    # ---------------------------------------------------------------- 路由
    def route_after_decide(state: dict[str, Any]) -> str:
        messages: list[BaseMessage] = list(state.get("messages") or [])
        last = messages[-1] if messages else None
        calls = list(getattr(last, "tool_calls", None) or [])
        rounds = int(state.get("tool_rounds") or 0)
        if calls and rounds < limit:
            return NODE_TOOL_EXEC
        return NODE_GENERATE_NAME

    def route_after_exec(state: dict[str, Any]) -> str:
        if int(state.get("tool_rounds") or 0) >= limit:
            return NODE_GENERATE_NAME
        return NODE_TOOL_DECIDE

    return {
        "decide": tool_decide_node,
        "exec": tool_exec_node,
        "route_after_decide": route_after_decide,
        "route_after_exec": route_after_exec,
        "tools_factory": _build_tools,
    }


def _coerce_per_hotspot(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = settings.TOPIC_DEFAULT_PER_HOTSPOT
    return number if number > 0 else settings.TOPIC_DEFAULT_PER_HOTSPOT


__all__ = [
    "NODE_TOOL_DECIDE",
    "NODE_TOOL_EXEC",
    "TOOL_NAMES",
    "TOOL_INSTRUCTION_TEMPLATE",
    "build_tool_loop",
]
