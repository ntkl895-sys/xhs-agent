"""周报 Agent（第 17 步）—— 在 LangGraph harness 上实现的**第五个业务图**。

对外入口：

- :func:`app.agent.weekly.service.generate_weekly_report` —— service 层编排入口
  （运营周期 → 一次周度复盘；**不落业务表**，结论写入 ``agent_runs.outputs``）；
- :func:`app.agent.weekly.service.list_weekly_reports` /
  :func:`app.agent.weekly.service.get_weekly_report` —— 历史查询（agent_runs 过滤）；
- :func:`app.agent.weekly.graph.build_weekly_report_graph` —— 单图业务图
  （五节点 + 条件边 + 有界回路 + 检查点）；
- :mod:`app.agent.weekly.contracts` —— 确定性计算
  （周期解析 / 周期事实构建 / 报告归一 / 专属异常）；
- :class:`app.agent.weekly.state.WeeklyState` —— 业务状态；
- :data:`app.agent.weekly.prompts.WEEKLY_PROMPT_VERSION` —— 提示词版本。

链路：``load_context → generate_report → validate_report →（条件边）
persist_run / 回炉重写 / give_up``。

红线：**只读 + 建议**——零业务表写入；**严禁外部行业数字**（行业均值 /
爆款门槛一律禁止，跨篇基准属第 18 步 RAG）；数据不足在 ``data_gaps`` 如实声明。
"""

from __future__ import annotations

from app.agent.weekly.contracts import (
    DIAG_SUMMARY_LIMIT,
    MAX_PERIOD_DAYS,
    NEW_NOTES_LIMIT,
    REPORT_MAX_LIST_ITEMS,
    REPORT_SUMMARY_MAX_LEN,
    REPORT_TEXT_MAX_LEN,
    WeeklyContextError,
    WeeklyGiveUpError,
    WeeklyReportError,
    build_period_facts,
    merge_data_gaps,
    normalize_report_payload,
    period_delta,
    resolve_period,
)
from app.agent.weekly.graph import (
    NODE_GENERATE,
    NODE_GIVE_UP,
    NODE_LOAD_CONTEXT,
    NODE_PERSIST,
    NODE_VALIDATE,
    build_weekly_report_graph,
)
from app.agent.weekly.prompts import (
    WEEKLY_PROMPT_VERSION,
    build_messages,
    build_user_prompt,
)
from app.agent.weekly.service import (
    generate_weekly_report,
    get_weekly_report,
    list_weekly_reports,
)
from app.agent.weekly.state import WeeklyState

__all__ = [
    "DIAG_SUMMARY_LIMIT",
    "MAX_PERIOD_DAYS",
    "NEW_NOTES_LIMIT",
    "NODE_GENERATE",
    "NODE_GIVE_UP",
    "NODE_LOAD_CONTEXT",
    "NODE_PERSIST",
    "NODE_VALIDATE",
    "REPORT_MAX_LIST_ITEMS",
    "REPORT_SUMMARY_MAX_LEN",
    "REPORT_TEXT_MAX_LEN",
    "WEEKLY_PROMPT_VERSION",
    "WeeklyContextError",
    "WeeklyGiveUpError",
    "WeeklyReportError",
    "WeeklyState",
    "build_messages",
    "build_period_facts",
    "build_weekly_report_graph",
    "generate_weekly_report",
    "get_weekly_report",
    "list_weekly_reports",
    "merge_data_gaps",
    "normalize_report_payload",
    "period_delta",
    "resolve_period",
]
