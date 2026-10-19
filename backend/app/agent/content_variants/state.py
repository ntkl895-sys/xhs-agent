"""多版本创作图状态：在统一基座状态上追加「同选题 N 版」业务字段。

设计要点（与 harness 基座、第 12 步创作图一致）：

- **只放可 JSON 序列化的数据** —— 状态会进检查点；模型对象经**闭包**注入节点，绝不入状态；
- 覆盖语义字段（``variant_drafts`` / ``variant_results`` / ``variant_feedback`` /
  ``retry_count``）只承载**本轮**结果，避免回环重生成时旧数据干扰；
- N 条 notes 的最终落库以 ``persist_variants`` 节点的单事务完成，状态里只回传 id。
"""

from __future__ import annotations

from typing import Any

from app.agent.state.base import BaseAgentState


class ContentVariantsState(BaseAgentState, total=False):
    """「单个已通过选题 → N 个可对比版本（同批次 notes）」的业务状态。"""

    # 基座已有 run_id / thread_id / kind / account_id / persona_id / input / output /
    # usage / parse_attempts / node_trace 等；这里显式重申业务近义字段便于阅读。
    account_id: str | None
    persona_id: str | None

    # ---- 运行参数（service 经 extra_state 注入）----
    topic_id: str
    #: 请求生成的版本数（1~5）
    variant_n: int
    #: 显式指定的角度列表（None / 空 = 模型从角度池自选且必须互不重复）
    variant_angles: list[str] | None

    # ---- load_context 填充的快照（与第 12 步同构；纯 dict）----
    topic_snapshot: dict[str, Any]
    persona_snapshot: dict[str, Any]
    account_snapshot: dict[str, Any]
    #: 第 18 步 · RAG 反哺：本账号历史经验提示词块（空串 / 缺省 = 无 RAG）。
    rag_context: str | None

    # ---- generate_variants 产出（覆盖语义：只保留本轮解析结果）----
    #: 归一后的版本草稿 ``[{angle, title, body, tags, type_errors}]``
    variant_drafts: list[dict[str, Any]]
    #: 模型返回数量超出 N 时被忽略的条数（防御性计数）
    variant_extra_ignored: int
    #: 模型原始输出（只保留最后成功一轮；仅供 service 回填 agent_runs.llm_raw）
    llm_raw: str | None

    # ---- review_variants 产出（覆盖语义：本轮结论）----
    #: 逐版判定 ``[{index, angle, title, body, tags, outcome, reason, hits}]``
    variant_results: list[dict[str, Any]]
    #: 下一轮重生成反馈（仅在存在 rejected 版本时非空）
    variant_feedback: str
    #: 已执行的判定轮数（给有限回路设上界）
    retry_count: int

    # ---- persist_variants 产出 ----
    note_ids: list[str]


__all__ = ["ContentVariantsState"]
