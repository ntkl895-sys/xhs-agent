"""运营看板路由：``/api/v1/dashboard``（第 17 步）。

契约：

    GET /api/v1/dashboard/summary   计数 + 全账号最新漏斗（?range_days=）
    GET /api/v1/dashboard/trend     指标趋势点（?metric=&range_days=&note_id=&bucket=）
    GET /api/v1/dashboard/ranking   指标排行（?metric=&limit=）

要点：

- **仅 GET**：不提供 POST / PUT / PATCH / DELETE（看板是确定性只读聚合，
  报告走 ``/api/v1/reports``；本组接口零 LLM、零写入）；
- **参数服务端校验**：``metric`` 六指标白名单、``range_days`` 上下限、
  ``bucket`` 二值——越界 / 非法由 FastAPI 直接 422（统一错误体）；
- **无数据不是错误**：空集合 / 空值（``null``）如实返回，绝不补 0 臆造；
- ⚠️ 路由层不写任何 SQLAlchemy 表达式（第 8 步分层纪律，``selftest_api`` 静态断言）：
  只依赖 Depends + Query，聚合逻辑全部在 service（``app.services.dashboard``）。

依赖说明：本组接口**不消耗任何 token**（无 LLM 调用），也不返回
``llm_raw`` / key / base64 / 绝对路径。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import settings
from app.schemas.common import ErrorOutSpec
from app.schemas.dashboard import (
    DashboardRankingOut,
    DashboardSummaryOut,
    DashboardTrendOut,
    MetricName,
    TrendBucket,
)
from app.services.dashboard.aggregator import (
    BUCKET_DAY,
    BUCKET_VALUES,
    MAX_RANGE_DAYS,
    MIN_RANGE_DAYS,
)
from app.services.dashboard.service import build_ranking, build_summary, build_trend

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


# ---------------------------------------------------------------------------
# GET /summary
# ---------------------------------------------------------------------------
@router.get(
    "/summary",
    response_model=DashboardSummaryOut,
    summary="运营看板汇总（计数 + 全账号最新漏斗）",
    responses=ErrorOutSpec.VALIDATION,
)
def get_dashboard_summary(
    range_days: int = Query(
        default=settings.DASHBOARD_DEFAULT_RANGE_DAYS,
        ge=MIN_RANGE_DAYS,
        le=MAX_RANGE_DAYS,
        description="「周期内新发布数」统计窗口（近 N 个自然日，含今天）",
    ),
    db: Session = Depends(get_db),
) -> DashboardSummaryOut:
    """返回计数与全账号最新漏斗（published 笔记合计口径；缺失按 0 且覆盖率可识别）。"""
    data = build_summary(db, range_days=range_days)
    return DashboardSummaryOut.model_validate(data)


# ---------------------------------------------------------------------------
# GET /trend
# ---------------------------------------------------------------------------
@router.get(
    "/trend",
    response_model=DashboardTrendOut,
    summary="指标趋势点（时间桶聚合）",
    responses={**ErrorOutSpec.NOT_FOUND, **ErrorOutSpec.VALIDATION},
)
def get_dashboard_trend(
    metric: MetricName = Query(description="指标（六指标之一）"),
    range_days: int = Query(
        default=settings.DASHBOARD_DEFAULT_RANGE_DAYS,
        ge=MIN_RANGE_DAYS,
        le=MAX_RANGE_DAYS,
        description="趋势窗口（近 N 个自然日，含今天）",
    ),
    note_id: uuid.UUID | None = Query(
        default=None, description="单笔记视图（不传 = 全账号，只统计 published）"
    ),
    bucket: TrendBucket = Query(
        default=BUCKET_DAY,
        description=f"分桶模式：{' / '.join(BUCKET_VALUES)}；超出点数上限时自动收紧为 auto",
    ),
    db: Session = Depends(get_db),
) -> DashboardTrendOut:
    """返回该指标的趋势点（空桶 ``null`` 不插值；``coverage`` 给出覆盖率）。

    - ``note_id`` 不存在 → 404；``metric`` / ``range_days`` / ``bucket``
      非法 → 422；
    - 实际生效的 ``bucket`` 在响应里如实返回（请求 day 但区间超上限时收紧为 auto）。
    """
    data = build_trend(
        db,
        metric=metric,
        range_days=range_days,
        note_id=note_id,
        bucket=bucket,
        max_points=settings.DASHBOARD_TREND_MAX_POINTS,
    )
    return DashboardTrendOut.model_validate(data)


# ---------------------------------------------------------------------------
# GET /ranking
# ---------------------------------------------------------------------------
@router.get(
    "/ranking",
    response_model=DashboardRankingOut,
    summary="指标排行（published 笔记）",
    responses=ErrorOutSpec.VALIDATION,
)
def get_dashboard_ranking(
    metric: MetricName = Query(description="排行指标（六指标之一）"),
    limit: int = Query(
        default=settings.DASHBOARD_RANKING_TOP_N,
        ge=1,
        le=100,
        description="返回条数上限",
    ),
    db: Session = Depends(get_db),
) -> DashboardRankingOut:
    """按该指标最新非空值降序返回 published 笔记（缺失值排最后、不臆造）。"""
    data = build_ranking(db, metric=metric, limit=limit)
    return DashboardRankingOut.model_validate(data)
