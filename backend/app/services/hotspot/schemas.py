"""热搜采集的内部数据模型（字段与 ``hotspots`` 表对齐）。

这层模型**不对外暴露**（HTTP 出入参见 ``app/schemas/hotspot.py``），
只用于采集链路内部传递：适配器产出 :class:`HotspotItem`，
runner 汇总为 :class:`SourceFetchOutcome`，service 再产出 :class:`SourceStoreStats`
与 :class:`CollectSummary`。

约定：``HotspotItem`` 的字段名与 ``hotspots`` 表列名**一一对应**，
使落库时无需再做映射（``source_item_id`` / ``hot_score`` / ``rank_position`` /
``raw_extra`` / ``mobile_url`` / ``cover_url`` / ``summary`` / ``author``）。
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SourceStatus(str, Enum):
    """单个数据源一次采集的结果状态。"""

    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"  # 合规禁用（如小红书占位源），不发起请求
    SKIPPED = "skipped"  # 配置/上游不支持，主动跳过


class HotspotItem(BaseModel):
    """归一化后的单条热搜（与 ``hotspots`` 表列对齐）。

    ``hot_date`` / ``content_hash`` 不在这里计算——它们依赖「当天」与落库上下文，
    统一由 :mod:`app.crud.hotspot` 在写入时补齐，避免时间漂移与重复逻辑。
    """

    model_config = ConfigDict(extra="ignore")

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
    raw_extra: dict[str, Any] = Field(default_factory=dict)
    hot_date: date | None = None


class SourceFetchOutcome(BaseModel):
    """单源「拉取」阶段的结果（尚未落库）。"""

    source: str
    source_name: str
    status: SourceStatus
    items: list[HotspotItem] = Field(default_factory=list)
    error: str | None = None

    @property
    def fetched(self) -> int:
        """本次从上游取得的有效条目数。"""
        return len(self.items)


class SourceStoreStats(BaseModel):
    """单源「拉取 + 落库」后的统计（对外汇总用）。"""

    source: str
    source_name: str
    status: SourceStatus
    fetched: int = 0
    new: int = 0
    updated: int = 0
    failed: int = 0
    blocked: int = 0
    error: str | None = None


class CollectTotals(BaseModel):
    """整次采集的总计。"""

    sources: int = 0
    fetched: int = 0
    new: int = 0
    updated: int = 0
    failed: int = 0
    blocked: int = 0


class CollectSummary(BaseModel):
    """``collect_and_store()`` 的返回值（同时作为 ``agent_runs.outputs`` 的来源）。"""

    run_id: str | None = None
    trigger: str = "manual"
    status: str = "success"  # success / partial / failed
    hot_date: date | None = None
    totals: CollectTotals = Field(default_factory=CollectTotals)
    per_source: list[SourceStoreStats] = Field(default_factory=list)
    latency_ms: int | None = None
