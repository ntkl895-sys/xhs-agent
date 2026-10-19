"""Agent Harness 基座（LangChain / LangGraph）。

本包是**所有 LLM 智能流程的统一运行底座**：

- 只编排「需要大模型」的流程（选题 / 创作 / 诊断 / RAG）；
- 不承载「无 LLM 的定时数据采集」——那是 APScheduler 的职责；
- 不承载「人工审核 + 手动发布」——那是业务表状态流转 + 网页操作。

对外入口（后续步骤在此基础上编写具体业务图）：

- :mod:`app.agent.llm.factory`        —— 统一模型工厂（OpenAI 兼容）
- :mod:`app.agent.parsing.json_repair`—— 稳健 JSON 解析 + 结构化输出重试
- :mod:`app.agent.state.base`         —— 统一状态结构 BaseAgentState
- :mod:`app.agent.checkpoint`         —— Postgres 检查点生命周期（框架托管表）
- :mod:`app.agent.graph.base`         —— 构图 / 编译 / 配置辅助
- :mod:`app.agent.tracing.recorder`   —— 运行审计（写 agent_runs）
- :mod:`app.agent.runner`             —— 同步统一入口 run_agent()
"""

from __future__ import annotations

from app.agent.errors import (
    AgentConfigError,
    AgentError,
    AgentJSONError,
    AgentNonRetryableError,
    AgentRetryableError,
)

__all__ = [
    "AgentError",
    "AgentConfigError",
    "AgentRetryableError",
    "AgentNonRetryableError",
    "AgentJSONError",
]
