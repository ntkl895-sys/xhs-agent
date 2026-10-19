"""diagnostics 的 Pydantic schema（第 16 步 · 单篇诊断）。

三个模型：

- ``DiagnosticGenerateRequest``：``POST /api/v1/diagnostics/generate`` 的请求体
  （``note_id`` 必填；``diagnosis_type`` 可选三值，缺省由服务端自动判定）；
- ``DiagnosticOut``：单条诊断结论（**逐列对齐** ``diagnostics`` 表；
  ``llm_meta`` 走白名单投影，响应**绝不含** ``llm_raw``）；
- ``DiagnosticListOut``：某笔记的诊断历史（``GET /api/v1/diagnostics?note_id=``）。

红线（自检会断言）：任何响应都不含 ``llm_raw`` / API key / base64 / 绝对路径；
``benchmark`` 里不得出现外部均值杜撰（``external_benchmark`` 恒为 null，
结构由诊断图固定生成）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import DiagnosisType

#: ``llm_meta`` 白名单键（严格五项；顺序即投影顺序）
_LLM_META_KEYS: tuple[str, ...] = (
    "prompt_version",
    "json_parse_attempts",
    "diagnosis_type",
    "diagnosis_type_source",
    "data_gaps",
)


class DiagnosticGenerateRequest(BaseModel):
    """诊断生成请求体。

    - ``note_id`` 必填（不存在 → 404；不是合法 UUID → 422）；
    - ``diagnosis_type`` 可选：``cold_start``（冷启动）/ ``review``（复盘）/
      ``anomaly``（异常）；**不传即自动判定**（判定来源在响应里以
      ``llm_meta.diagnosis_type_source`` 标注 auto / manual）。
    """

    model_config = ConfigDict(extra="ignore")

    note_id: uuid.UUID = Field(description="目标笔记 id（必填）")
    diagnosis_type: Literal["cold_start", "review", "anomaly"] | None = Field(
        default=None, description="手动诊断类型；不传则自动判定"
    )


class DiagnosticBenchmark(BaseModel):
    """``benchmark`` 列的固定结构（**只装该笔记自身相对指标 + 基准缺失声明**）。

    本期没有任何外部基准（行业均值 / 爆款阈值一律禁止，跨篇基准属第 18 步 RAG）：
    ``external_benchmark`` 恒为 ``null``，``benchmark_note`` 说明该口径。
    各字段都给默认值：即使遇到历史脏行也不让详情报错。
    """

    model_config = ConfigDict(extra="ignore")

    basis: str | None = Field(default=None, description="基准口径：本期恒为 self_only")
    latest_metrics: dict[str, Any] = Field(default_factory=dict, description="最新六指标")
    rates: dict[str, Any] = Field(default_factory=dict, description="六条互动率")
    trends: dict[str, Any] = Field(default_factory=dict, description="环比 / 总增量 / 增长率 / 覆盖时长")
    thresholds: dict[str, Any] = Field(default_factory=dict, description="本地判定阈值（异常降幅比例）")
    external_benchmark: Any | None = Field(
        default=None, description="外部基准：本期恒为 null（绝不杜撰）"
    )
    benchmark_note: str | None = Field(default=None, description="基准缺失声明")


class DiagnosticMeta(BaseModel):
    """``llm_meta`` 白名单投影（严格五项；绝不回传 ``llm_raw`` / 提示词全文 / key）。"""

    model_config = ConfigDict(extra="ignore")

    prompt_version: str | None = None
    json_parse_attempts: int | None = None
    diagnosis_type: str | None = None
    diagnosis_type_source: str | None = Field(
        default=None, description="判定来源：auto（自动）/ manual（用户显式指定）"
    )
    data_gaps: list[str] = Field(default_factory=list)


class DiagnosticOut(BaseModel):
    """单条诊断结论输出（逐列对齐 ``diagnostics`` 表；无 ``llm_raw``）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    note_id: uuid.UUID
    based_on_metric_id: uuid.UUID | None = Field(
        default=None, description="所依据的数据快照 id（无快照时为 null）"
    )
    diagnosis_type: DiagnosisType | None = None
    score: float | None = Field(default=None, description="0~100；数据严重不足时为 null（不硬打分）")
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    benchmark: DiagnosticBenchmark | None = None
    llm_model: str | None = Field(default=None, description="实际模型名（不含 key）")
    llm_meta: DiagnosticMeta | None = None
    created_at: datetime

    @field_validator("strengths", "weaknesses", "reasons", "suggestions", mode="before")
    @classmethod
    def _none_to_empty_list(cls, value: Any) -> Any:
        """防御：库中该列为 NULL 时归一为空数组（前端按数组渲染）。"""
        return [] if value is None else value

    @field_validator("llm_meta", mode="before")
    @classmethod
    def _whitelist_llm_meta(cls, value: Any) -> Any:
        """白名单投影：只保留五个允许键（即使库里混入其它键也不外泄）。"""
        if isinstance(value, dict):
            return {key: value[key] for key in _LLM_META_KEYS if key in value}
        return value


class DiagnosticListOut(BaseModel):
    """``GET /api/v1/diagnostics`` 响应：某笔记的诊断历史（``created_at`` 倒序）。"""

    note_id: uuid.UUID
    items: list[DiagnosticOut]


__all__ = [
    "DiagnosticGenerateRequest",
    "DiagnosticBenchmark",
    "DiagnosticMeta",
    "DiagnosticOut",
    "DiagnosticListOut",
]
