"""周报 schema（第 17 步 · 运营看板 + 周报）。

四个模型：

- ``WeeklyReportGenerateRequest``：``POST /api/v1/reports/weekly/generate`` 请求体
  （三个参数全可选：显式起止 / 近 N 天）；
- ``WeeklyReportSections``：报告本体（归一后的固定结构；``data_gaps`` 已在
  persist 阶段与本地缺口合并去重）；
- ``WeeklyReportOut``：POST 生成响应（周报摘要 + report；**无 llm_raw / key**）；
- ``WeeklyReportDetailOut`` / ``WeeklyReportListItem`` / ``WeeklyReportListOut``：
  ``GET /weekly/{run_id}`` 详情与 ``GET /weekly`` 历史（从 ``agent_runs`` 组装）。

红线（自检会断言）：任何响应都不含 ``llm_raw`` / API key / base64 / 绝对路径；
周报不编造外部行业数字（提示词约束 + 黑名单扫描在自检/检查脚本）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WeeklyReportGenerateRequest(BaseModel):
    """周报生成请求体（三参数全可选）。

    - 不传 → 默认周期（近 ``WEEKLY_DEFAULT_DAYS`` 天，含今天）；
    - ``days``：近 N 天（与显式起止**互斥**，冲突 → 422）；
    - ``period_start`` / ``period_end``：``YYYY-MM-DD`` 或 ISO 8601 时间，
      可只给一端（另一端按默认规则补齐）；跨度 1~90 天。
    """

    model_config = ConfigDict(extra="ignore")

    period_start: str | None = Field(
        default=None, description="周期起点（YYYY-MM-DD 或 ISO 8601；可缺省）"
    )
    period_end: str | None = Field(
        default=None, description="周期终点（YYYY-MM-DD 或 ISO 8601；可缺省）"
    )
    days: int | None = Field(
        default=None, ge=1, le=90, description="近 N 天（与显式起止互斥）"
    )


class WeeklyReportSections(BaseModel):
    """报告本体（归一后的固定结构；逐字段对齐 persist 的 report）。"""

    model_config = ConfigDict(extra="ignore")

    period_start: str | None = None
    period_end: str | None = None
    summary: str = Field(default="", description="周期整体概述")
    highlights: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    data_evidence: list[str] = Field(
        default_factory=list, description="关键结论的数据依据（引用给定事实数字）"
    )
    next_week_suggestions: list[str] = Field(
        default_factory=list, description="下周可执行动作（按重要性排序）"
    )
    data_gaps: list[str] = Field(default_factory=list, description="数据缺口声明")
    confidence: float | None = Field(default=None, description="0~1；无法评估时为 null")


class WeeklyReportOut(BaseModel):
    """``POST /api/v1/reports/weekly/generate`` 响应（同步生成；落 agent_runs）。"""

    model_config = ConfigDict(extra="ignore")

    run_id: uuid.UUID = Field(description="本次运行 id（可在 GET /weekly/{run_id} 复查）")
    period_start: str | None = None
    period_end: str | None = None
    report: WeeklyReportSections | None = None
    parse_attempts: int | None = Field(default=None, description="JSON 解析尝试（理想为 1）")
    data_gaps: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class WeeklyReportDetailOut(BaseModel):
    """``GET /api/v1/reports/weekly/{run_id}`` 详情（从 ``agent_runs`` 组装）。"""

    model_config = ConfigDict(extra="ignore")

    run_id: uuid.UUID
    status: str | None = Field(default=None, description="success / partial / failed")
    created_at: datetime | None = None
    llm_model: str | None = Field(default=None, description="实际模型名（不含 key）")
    parse_attempts: int | None = None
    total_tokens: int | None = None
    period_start: str | None = None
    period_end: str | None = None
    report: WeeklyReportSections | None = None


class WeeklyReportListItem(BaseModel):
    """``GET /api/v1/reports/weekly`` 列表项（精简；summary 截断展示）。"""

    model_config = ConfigDict(extra="ignore")

    run_id: uuid.UUID
    status: str | None = None
    created_at: datetime | None = None
    period_start: str | None = None
    period_end: str | None = None
    summary: str = Field(default="", description="概述截断（列表展示用）")
    confidence: float | None = None
    data_gaps_count: int = 0
    next_week_suggestions_count: int = 0
    parse_attempts: int | None = None
    total_tokens: int | None = None


class WeeklyReportListOut(BaseModel):
    """``GET /api/v1/reports/weekly`` 响应（``created_at`` 倒序）。"""

    model_config = ConfigDict(extra="ignore")

    items: list[WeeklyReportListItem] = Field(default_factory=list)


def sections_from_outputs(outputs: Any) -> WeeklyReportSections | None:
    """从 ``agent_runs.outputs`` 提取报告本体（防御：结构不对时返回 None）。"""
    if not isinstance(outputs, dict):
        return None
    report = outputs.get("report")
    if not isinstance(report, dict):
        return None
    return WeeklyReportSections.model_validate(report)


__all__ = [
    "WeeklyReportDetailOut",
    "WeeklyReportGenerateRequest",
    "WeeklyReportListItem",
    "WeeklyReportListOut",
    "WeeklyReportOut",
    "WeeklyReportSections",
    "sections_from_outputs",
]
