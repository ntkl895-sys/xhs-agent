"""单篇诊断 Agent（第 16 步）—— 在 LangGraph harness 上实现的**第四个业务图**。

对外入口：

- :func:`app.agent.diagnosis.service.diagnose_note` —— service 层编排入口
  （单篇笔记 → 一次诊断结论，**落库 diagnostics 一条**；只读 + 建议）；
- :func:`app.agent.diagnosis.graph.build_diagnosis_graph` —— 单图业务图
  （五节点 + 条件边 + 有界回路 + 检查点）；
- :mod:`app.agent.diagnosis.contracts` —— 确定性计算纯函数
  （最新值 / 六率 / 环比 / 覆盖时长 / 类型判定 / data_gaps / 输出归一）与专属异常；
- :class:`app.agent.diagnosis.state.DiagnosisState` —— 业务状态；
- :data:`app.agent.diagnosis.prompts.DIAG_PROMPT_VERSION` —— 提示词版本。

链路：``load_context → generate_diagnosis → validate_diagnosis →（条件边）
persist / 回炉重写 / give_up``。

红线：**不引入任何外部基准数字**（``benchmark.external_benchmark`` 恒为 null，
跨篇基准属第 18 步 RAG）；数据不足如实声明（``data_gaps``），严重不足时
``score`` 为 ``null``；诊断绝不修改 notes / topics / note_metrics。
"""

from __future__ import annotations

from app.agent.diagnosis.contracts import (
    BENCHMARK_NOTE,
    DATA_GAPS_LIMIT,
    METRIC_FIELDS,
    RATE_FIELDS,
    RATE_PRECISION,
    DiagnosisContextError,
    DiagnosisError,
    DiagnosisGiveUpError,
    collect_data_gaps,
    compute_rates,
    compute_trends,
    decide_diagnosis_type,
    merge_data_gaps,
    metric_latest,
    normalize_diagnosis_payload,
)
from app.agent.diagnosis.graph import (
    NODE_GENERATE,
    NODE_GIVE_UP,
    NODE_LOAD_CONTEXT,
    NODE_PERSIST,
    NODE_VALIDATE,
    build_diagnosis_graph,
)
from app.agent.diagnosis.prompts import DIAG_PROMPT_VERSION, build_messages, build_user_prompt
from app.agent.diagnosis.service import diagnose_note
from app.agent.diagnosis.state import DiagnosisState

__all__ = [
    "BENCHMARK_NOTE",
    "DATA_GAPS_LIMIT",
    "DIAG_PROMPT_VERSION",
    "METRIC_FIELDS",
    "NODE_GENERATE",
    "NODE_GIVE_UP",
    "NODE_LOAD_CONTEXT",
    "NODE_PERSIST",
    "NODE_VALIDATE",
    "RATE_FIELDS",
    "RATE_PRECISION",
    "DiagnosisContextError",
    "DiagnosisError",
    "DiagnosisGiveUpError",
    "DiagnosisState",
    "build_diagnosis_graph",
    "build_messages",
    "build_user_prompt",
    "collect_data_gaps",
    "compute_rates",
    "compute_trends",
    "decide_diagnosis_type",
    "diagnose_note",
    "merge_data_gaps",
    "metric_latest",
    "normalize_diagnosis_payload",
]
