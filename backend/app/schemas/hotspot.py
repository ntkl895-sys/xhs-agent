"""hotspots 的 Pydantic schema（第 9 步 HTTP 出入参）。

沿用第 8 步范式：列表统一 ``Page``，错误统一 ``AppError``。
``HotspotOut`` 字段与 ``hotspots`` 表**一一对齐**（不新增列、不做 join 造字段）。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import HotspotStatus, RunStatus, RunTrigger, RunType


class HotspotOut(BaseModel):
    """单条热搜输出（字段对齐 ``hotspots`` 表）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    source_item_id: str | None = None
    title: str
    url: str | None = None
    mobile_url: str | None = None
    hot_score: int | None = None
    rank_position: int | None = None
    cover_url: str | None = None
    summary: str | None = None
    author: str | None = None
    raw_extra: dict[str, Any] | None = None
    hot_date: date | None = None
    fetched_at: datetime | None = None
    content_hash: str
    status: HotspotStatus
    created_at: datetime


class HotspotRefreshRequest(BaseModel):
    """``POST /refresh`` 请求体（可整段省略）。"""

    sources: list[str] | None = Field(
        default=None,
        description="指定要采集的源；缺省用配置里的启用源。小红书相关标识会被自动过滤。",
    )


class HotspotUpdateRequest(BaseModel):
    """``PATCH /{hotspot_id}`` 请求体（第 11 步）：人工标记处理状态。

    仅一个字段 ``status``，取值 ``new`` / ``adopted`` / ``ignored``（三态可人工互逆切换）；
    非法值由 Pydantic 直接返 422。不物理删除、不改其它字段、不联动 ``topics``。
    """

    status: HotspotStatus = Field(description="new（新）/ adopted（已采用）/ ignored（已忽略）")


class SourceStatOut(BaseModel):
    """单源统计。"""

    source: str
    source_name: str
    status: str
    fetched: int = 0
    new: int = 0
    updated: int = 0
    failed: int = 0
    blocked: int = 0
    error: str | None = None


class CollectTotalsOut(BaseModel):
    """整次采集总计（``failed`` / ``blocked`` 记的是**源个数**）。"""

    sources: int = 0
    fetched: int = 0
    new: int = 0
    updated: int = 0
    failed: int = 0
    blocked: int = 0


class HotspotRefreshOut(BaseModel):
    """``POST /refresh`` 响应：整次汇总 + 每源统计。"""

    run_id: str | None = None
    trigger: str
    status: str
    hot_date: date | None = None
    latency_ms: int | None = None
    totals: CollectTotalsOut
    per_source: list[SourceStatOut] = Field(default_factory=list)


class HotspotSourceOut(BaseModel):
    """``GET /sources`` 的单源信息（配置清单 + 最近一次运行统计）。"""

    source: str
    name: str
    enabled: bool
    last_fetched_at: datetime | None = None
    last_status: str | None = None
    last_new: int | None = None
    last_updated: int | None = None
    last_failed: int | None = None


class HotspotRunOut(BaseModel):
    """``GET /runs`` 的运行记录（精简）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_type: RunType
    trigger: RunTrigger
    status: RunStatus | None = None
    inputs: dict[str, Any] | None = None
    outputs: dict[str, Any] | None = None
    latency_ms: int | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


__all__ = [
    "CollectTotalsOut",
    "HotspotOut",
    "HotspotRefreshOut",
    "HotspotRefreshRequest",
    "HotspotRunOut",
    "HotspotSourceOut",
    "HotspotUpdateRequest",
    "SourceStatOut",
]
