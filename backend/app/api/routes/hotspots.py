"""热搜路由：``/api/v1/hotspots``（第 9 步）。

契约：

    GET   /api/v1/hotspots            分页列表（source / status / q / hot_date / sort / order）
    GET   /api/v1/hotspots/sources    各源状态（配置清单 + 最近一次采集统计）
    POST  /api/v1/hotspots/refresh    手动触发一次采集（同步；采集中返回 409）
    GET   /api/v1/hotspots/runs       最近 hotspot_fetch 运行记录（limit）
    PATCH /api/v1/hotspots/{id}       人工标记状态（new / adopted / ignored，三态互逆）——第 11 步

沿用第 8 步的统一错误体与 ``{items,total,page,page_size}`` 分页结构；
路由层**不写 SQLAlchemy 表达式**，查询条件全部委托 ``app.crud.hotspot``。
采集与汇总委托 ``app.services.hotspot.service``。

``/sources`` 与 ``/runs`` 返回**普通数组**（非分页资源），保持语义直白。
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import PageParams, get_db, pagination
from app.core.errors import AppError, not_found, validation_error
from app.crud.hotspot import crud_hotspot
from app.models.enums import HotspotStatus
from app.schemas.common import ErrorOutSpec, Page
from app.schemas.hotspot import (
    HotspotOut,
    HotspotRefreshOut,
    HotspotRefreshRequest,
    HotspotRunOut,
    HotspotSourceOut,
    HotspotUpdateRequest,
)
from app.services.hotspot import service

router = APIRouter(prefix="/api/v1/hotspots", tags=["hotspots"])

_SORT_KEYS = ("hot_score", "fetched_at")
_ORDER_KEYS = ("desc", "asc")


def _normalize_status(raw: str | None) -> HotspotStatus | None:
    """``status``：不传 / 空 / ``all`` = 不过滤；否则必须是合法枚举。"""
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in ("", "all"):
        return None
    try:
        return HotspotStatus(value)
    except ValueError:
        raise validation_error(
            "status 只能是 new / adopted / ignored，或留空 / all 表示全部",
            details=[{"loc": ["query", "status"], "msg": "非法取值", "type": "enum"}],
        ) from None


def _parse_hot_date(raw: str | None) -> tuple[date | None, bool]:
    """``hot_date``：缺省 = 今天；``all`` = 不限日期；否则 ``YYYY-MM-DD``。"""
    if raw is None:
        return (None, False)
    value = raw.strip()
    if value.lower() in ("", "all"):
        return (None, True)
    try:
        return (date.fromisoformat(value), False)
    except ValueError:
        raise validation_error(
            "hot_date 需为 YYYY-MM-DD，或 all 表示不限日期",
            details=[{"loc": ["query", "hot_date"], "msg": "非法日期", "type": "value_error"}],
        ) from None


def _normalize_sort(raw: str | None) -> str:
    value = (raw or "").strip().lower() or "hot_score"
    if value not in _SORT_KEYS:
        raise validation_error(
            "sort 只能是 hot_score / fetched_at",
            details=[{"loc": ["query", "sort"], "msg": "非法取值", "type": "enum"}],
        )
    return value


def _normalize_order(raw: str | None) -> str:
    value = (raw or "").strip().lower() or "desc"
    if value not in _ORDER_KEYS:
        raise validation_error(
            "order 只能是 desc / asc",
            details=[{"loc": ["query", "order"], "msg": "非法取值", "type": "enum"}],
        )
    return value


@router.get(
    "",
    response_model=Page[HotspotOut],
    summary="热搜列表",
    responses=ErrorOutSpec.VALIDATION,
)
def list_hotspots(
    db: Session = Depends(get_db),
    page_params: PageParams = Depends(pagination),
    source: str | None = Query(None, description="源标识精确匹配，如 zhihu"),
    status: str | None = Query(None, description="new | adopted | ignored；留空或 all 表示全部"),
    q: str | None = Query(None, description="按标题模糊匹配"),
    hot_date: str | None = Query(None, description="YYYY-MM-DD；缺省=今天；all=不限日期"),
    sort: str | None = Query(None, description="hot_score（默认）| fetched_at"),
    order: str | None = Query(None, description="desc（默认）| asc"),
) -> Page[HotspotOut]:
    """分页查询热搜；默认只看**今天**、按 ``hot_score`` 倒序。"""
    hot_date_value, include_all = _parse_hot_date(hot_date)
    criteria = crud_hotspot.build_criteria(
        source=source,
        status=_normalize_status(status),
        q=q,
        hot_date=hot_date_value,
        include_all_dates=include_all,
    )
    total = crud_hotspot.count(db, *criteria)
    rows = crud_hotspot.get_multi_sorted(
        db,
        criteria=criteria,
        sort=_normalize_sort(sort),
        order=_normalize_order(order),
        offset=page_params.offset,
        limit=page_params.limit,
    )
    return Page[HotspotOut](
        items=[HotspotOut.model_validate(row) for row in rows],
        total=total,
        page=page_params.page,
        page_size=page_params.page_size,
    )


@router.get(
    "/sources",
    response_model=list[HotspotSourceOut],
    summary="各源状态",
)
def list_sources(db: Session = Depends(get_db)) -> list[HotspotSourceOut]:
    """当前启用源清单 + 最近一次采集的每源统计。

    ``dailyhot`` 容器的可用性**不**反映在这里（更不作为就绪探针）；
    这里只按已发生的采集结果与配置展示。
    """
    return [HotspotSourceOut(**row) for row in service.source_status_snapshot(db)]


@router.get(
    "/runs",
    response_model=list[HotspotRunOut],
    summary="最近采集运行",
)
def list_runs(
    db: Session = Depends(get_db),
    limit: int = Query(10, ge=1, le=50, description="返回条数，1~50"),
) -> list[HotspotRunOut]:
    """最近 ``run_type=hotspot_fetch`` 的运行记录（精简）。"""
    rows = service.list_recent_runs(db, limit=limit)
    return [HotspotRunOut.model_validate(row) for row in rows]


@router.post(
    "/refresh",
    response_model=HotspotRefreshOut,
    summary="手动触发采集",
    responses={**ErrorOutSpec.CONFLICT, **ErrorOutSpec.VALIDATION},
)
def refresh_hotspots(payload: HotspotRefreshRequest | None = None) -> HotspotRefreshOut:
    """同步触发一次采集并落库，返回每源与总计统计。

    已有采集在跑时返回 **409 ``fetch_in_progress``**（``AppError``）。
    本路由用 ``def``，由 FastAPI 线程池执行，避免阻塞事件循环。
    """
    sources = payload.sources if payload is not None else None
    try:
        summary = service.collect_and_store(sources=sources, trigger="manual", raise_if_busy=True)
    except service.FetchInProgress as exc:
        raise AppError("fetch_in_progress", str(exc), status_code=409) from exc
    assert summary is not None  # raise_if_busy=True 时不会为 None
    return HotspotRefreshOut.model_validate(summary.model_dump(mode="json"))


@router.patch(
    "/{hotspot_id}",
    response_model=HotspotOut,
    summary="人工标记热点状态（第 11 步）",
    responses={**ErrorOutSpec.NOT_FOUND, **ErrorOutSpec.VALIDATION},
)
def update_hotspot(
    hotspot_id: uuid.UUID,
    payload: HotspotUpdateRequest,
    db: Session = Depends(get_db),
) -> HotspotOut:
    """仅更新 ``status``（``new`` / ``adopted`` / ``ignored``），三态可人工互逆切换。

    - 热点不存在 → **404**；
    - 非法状态由 Pydantic 校验 → **422**；
    - **不物理删除、不改其它字段、不联动 topics**（人工标记语义，与选题解耦）。
    """
    hotspot = crud_hotspot.set_status(db, hotspot_id, payload.status)
    if hotspot is None:
        raise not_found("热点不存在", details={"hotspot_id": str(hotspot_id)})
    return HotspotOut.model_validate(hotspot)
