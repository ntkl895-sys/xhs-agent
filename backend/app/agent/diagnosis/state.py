"""单篇诊断图状态：在统一基座状态上追加「诊断」业务字段。

设计要点（与 harness 基座、第 10 / 12 / 15 步图一致）：

- 只放**可 JSON 序列化**的小数据——状态会进检查点；
- **模型对象与 ORM 对象绝不入状态**：模型经闭包注入节点；笔记 / 快照等 ORM 行
  在 ``load_context`` 内转成**普通 dict** 后再放进状态；
- 覆盖语义字段（``raw`` / ``latest`` / ``rates`` 等）只承载**最后一轮**结果，
  避免回路重试时旧数据干扰；
- 本图的 ``persist`` **会落库**（写 ``diagnostics`` 一条），与第 15 步识别图
  （刻意不 persist）不同——诊断是「只读 + 建议 + 留存结论」，不改笔记数据。
"""

from __future__ import annotations

from typing import Any

from app.agent.state.base import BaseAgentState


class DiagnosisState(BaseAgentState, total=False):
    """「单篇笔记 → 一次诊断结论」的业务状态。"""

    # ---- 运行参数（service 经 extra_state 注入）----
    #: 目标笔记 id（必填；不存在 → DiagnosisContextError）
    note_id: str | None
    #: 用户手动指定的诊断类型（cold_start / review / anomaly；可空 = 自动判定）
    diagnosis_type: str | None

    # ---- load_context 产出（普通 dict / 标量，全部可序列化）----
    #: 笔记快照（title / body_excerpt / tags / status / published_at 等）
    note_snapshot: dict[str, Any]
    #: 人设快照（可空：笔记未关联人设）
    persona_snapshot: dict[str, Any] | None
    #: 是否已有选中封面（covers.is_selected）
    has_cover: bool
    #: 快照条数（时间线长度）
    snapshot_count: int
    #: 每个指标的最新非空值（六字段；缺失 = None）
    latest: dict[str, int | None]
    #: 最新一条快照 id（→ diagnostics.based_on_metric_id；无快照 = None）
    latest_snapshot_id: str | None
    #: 六条漏斗比率（缺失 = None）
    rates: dict[str, float | None]
    #: 环比 / 趋势 / 覆盖时长（本地确定性计算）
    trends: dict[str, Any]
    #: 自动 / 手动判定后的诊断类型（cold_start / review / anomaly）
    diagnosis_type_resolved: str
    #: 判定来源：auto / manual
    diagnosis_type_source: str
    #: 判定依据（人话说明，供展示与审计）
    diagnosis_type_reason: str
    #: 本地收集的数据缺口（只列事实）
    data_gaps: list[str]

    # ---- generate_diagnosis 产出（覆盖语义：只保留最后一轮）----
    #: 模型输出的原始 JSON（对象）
    raw: dict[str, Any]
    #: 模型原始文本（只保留最后成功一轮；仅供 service 回填 agent_runs.llm_raw）
    llm_raw: str | None
    #: 本轮是否 JSON 解析失败（触发有限回路 / give_up）
    parse_failed: bool

    # ---- validate_diagnosis 产出（覆盖语义：本轮结论）----
    #: 本轮校验是否通过（False → 回炉 / 超限放弃）
    validation_ok: bool
    #: 硬伤清单（本地无法修复的结构问题）
    field_errors: list[str]
    #: 下一轮回炉的纠正提示（追加进对话）
    feedback: str
    #: 已执行的生成轮数（给有限回路设上界；调用模型次数 = 轮数 × invoke_json 内部尝试）
    retry_count: int
    #: 归一后的评分（0~100 或 None；冷启动强制 None）
    score: float | None
    #: 归一后的四个文本数组（本地已做条数 / 长度截断）
    strengths: list[str]
    weaknesses: list[str]
    reasons: list[str]
    suggestions: list[str]
    #: 模型补充的数据缺口（合并前；persist 时与本地 data_gaps 去重合并）
    model_data_gaps: list[str]
    #: 宽容修正记录（截断 / 冷启动忽略评分等）
    warnings: list[str]

    # ---- persist 产出 ----
    #: 落库后的 diagnostics 行 id
    diagnostic_id: str


__all__ = ["DiagnosisState"]
