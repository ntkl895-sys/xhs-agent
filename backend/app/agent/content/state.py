"""创作图状态：在统一基座状态上追加「正文创作」业务字段。

设计要点（与 harness 基座、第 10 步选题图一致）：

- **只放可 JSON 序列化的数据** —— 状态会进检查点；模型对象经**闭包**注入节点，绝不入状态；
- 覆盖语义字段（``draft`` / ``field_errors`` / ``forbidden_hits`` / ``feedback`` / ``retry_count``）
  只承载**本轮**结果，避免回环重生成时旧数据干扰；
- ``notes`` 的最终落库以 ``persist`` 节点的单事务完成，状态里只回传 id。
"""

from __future__ import annotations

from typing import Any

from app.agent.state.base import BaseAgentState


class ContentState(BaseAgentState, total=False):
    """「单个已通过选题 → 一篇笔记草稿」的业务状态。"""

    # 基座已有 run_id / thread_id / kind / account_id / persona_id / input / output /
    # usage / parse_attempts / node_trace 等；这里显式重申业务近义字段便于阅读。
    account_id: str | None
    persona_id: str | None

    # ---- 运行参数（service 经 extra_state 注入）----
    topic_id: str

    # ---- load_context 填充的快照（纯 dict）----
    topic_snapshot: dict[str, Any]
    persona_snapshot: dict[str, Any]
    account_snapshot: dict[str, Any]
    #: 第 18 步 · RAG 反哺：本账号历史经验提示词块（空串 / 缺省 = 无 RAG）。
    #: 只是文本片段 —— 检索失败时降级为空，绝不影响本图其余逻辑。
    rag_context: str | None

    # ---- generate_content 产出（覆盖语义：只保留本轮草稿）----
    #: 模型草稿 ``{title, body, tags}``（已做基础归一，未做合规判定）
    draft: dict[str, Any]
    #: 模型原始输出（只保留最后成功一轮；仅供 service 回填 agent_runs.llm_raw）
    llm_raw: str | None

    # ---- validate_compliance 产出（覆盖语义：本轮结论）----
    #: 字段硬伤列表（标题 / 正文 / 标签不达阈值时的可读原因）
    field_errors: list[str]
    #: 本轮违禁词命中列表 ``[{word, category, field, snippet}]``
    forbidden_hits: list[dict[str, Any]]
    #: 落 notes.content_check 的结构
    content_check: dict[str, Any]
    #: passed / warning（0 命中 = passed）
    compliance_status: str
    #: 下一轮重生成的反馈（字段问题 + 违禁词命中）
    feedback: str
    #: 已执行的校验轮数（给回路设上界）
    retry_count: int

    # ---- persist 产出 ----
    note_id: str


__all__ = ["ContentState"]
