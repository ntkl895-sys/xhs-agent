"""规律提炼图状态：在统一基座状态上追加「爆款规律」业务字段（第 18 步）。

设计要点（与 harness 基座、第 10 / 12 / 16 / 17 步图一致）：

- 只放**可 JSON 序列化**的小数据 —— 状态会进检查点；
- **模型对象与 embedder 绝不入状态**：均经闭包注入节点；取数事实
  （笔记 / 快照 / 诊断摘要）在 ``load_context`` 内由
  :func:`app.agent.insights.contracts.build_insight_facts` 转成普通 dict 后进状态；
- **向量绝不入状态**：embedding 只在 ``embed_and_persist`` 节点内用于落库；
- 覆盖语义字段（``raw`` / ``patterns`` 等）只承载**最后一轮**结果，
  避免回路重试时旧数据干扰。
"""

from __future__ import annotations

from typing import Any

from app.agent.state.base import BaseAgentState


class InsightsState(BaseAgentState, total=False):
    """「本账号已发布笔记 → 可复用爆款规律」的业务状态。"""

    # ---- load_context 产出 ----
    #: 提炼事实（全部本地计算好的普通 dict；结构见 contracts.build_insight_facts）
    facts: dict[str, Any]
    #: 样本是否达到门槛（False → 条件边直接走 insufficient_sample，零模型调用）
    sample_sufficient: bool

    # ---- generate_patterns 产出（覆盖语义：只保留最后一轮）----
    #: 模型输出的原始 JSON（对象）
    raw: dict[str, Any]
    #: 模型原始文本（只保留最后成功一轮；仅供 service 回填 agent_runs.llm_raw）
    llm_raw: str | None
    #: 本轮是否 JSON 解析失败（触发有限回路 / give_up）
    parse_failed: bool
    #: 已执行的生成轮数（给有限回路设上界）
    retry_count: int

    # ---- validate_patterns 产出（覆盖语义：本轮结论）----
    #: 本轮校验是否通过（False → 回炉 / 超限放弃）
    validation_ok: bool
    #: 归一后的合格规律（固定六键；vector 不在其中）
    patterns: list[dict[str, Any]]
    #: 被剔除的条目及原因（用于 partial 判定与审计）
    rejected_patterns: list[dict[str, Any]]
    #: 硬伤清单（本地无法修复的结构问题）
    field_errors: list[str]
    #: 下一轮回炉的纠正提示
    feedback: str
    #: 宽容修正记录（截断 / 置信度置空等）
    warnings: list[str]

    # ---- embed_and_persist 产出 ----
    #: 落库统计 ``{generated, persisted, reused, skipped, snippet_ids}``
    persist_result: dict[str, Any]


__all__ = ["InsightsState"]
