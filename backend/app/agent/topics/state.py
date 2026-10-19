"""选题图状态：在统一基座状态上追加选题业务字段。

设计要点（与 harness 基座一致）：

- **只放可 JSON 序列化的数据** —— 状态会进检查点；模型对象经**闭包**注入节点，绝不入状态；
- 需要**追加**语义的列表字段用 ``Annotated[list, operator.add]``（``validated`` / ``rejected`` /
  ``topic_ids``）；其余为覆盖语义；
- ``candidates`` 刻意用**覆盖**语义 —— 它只承载「本轮模型返回的原始候选」，
  若用追加，回退重生成时会把上一轮的候选再校验一遍，产生重复拒绝。
- 运行参数（``per_hotspot`` / ``hotspot_id``）由 service 经 ``extra_state`` 注入；
  ``persona_snapshot`` / ``hotspot_snapshot`` 由 ``load_context`` 节点填充（纯 dict）。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any

from langgraph.graph.message import add_messages

from app.agent.state.base import BaseAgentState


class TopicState(BaseAgentState, total=False):
    """单热点 → K 个选题 的业务状态。"""

    # 基座已有 account_id / persona_id / input / output / usage / parse_attempts /
    # node_trace 等；这里显式重申两个业务近义字段，便于阅读与自检断言。
    account_id: str | None
    persona_id: str | None

    # ---- 运行参数（service 注入）----
    hotspot_id: str
    per_hotspot: int

    # ---- load_context 填充的快照（纯 dict，可 JSON 序列化）----
    persona_snapshot: dict[str, Any]
    hotspot_snapshot: dict[str, Any]
    #: 第 18 步 · RAG 反哺：本账号历史经验提示词块（空串 / 缺省 = 无 RAG）。
    #: 只是文本片段 —— 检索失败时降级为空，绝不影响本图其余逻辑。
    rag_context: str | None

    # ---- generate_topics 产出（覆盖语义：只保留本轮原始候选）----
    candidates: list[dict[str, Any]]

    # ---- validate_dedupe 产出（追加语义）----
    #: 合格候选（跨轮累积、去重）
    validated: Annotated[list[dict[str, Any]], operator.add]
    #: 不合格 / 重复候选（跨轮累积），元素形如 {"title": ..., "reason": ..., "kind": ...}
    rejected: Annotated[list[dict[str, Any]], operator.add]
    #: 本轮不合格原因摘要，作为下一轮重生成的反馈（覆盖语义）
    feedback: str
    #: 已执行的校验轮数（用于给回路设上界，防止模型持续少给导致死循环）
    retry_count: int

    # ---- persist 产出（追加语义）----
    topic_ids: Annotated[list[str], operator.add]

    # ---- 模型原始输出（覆盖语义：只保留最后成功一轮的原始文本）----
    llm_raw: str | None

    # ---- 可选：工具调用（TOPIC_TOOLCALLING_ENABLED=false 时全部不写入）----
    #: 工具决策循环的消息通道（AIMessage / ToolMessage）。由 LangGraph 的
    #: ``add_messages`` 追加；检查点由框架的序列化器处理（非纯 JSON，但可持久化）。
    messages: Annotated[list[Any], add_messages]
    #: 已执行的**工具轮数**（一轮 = 一批 tool_calls），用于硬上限收敛。
    tool_rounds: int
    #: 逐次工具调用事件：{tool, args, ok, result_summary, duration_ms, error}
    #: 追加语义；进 node_trace 与 agent_runs.outputs 供回放。
    tool_events: Annotated[list[dict[str, Any]], operator.add]
    #: 工具结果渲染成的提示词块（仅工具模式；空 = 不追加任何文本）。
    tool_context: str | None
    #: 模型请求过、但不在白名单内的工具名（幻觉工具，追加语义）。
    hallucinated_tools: Annotated[list[str], operator.add]


__all__ = ["TopicState"]
