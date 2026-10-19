"""离线冒烟图（**不含任何业务逻辑**，仅用于验证底座能力）。

用最小的三节点图端到端演示 Harness 的四项核心能力：

1. **状态流转**：``draft`` 节点产出 → ``finalize`` 节点收口成一个小型
   Pydantic 结构化对象（:class:`SmokeDraft`）；
2. **脏 JSON 修复**：模型返回带 ```` ```json ```` 围栏 / 裸控制字符 / 前后噪声的
   输出时，经 :func:`app.agent.parsing.json_repair.invoke_json` 成功解析，
   且 ``parse_attempts`` 被如实计数；
3. **检查点回读**：Postgres 检查点下，同一 ``thread_id`` 运行后可把状态写回并读回；
4. **人在回路**：``review`` 节点调用 ``interrupt()`` 暂停并抛出待确认载荷，
   随后用 ``Command(resume=...)`` 恢复并跑完。

全部离线：使用 ``FakeListChatModel`` / ``GenericFakeChatModel`` / :class:`ScriptedChatModel`，
**不联网、不耗 token、不需要 API Key**。

实现要点：模型通过**闭包**注入节点，**不放进状态**——状态会被序列化进检查点，
放一个不可 JSON 序列化的模型对象会直接让检查点写入失败。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.graph import END, START
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from app.agent.graph.base import compile_graph, llm_retry_policy, new_state_graph
from app.agent.parsing.json_repair import invoke_json
from app.agent.state.base import BaseAgentState, merge_parse_attempts, merge_usage

#: 冒烟运行在 agent_runs 里的 kind 标记（run_type 落 other）
SMOKE_KIND = "harness_smoke"


# ---------------------------------------------------------------------------
# 状态与产出模型
# ---------------------------------------------------------------------------
class SmokeState(BaseAgentState, total=False):
    """冒烟图状态：在统一基座状态上只追加一个业务无关字段。

    运行参数（如是否走人在回路）统一从 ``state["input"]`` 读，
    这样 ``run_agent`` 只需一个 ``inputs`` 参数，语义单一。
    """

    draft: dict[str, Any] | None


class SmokeDraft(BaseModel):
    """冒烟图最终产出的**小型结构化对象**。"""

    title: str = Field(min_length=1, max_length=100)
    keywords: list[str] = Field(default_factory=list)
    score: int = Field(ge=0, le=100, default=60)


# ---------------------------------------------------------------------------
# 假模型
# ---------------------------------------------------------------------------
class ScriptedChatModel(BaseChatModel):
    """按脚本行为的假模型：脚本项是字符串就返回它，是异常就抛出。

    用于**确定性地**验证重试语义（可重试错误按策略重试、不可重试错误立即失败），
    不联网、不耗 token。``calls`` 记录实际被调用次数。
    """

    script: list[Any] = Field(default_factory=list)
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # noqa: ANN001
        index = min(self.calls, len(self.script) - 1)
        item = self.script[index] if self.script else ""
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        message = AIMessage(
            content=item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


#: 脏 JSON 样本：代码围栏 + 字符串内裸换行/制表符 + 前后噪声
DIRTY_JSON_RESPONSE = (
    "好的，这是结果：\n"
    "```json\n"
    '{"title": "三分钟看懂保温杯",\n'
    '\t"keywords": ["保温杯",\n"选购"], "score": 88}\n'
    "```\n"
    "以上。"
)

#: 完全无法解析的样本（用于验证「纠正重问」与 JSON 失败路径）
UNPARSEABLE_RESPONSE = "抱歉，我无法给出结构化的结果，只有这段自由文本。"


def make_fake_model(responses: list[str] | None = None) -> BaseChatModel:
    """默认假模型：返回一段**带围栏 + 裸控制字符 + 前后噪声**的脏 JSON。"""
    if responses is None:
        responses = [DIRTY_JSON_RESPONSE]
    return FakeListChatModel(responses=responses)


def make_usage_fake_model(content: str, usage: dict[str, int]) -> BaseChatModel:
    """返回单条 AIMessage 的假模型，**带 usage_metadata**（用于断言 token 回填）。"""
    return GenericFakeChatModel(
        messages=iter([AIMessage(content=content, usage_metadata=dict(usage))])
    )


# ---------------------------------------------------------------------------
# 构图（模型经闭包注入，不进入状态）
# ---------------------------------------------------------------------------
def build_smoke_graph(
    model: BaseChatModel | None = None,
    *,
    use_checkpointer: bool = True,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译冒烟图（``START → draft → review → finalize → END``）。

    Args:
        model: 假模型；默认 :func:`make_fake_model`。
        use_checkpointer: 是否绑定检查点（interrupt 必须有检查点才生效）。
        checkpointer: 显式指定检查点实现。

    Returns:
        CompiledStateGraph
    """
    chosen = model or make_fake_model()

    def draft_node(state: SmokeState) -> dict[str, Any]:
        """调用模型产出结构化草稿（演示 JSON 修复与计数）。"""
        started = time.perf_counter()
        result = invoke_json(
            chosen,
            [
                SystemMessage(content="你是一个结构化输出助手，只输出 JSON。"),
                HumanMessage(content="请输出一个标题、若干关键词与 0~100 的分数。"),
            ],
        )
        draft = SmokeDraft.model_validate(result.payload)
        return {
            "draft": draft.model_dump(),
            "parse_attempts": merge_parse_attempts(state.get("parse_attempts"), result.attempts),
            "usage": merge_usage(state.get("usage"), result.usage),
            "node_trace": [
                {
                    "node": "draft",
                    "status": "ok",
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "json_parse_attempts": result.attempts,
                    "json_mode_used": result.json_mode_used,
                    "warnings": list(result.warnings),
                }
            ],
        }

    def review_node(state: SmokeState) -> dict[str, Any]:
        """可选的人在回路节点：``input.require_human`` 为真时 ``interrupt()`` 暂停。"""
        started = time.perf_counter()
        require_human = bool((state.get("input") or {}).get("require_human"))
        if not require_human:
            return {
                "node_trace": [
                    {
                        "node": "review",
                        "status": "skipped",
                        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    }
                ]
            }

        payload = {
            "question": "是否采用这份草稿？",
            "draft": state.get("draft"),
            "options": ["approve", "reject"],
        }
        # interrupt() 在此暂停整张图并把 payload 交给外部；
        # 恢复时返回 Command(resume=...) 携带的值。
        decision = interrupt(payload)

        draft = dict(state.get("draft") or {})
        if isinstance(decision, dict):
            if decision.get("title"):
                draft["title"] = str(decision["title"])
            if decision.get("score") is not None:
                draft["score"] = int(decision["score"])

        return {
            "draft": draft,
            "human_payload": payload,
            "node_trace": [
                {
                    "node": "review",
                    "status": "resumed",
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "decision": decision if isinstance(decision, (dict, str)) else str(decision),
                }
            ],
        }

    def finalize_node(state: SmokeState) -> dict[str, Any]:
        """收口成最终结构化对象。"""
        started = time.perf_counter()
        obj = SmokeDraft.model_validate(state.get("draft") or {})
        return {
            "output": obj.model_dump(),
            "node_trace": [
                {
                    "node": "finalize",
                    "status": "ok",
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            ],
        }

    graph = new_state_graph("harness-smoke", SmokeState)
    # 调用 LLM 的节点统一带重试策略（只重试瞬时错误）。
    # ⚠️ 这里**不设** LangGraph 的节点级 timeout：LangGraph 明确不支持**同步**节点的
    #  timeout（"sync Python execution cannot be safely cancelled in-process"），
    # 而本项目刻意只用同步栈。节点超时改由 HTTP 客户端超时（AGENT_NODE_TIMEOUT_SECONDS
    # 注入 ChatOpenAI 的 timeout）来保证，见 app/agent/llm/factory.py。
    graph.add_node("draft", draft_node, retry_policy=llm_retry_policy())
    graph.add_node("review", review_node)
    graph.add_node("finalize", finalize_node)
    graph.add_edge(START, "draft")
    graph.add_edge("draft", "review")
    graph.add_edge("review", "finalize")
    graph.add_edge("finalize", END)

    return compile_graph(graph, checkpointer=checkpointer, use_checkpointer=use_checkpointer)


def smoke_build_fn(
    model: BaseChatModel | None = None,
    *,
    use_checkpointer: bool = True,
) -> Callable[[], Any]:
    """返回一个**无参**工厂，供 :func:`app.agent.runner.run_agent` 使用。"""
    chosen = model or make_fake_model()
    return lambda: build_smoke_graph(chosen, use_checkpointer=use_checkpointer)


def smoke_inputs(*, require_human: bool = False) -> dict[str, Any]:
    """构造冒烟图的入参（纯数据，可安全写入 ``agent_runs.inputs``）。"""
    return {
        "kind": SMOKE_KIND,
        "require_human": require_human,
        "prompt": "演示：请产出结构化草稿",
    }


__all__ = [
    "SMOKE_KIND",
    "SmokeState",
    "SmokeDraft",
    "ScriptedChatModel",
    "make_fake_model",
    "make_usage_fake_model",
    "build_smoke_graph",
    "smoke_build_fn",
    "smoke_inputs",
    "DIRTY_JSON_RESPONSE",
    "UNPARSEABLE_RESPONSE",
]
