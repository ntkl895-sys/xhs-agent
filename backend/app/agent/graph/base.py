"""构图 / 编译 / 运行配置的统一辅助。

业务图（选题 / 创作 / 诊断 / RAG）都应当走这里，以保证：

- 检查点绑定方式一致（interrupt/resume 才能跨进程生效）；
- ``recursion_limit`` 统一来自配置，不会出现某张图忘记设置；
- 调用 LLM 的节点用**统一的可重试策略**（只重试瞬时错误）。
"""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.graph import StateGraph
from langgraph.types import RetryPolicy

from app.agent.errors import AgentRetryableError
from app.agent.state.base import BaseAgentState
from app.core.config import settings

#: LangGraph 把 recursion_limit 放在 config 顶层，不是 configurable 里
_RECURSION_KEY = "recursion_limit"


def new_thread_id() -> str:
    """生成一个新的线程 id（检查点按 thread_id 归属，interrupt/resume 依赖它）。"""
    return str(uuid.uuid4())


def make_config(thread_id: str, **extra: Any) -> dict[str, Any]:
    """构造 ``graph.invoke`` 用的运行配置。

    Args:
        thread_id: 线程 id（``new_thread_id()`` 产生）。
        **extra: 追加到 ``configurable`` 的键值（如 ``checkpoint_ns``）。

    Returns:
        dict: ``{"configurable": {"thread_id": ...}, "recursion_limit": N}``
    """
    configurable: dict[str, Any] = {"thread_id": thread_id}
    configurable.update(extra)
    return {
        "configurable": configurable,
        _RECURSION_KEY: settings.AGENT_RECURSION_LIMIT,
    }


def llm_retry_policy() -> RetryPolicy:
    """调用 LLM 的节点统一使用的重试策略。

    只对 :class:`AgentRetryableError`（429 / 5xx / 网络异常 / JSON 解析失败）重试；
    4xx / 鉴权 / 参数错误抛的是 :class:`AgentNonRetryableError`，会**立即失败**。

    ⚠️ 语义换算（容易搞错，这里写死）：
    ``AGENT_NODE_MAX_RETRIES`` 是**重试次数**，而 LangGraph 的 ``RetryPolicy.max_attempts``
    是**总尝试次数（含首次）**。因此

        max_attempts = AGENT_NODE_MAX_RETRIES + 1

    默认配置下：1 次原始尝试 + 2 次重试 = **最多 3 次**。
    """
    return RetryPolicy(
        max_attempts=max(1, settings.AGENT_NODE_MAX_RETRIES + 1),
        initial_interval=0.5,
        backoff_factor=2.0,
        max_interval=8.0,
        jitter=True,
        retry_on=AgentRetryableError,
    )


def new_state_graph(
    name: str = "xhs-agent-graph",
    state_schema: type = BaseAgentState,
) -> StateGraph:
    """创建一个带统一状态结构的 StateGraph。

    Args:
        name: 图名（出现在 LangGraph 日志与（可选）LangSmith 中）。
        state_schema: 状态结构，默认 :class:`BaseAgentState`；业务图可继承扩展。
    """
    return StateGraph(state_schema, name=name)


def compile_graph(
    graph: StateGraph,
    *,
    checkpointer: Any | None = None,
    use_checkpointer: bool = True,
) -> Any:
    """编译图并绑定检查点。

    Args:
        graph: 已装配好节点与边的 StateGraph。
        checkpointer: 显式指定的检查点实现；``None`` 表示取全局单例。
        use_checkpointer: 置 False 可编译出**无检查点**的图（不需要 interrupt/resume
            的纯计算图可用，能少一次数据库往返）。

    Returns:
        CompiledStateGraph: 可直接 ``.invoke(state, config)``。
    """
    if not use_checkpointer:
        return graph.compile()
    if checkpointer is None:
        from app.agent.checkpoint import get_checkpointer

        checkpointer = get_checkpointer()
    return graph.compile(checkpointer=checkpointer)


__all__ = [
    "new_thread_id",
    "make_config",
    "llm_retry_policy",
    "new_state_graph",
    "compile_graph",
]
