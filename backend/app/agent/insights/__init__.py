"""爆款规律提炼（第六个业务图，第 18 步）。

对外入口：

- :func:`~app.agent.insights.service.generate_insights` —— 跑一次提炼并把业务
  明细回填 ``agent_runs``（``run_type=insights``）；
- :func:`~app.agent.insights.graph.build_insights_graph` —— 构图工厂
  （六节点真 LangGraph；模型与 embedder 闭包注入）；
- 契约（取数事实 / 归一 / 异常）见 :mod:`app.agent.insights.contracts`。

红线：规律只来自本账号本地数据（不爬竞品、不引入外部数字）；样本不足不硬造
规律（run 记 partial）；只写 ``knowledge_snippets``。
"""

from app.agent.insights.contracts import (
    InsightsContextError,
    InsightsError,
    InsightsGiveUpError,
    build_insight_facts,
    normalize_patterns,
)
from app.agent.insights.graph import build_insights_graph
from app.agent.insights.prompts import INSIGHTS_PROMPT_VERSION
from app.agent.insights.service import generate_insights
from app.agent.insights.state import InsightsState

__all__ = [
    "INSIGHTS_PROMPT_VERSION",
    "InsightsContextError",
    "InsightsError",
    "InsightsGiveUpError",
    "InsightsState",
    "build_insight_facts",
    "build_insights_graph",
    "generate_insights",
    "normalize_patterns",
]
