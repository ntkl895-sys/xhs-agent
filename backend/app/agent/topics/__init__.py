"""选题 Agent（第 10 步）——在 LangGraph harness 上实现的第一个业务图。

对外入口：

- :func:`app.agent.topics.service.generate_for_hotspots` —— service 层批量编排入口；
- :func:`app.agent.topics.graph.build_topic_graph` —— 单热点业务图（多节点 + 条件边 + 有限回路）；
- :class:`app.agent.topics.state.TopicState` —— 业务状态；
- :data:`app.agent.topics.prompts.TOPIC_PROMPT_VERSION` —— 提示词版本。

链路：``load_context → generate_topics → validate_dedupe →（条件边）→ persist / 回退重生成 / give_up``。
"""

from __future__ import annotations

from app.agent.topics.contracts import TopicCandidate
from app.agent.topics.graph import build_topic_graph
from app.agent.topics.prompts import TOPIC_PROMPT_VERSION
from app.agent.topics.service import generate_for_hotspots
from app.agent.topics.state import TopicState

__all__ = [
    "TopicCandidate",
    "TopicState",
    "TOPIC_PROMPT_VERSION",
    "build_topic_graph",
    "generate_for_hotspots",
]
