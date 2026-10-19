"""周报图状态：在统一基座状态上追加「周度复盘」业务字段（第 17 步）。

设计要点（与 harness 基座、第 10 / 12 / 15 / 16 步图一致）：

- 只放**可 JSON 序列化**的小数据——状态会进检查点；
- **模型对象与 ORM 对象绝不入状态**：模型经闭包注入节点；周期事实
  （笔记 / 快照 / 诊断摘要等）在 ``load_context`` 内由
  :func:`app.agent.weekly.contracts.build_period_facts` 转成**普通 dict** 后进状态；
- 覆盖语义字段（``raw`` / ``report`` 等）只承载**最后一轮**结果，
  避免回路重试时旧数据干扰；
- 本图的 ``persist_run`` **不写任何业务表**：把归一后的周报组装进
  ``state["output"]``，由 service 用 ``run_id`` 回填 ``agent_runs``——
  「周报结论存 agent_runs.outputs」是本步既定口径。
"""

from __future__ import annotations

from typing import Any

from app.agent.state.base import BaseAgentState


class WeeklyState(BaseAgentState, total=False):
    """「运营周期 → 一次周度复盘」的业务状态。"""

    # ---- 运行参数（service 经 extra_state 注入）----
    #: 周期起点（ISO 字符串；service 已校验，load_context 兜底再校验）
    period_start: str | None
    #: 周期终点（ISO 字符串）
    period_end: str | None
    #: 便捷参数：等价于「近 N 天」（与 period_start/period_end 互斥）
    days: int | None

    # ---- load_context 产出 ----
    #: 周期事实（全部本地计算好的普通 dict；结构见 contracts.build_period_facts）
    period_facts: dict[str, Any]

    # ---- generate_report 产出（覆盖语义：只保留最后一轮）----
    #: 模型输出的原始 JSON（对象）
    raw: dict[str, Any]
    #: 模型原始文本（只保留最后成功一轮；仅供 service 回填 agent_runs.llm_raw）
    llm_raw: str | None
    #: 本轮是否 JSON 解析失败（触发有限回路 / give_up）
    parse_failed: bool

    # ---- validate_report 产出（覆盖语义：本轮结论）----
    #: 本轮校验是否通过（False → 回炉 / 超限放弃）
    validation_ok: bool
    #: 硬伤清单（本地无法修复的结构问题，如 next_week_suggestions 为空）
    field_errors: list[str]
    #: 下一轮回炉的纠正提示
    feedback: str
    #: 已执行的生成轮数（给有限回路设上界）
    retry_count: int
    #: 归一后的周报（sections / 周期 / confidence；data_gaps 在 persist 时合并）
    report: dict[str, Any]
    #: 模型补充的数据缺口（合并前）
    model_data_gaps: list[str]
    #: 宽容修正记录（截断 / 条目裁剪等）
    warnings: list[str]


__all__ = ["WeeklyState"]
