"""周报路由：``/api/v1/reports``（第 17 步）。

契约：

    POST /api/v1/reports/weekly/generate   跑一次周报（同步，201；落 agent_runs）
    GET  /api/v1/reports/weekly            周报历史（created_at 倒序，?limit=）
    GET  /api/v1/reports/weekly/{run_id}   单条周报详情

要点：

- **只增不改**：本步**不提供** PUT / PATCH / DELETE —— 周报一经生成不可修改
  （结论存 ``agent_runs.outputs``，随运行记录自然留存）；
- **静态路径在前**：``/weekly/generate`` 必须先于 ``/weekly/{run_id}`` 注册；
- 周报 = 只读 + 建议：**零业务表写入**（不改 notes / topics / note_metrics /
  diagnostics），也不触发任何发布动作；
- 错误统一为 ``{status, code, message, details}``：400（LLM 未就绪 / 结构重试
  超限 / 其它运行失败）/ 404（周报记录不存在）/ 422（周期参数不合法）；
  **响应不含 Traceback 与 ``llm_raw``**（统一错误体由 service 翻译，app.main 渲染）。

⚠️ 路由层不写任何 SQLAlchemy 表达式（第 8 步分层纪律，``selftest_api`` 静态断言）。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.agent.weekly.service import (
    generate_weekly_report,
    get_weekly_report,
    list_weekly_reports,
)
from app.api.deps import get_db
from app.core.errors import not_found
from app.schemas.common import ErrorOutSpec
from app.schemas.report import (
    WeeklyReportDetailOut,
    WeeklyReportGenerateRequest,
    WeeklyReportListItem,
    WeeklyReportListOut,
    WeeklyReportOut,
    WeeklyReportSections,
    sections_from_outputs,
)

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])

#: 列表摘要截断长度（仅列表展示用；详情返回完整 summary）
_LIST_SUMMARY_LEN = 160


def _as_float(value: Any) -> float | None:
    """宽容解析（库中历史数据可能是字符串数字；失败 → None）。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _outputs_of(row: Any) -> dict[str, Any]:
    """取 ``agent_runs.outputs``（防御：非 dict 时给空 dict）。"""
    return row.outputs if isinstance(row.outputs, dict) else {}


def _report_of(outputs: dict[str, Any]) -> dict[str, Any]:
    """取 outputs 里的 report 本体（防御：结构不对时给空 dict）。"""
    report = outputs.get("report")
    return report if isinstance(report, dict) else {}


# ---------------------------------------------------------------------------
# POST /weekly/generate（静态路径，先注册）
# ---------------------------------------------------------------------------
@router.post(
    "/weekly/generate",
    response_model=WeeklyReportOut,
    status_code=201,
    summary="生成一次周报（同步，落 agent_runs）",
    responses={
        **ErrorOutSpec.BAD_REQUEST,
        **ErrorOutSpec.VALIDATION,
    },
)
def generate_weekly(
    payload: WeeklyReportGenerateRequest | None = None,
) -> WeeklyReportOut:
    """跑一次周报并把结论写入 ``agent_runs.outputs``，返回摘要 + 报告本体。

    - 周期参数非法 / 互斥冲突 → 422（**零运行记录产生**）；
    - LLM 未就绪 → 400（**不发起任何对外请求、不产生运行记录**）；
    - 结构校验连续超限 → 400（可重试）；
    - 少数据 / 无数据**不是错误**：正常产出周报，``data_gaps`` 如实声明
      （不硬造亮点）。
    """
    data = payload or WeeklyReportGenerateRequest()
    summary = generate_weekly_report(
        period_start=data.period_start,
        period_end=data.period_end,
        days=data.days,
    )
    report = summary.get("report")
    return WeeklyReportOut(
        run_id=uuid.UUID(str(summary["run_id"])),
        period_start=summary.get("period_start"),
        period_end=summary.get("period_end"),
        report=WeeklyReportSections.model_validate(report)
        if isinstance(report, dict)
        else None,
        parse_attempts=summary.get("parse_attempts"),
        data_gaps=list(summary.get("data_gaps") or []),
        warnings=list(summary.get("warnings") or []),
    )


# ---------------------------------------------------------------------------
# GET /weekly（周报历史）
# ---------------------------------------------------------------------------
@router.get(
    "/weekly",
    response_model=WeeklyReportListOut,
    summary="周报历史（created_at 倒序）",
)
def list_weekly(
    limit: int = Query(10, ge=1, le=50, description="返回条数，1~50"),
    db: Session = Depends(get_db),
) -> WeeklyReportListOut:
    """最近 ``run_type=weekly_report`` 的运行（``id`` 兜底保证顺序稳定）。"""
    rows = list_weekly_reports(db, limit=limit)
    items: list[WeeklyReportListItem] = []
    for row in rows:
        outputs = _outputs_of(row)
        report = _report_of(outputs)
        items.append(
            WeeklyReportListItem(
                run_id=row.id,
                status=row.status.value if row.status is not None else None,
                created_at=row.created_at,
                period_start=outputs.get("period_start"),
                period_end=outputs.get("period_end"),
                summary=str(report.get("summary") or "")[:_LIST_SUMMARY_LEN],
                confidence=_as_float(report.get("confidence")),
                data_gaps_count=len(report.get("data_gaps") or []),
                next_week_suggestions_count=len(
                    report.get("next_week_suggestions") or []
                ),
                parse_attempts=row.json_parse_attempts,
                total_tokens=row.total_tokens,
            )
        )
    return WeeklyReportListOut(items=items)


# ---------------------------------------------------------------------------
# GET /weekly/{run_id}（UUID 路径，最后注册）
# ---------------------------------------------------------------------------
@router.get(
    "/weekly/{run_id}",
    response_model=WeeklyReportDetailOut,
    summary="周报详情",
    responses=ErrorOutSpec.NOT_FOUND,
)
def get_weekly(run_id: uuid.UUID, db: Session = Depends(get_db)) -> WeeklyReportDetailOut:
    """按 ``run_id`` 取单条周报（不存在 / 非周报类型 → 404）。"""
    row = get_weekly_report(db, run_id)
    if row is None:
        raise not_found("周报记录不存在", details={"run_id": str(run_id)})
    outputs = _outputs_of(row)
    return WeeklyReportDetailOut(
        run_id=row.id,
        status=row.status.value if row.status is not None else None,
        created_at=row.created_at,
        llm_model=row.llm_model,
        parse_attempts=row.json_parse_attempts,
        total_tokens=row.total_tokens,
        period_start=outputs.get("period_start"),
        period_end=outputs.get("period_end"),
        report=sections_from_outputs(outputs),
    )
