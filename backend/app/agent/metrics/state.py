"""截图识别图状态：在统一基座状态上追加「数据回填」业务字段。

设计要点（与 harness 基座、第 10 / 12 步图一致）：

- 只放**可 JSON 序列化**的数据——状态会进检查点；
- **图片字节与视觉模型都经闭包注入节点，绝不入状态**：图片是二进制大对象，
  放进状态会污染检查点（每一步都会把整张图写进 ``langgraph`` schema）；
- 覆盖语义字段（``raw`` / ``metrics`` / ``evidence`` 等）只承载**最后一轮**结果，
  避免回路重试时旧数据干扰；
- 图**不做 persist**：``output`` 只是候选，等待前端人工确认后走 ``POST /metrics`` 落库。
"""

from __future__ import annotations

from typing import Any

from app.agent.state.base import BaseAgentState


class MetricState(BaseAgentState, total=False):
    """「一张数据截图 → 六个指标候选」的业务状态。"""

    # ---- 运行参数（service 经 extra_state 注入）----
    #: 目标笔记 id（可空；不为空时 load_image 会校验存在并载入标题供人工核对）
    note_id: str | None

    # ---- load_image 产出 ----
    #: 目标笔记标题（人工核对用；笔记不存在则整次运行失败）
    note_title: str | None
    #: 图片元信息（文件名 / 探测类型 / 字节数 / sha256；**不含图片数据与绝对路径**）
    image_meta: dict[str, Any]

    # ---- transcribe 产出（覆盖语义：只保留最后一轮）----
    #: 模型输出的原始 JSON（对象）
    raw: dict[str, Any]
    #: 模型原始文本（只保留最后成功一轮；仅供 service 回填 agent_runs.llm_raw）
    llm_raw: str | None
    #: 本轮是否 JSON 解析失败（触发有限回路 / give_up）
    parse_failed: bool
    #: 是否探测到「模型不支持图片」（触发 give_up，由 service 转友好错误）
    vision_unsupported: bool
    #: 「不支持图片」的接口报错摘要（截断；只进审计细节，不进用户文案正文）
    vision_error: str | None

    # ---- normalize_validate 产出（覆盖语义：本轮结论）----
    #: 归一后的六指标（缺失一律 None，**绝不臆造**）
    metrics: dict[str, int | None]
    #: 每字段的来源 / 置信度 / missing / uncertain 标记
    evidence: dict[str, Any]
    #: 缺失 / 不确定字段清单（标准字段名，六字段口径）
    missing_fields: list[str]
    uncertain_fields: list[str]
    #: 模型 warnings + 本地 warnings
    warnings: list[str]
    #: 识别时刻（服务端 UTC，ISO 串；人工可改后再落库）
    captured_at: str

    # ---- 回路控制 ----
    #: 已执行的 transcribe 轮数（给有限回路设上界）
    retry_count: int
    #: 下一轮重试的纠正提示（追加进对话）
    feedback: str


__all__ = ["MetricState"]
