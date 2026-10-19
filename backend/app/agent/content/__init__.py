"""内容创作 Agent（第 12 步）—— 在 LangGraph harness 上实现的第二个业务图。

对外入口：

- :func:`app.agent.content.service.create_note_for_topic` —— service 层单题编排入口；
- :func:`app.agent.content.graph.build_content_graph` —— 单题业务图（多节点 + 条件边 + 有限回路）；
- :class:`app.agent.content.state.ContentState` —— 业务状态；
- :data:`app.agent.content.prompts.CONTENT_PROMPT_VERSION` —— 提示词版本；
- :mod:`app.agent.content.forbidden_words` —— 本地确定性违禁词合规自检。

链路：``load_context → generate_content → validate_compliance →（条件边）→ persist / 回炉重写 / give_up``。
"""

from __future__ import annotations

from app.agent.content.contracts import ContentDraft
from app.agent.content.forbidden_words import (
    build_content_check,
    compliance_of,
    describe_wordlist,
    scan_note,
    scan_text,
)
from app.agent.content.graph import build_content_graph
from app.agent.content.prompts import CONTENT_PROMPT_VERSION
from app.agent.content.service import create_note_for_topic
from app.agent.content.state import ContentState

__all__ = [
    "CONTENT_PROMPT_VERSION",
    "ContentDraft",
    "ContentState",
    "build_content_check",
    "build_content_graph",
    "compliance_of",
    "create_note_for_topic",
    "describe_wordlist",
    "scan_note",
    "scan_text",
]
