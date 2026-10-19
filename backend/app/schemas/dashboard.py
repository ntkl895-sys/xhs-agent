"""看板 schema（第 17 步 · 运营看板）。

三个响应模型（均为**只读**聚合结果；绝不含 ``llm_raw`` / key / base64 /
``screenshot_path`` 等敏感字段）：

- ``DashboardSummaryOut``：计数 + 全账号最新漏斗 + 生成时间；
- ``DashboardTrendOut``：单指标趋势点（``bucket`` = 生效分桶模式）+ 覆盖率；
- ``DashboardRankingOut``：单指标排行（缺失值 ``value=None`` 排最后）。

参数白名单（路由层 Query 校验，越界 / 非白名单 → 422）：

- ``metric``：六指标之一（:data:`METRIC_FIELDS` 同口径）；
- ``range_days``：``MIN_RANGE_DAYS ~ MAX_RANGE_DAYS``；
- ``bucket``：``day`` / ``auto``。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: 指标白名单（与 note_metrics 六指标一致；非法值由 FastAPI 直接 422）
MetricName = Literal["views", "likes", "collects", "comments", "shares", "new_followers"]

#: 趋势分桶模式（``day`` 自然日 / ``auto`` 等间隔）
TrendBucket = Literal["day", "auto"]


class DashboardFunnel(BaseModel):
    """全账号最新漏斗（published 笔记合计口径）。"""

    model_config = ConfigDict(extra="ignore")

    latest_totals: dict[str, int] = Field(
        default_factory=dict, description="六指标合计（缺失按 0；覆盖率见 metric_coverage）"
    )
    rates: dict[str, float | None] = Field(
        default_factory=dict, description="六率（views 合计<=0 时全 null；4 位小数）"
    )
    covered_notes: int = Field(default=0, description="至少一个指标有值的 published 笔记数")
    published_notes: int = Field(default=0, description="参与统计的 published 笔记总数")
    metric_coverage: dict[str, int] = Field(
        default_factory=dict, description="每个指标有非空值的笔记数（覆盖率可识别）"
    )


class DashboardSummaryOut(BaseModel):
    """``GET /api/v1/dashboard/summary`` 响应。"""

    model_config = ConfigDict(extra="ignore")

    counts: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "计数：accounts / notes_total / notes_published / notes_ready / "
            "notes_draft / topics_writing / notes_published_in_range"
        ),
    )
    funnel: DashboardFunnel
    range_days: int = Field(description="周期窗口（近 N 个自然日，含今天）")
    generated_at: datetime = Field(description="聚合生成时间（本地时区）")


class TrendPoint(BaseModel):
    """趋势上的一个桶（``value=None`` 表示该桶无数据，前端断线展示）。"""

    model_config = ConfigDict(extra="ignore")

    bucket: str = Field(description="桶起始日（YYYY-MM-DD，本地时区）")
    value: int | None = Field(default=None, description="该桶合计值；无数据为 null")


class TrendCoverage(BaseModel):
    """趋势覆盖率信息（可识别「有多少笔记 / 多少桶真的有数据」）。"""

    model_config = ConfigDict(extra="ignore")

    notes: int = Field(default=0, description="参与统计的笔记数（全账号=published 数）")
    notes_with_data: int = Field(default=0, description="周期内该指标有非空值的笔记数")
    points_with_data: int = Field(default=0, description="有数据的桶数")
    points_total: int = Field(default=0, description="桶总数")
    step_days: int = Field(default=1, description="实际桶步长（天）")
    range_days: int = Field(default=0, description="请求的周期窗口（天）")


class DashboardTrendOut(BaseModel):
    """``GET /api/v1/dashboard/trend`` 响应。"""

    model_config = ConfigDict(extra="ignore")

    metric: MetricName
    bucket: TrendBucket = Field(description="**生效**分桶模式（请求 day 但超点数上限时会收紧为 auto）")
    points: list[TrendPoint] = Field(default_factory=list)
    coverage: TrendCoverage = Field(default_factory=TrendCoverage)


class RankingItem(BaseModel):
    """排行中的一篇笔记（``value=None`` = 该指标暂无数据）。"""

    model_config = ConfigDict(extra="ignore")

    note_id: uuid.UUID
    title: str | None = None
    value: int | None = Field(default=None, description="该指标最新非空值；无数据为 null")
    published_at: datetime | None = None


class DashboardRankingOut(BaseModel):
    """``GET /api/v1/dashboard/ranking`` 响应。"""

    model_config = ConfigDict(extra="ignore")

    metric: MetricName
    items: list[RankingItem] = Field(default_factory=list)


__all__ = [
    "DashboardFunnel",
    "DashboardRankingOut",
    "DashboardSummaryOut",
    "DashboardTrendOut",
    "MetricName",
    "RankingItem",
    "TrendBucket",
    "TrendCoverage",
    "TrendPoint",
]
