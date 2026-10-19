"""热搜采集服务层：单入口编排（采集 → 落库 → 审计）。

``collect_and_store()`` 是**唯一**的采集入口，被两处调用：

1. 手动刷新：``POST /api/v1/hotspots/refresh``（``trigger="manual"``）；
2. 定时任务：:mod:`app.services.hotspot.scheduler`（``trigger="scheduled"``）。

两者共用**模块级互斥锁** :data:`_collect_lock`：已有采集在跑时，手动刷新直接抛
:class:`FetchInProgress`（路由翻成 409 ``fetch_in_progress``），定时任务则安静跳过。

审计复用 Agent Harness 的 ``AgentRunRecorder``（``run_type=hotspot_fetch``）：
每个源一条 ``node_trace``，``outputs`` 记录每源与总计统计。热搜不接 LLM，
故 ``llm_model`` / ``llm_base_url`` 显式写空串，token 三列保持 NULL。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.tracing.recorder import AgentRunRecorder
from app.core.config import settings
from app.core.timeutil import today_local
from app.crud.hotspot import crud_hotspot
from app.db.session import SessionLocal
from app.models.agent_run import AgentRun
from app.models.enums import RunStatus, RunTrigger, RunType
from app.services.hotspot import adapters
from app.services.hotspot.client import DailyHotClient
from app.services.hotspot.runner import fetch_all_sources
from app.services.hotspot.schemas import (
    CollectSummary,
    CollectTotals,
    SourceStatus,
    SourceStoreStats,
)

logger = logging.getLogger("xhs-agent.hotspot.service")

#: 采集互斥锁：定时作业与手动刷新共用同一入口，防止重叠采集
_collect_lock = threading.Lock()


class FetchInProgress(RuntimeError):
    """已有采集任务在执行中（路由据此返回 409 ``fetch_in_progress``）。"""


def is_running() -> bool:
    """当前是否有采集任务在执行。"""
    return _collect_lock.locked()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def collect_and_store(
    *,
    sources: list[str] | None = None,
    force_refresh: bool | None = None,
    trigger: str = "manual",
    per_source_limit: int | None = None,
    raise_if_busy: bool = True,
) -> CollectSummary | None:
    """执行一次采集并落库，返回汇总；若已有采集在跑见 ``raise_if_busy``。

    Args:
        sources: 指定源（缺省用 ``HOTSPOT_ENABLED_SOURCES``）；小红书会被过滤。
        force_refresh: 是否强刷上游缓存（缺省用配置）。
        trigger: ``manual`` / ``scheduled``。
        per_source_limit: 每源条数（缺省用配置）。
        raise_if_busy: True 抢不到锁时抛 :class:`FetchInProgress`（手动）；
            False 则记日志并返回 ``None``（定时，安静跳过）。

    Returns:
        :class:`CollectSummary`；抢不到锁且 ``raise_if_busy=False`` 时返回 ``None``。
    """
    if not _collect_lock.acquire(blocking=False):
        if raise_if_busy:
            raise FetchInProgress("已有采集任务在执行中，请稍后再试")
        logger.info("已有采集任务在执行中，本次定时触发跳过")
        return None

    try:
        return _collect_locked(
            sources=sources,
            force_refresh=force_refresh,
            trigger=trigger,
            per_source_limit=per_source_limit,
        )
    finally:
        _collect_lock.release()


def _collect_locked(
    *,
    sources: list[str] | None,
    force_refresh: bool | None,
    trigger: str,
    per_source_limit: int | None,
) -> CollectSummary:
    resolved = adapters.resolve_enabled_sources(sources)
    hot_date = today_local()
    trigger_enum = _to_trigger(trigger)
    effective_force = settings.HOTSPOT_FORCE_REFRESH if force_refresh is None else force_refresh
    effective_limit = (
        settings.HOTSPOT_PER_SOURCE_LIMIT if per_source_limit is None else per_source_limit
    )

    recorder = AgentRunRecorder(
        run_type=RunType.HOTSPOT_FETCH,
        kind="hotspot_fetch",
        trigger=trigger_enum,
        inputs={
            "sources": resolved,
            "force_refresh": bool(effective_force),
            "per_source_limit": effective_limit,
        },
        # 热搜采集不接 LLM：显式写空串，token 三列保持 NULL
        llm_model="",
        llm_base_url="",
    )
    recorder.start()
    started = time.perf_counter()
    client = DailyHotClient.from_settings(
        force_refresh=force_refresh, per_source_limit=per_source_limit
    )

    try:
        if not resolved:
            totals = CollectTotals()
            recorder.set_outputs(
                {"hot_date": hot_date.isoformat(), "totals": totals.model_dump(), "per_source": []}
            )
            recorder.finish(
                status=RunStatus.FAILED,
                error_message="没有可用的采集源（HOTSPOT_ENABLED_SOURCES 为空或全被合规过滤）",
            )
            return CollectSummary(
                run_id=str(recorder.run_id),
                trigger=trigger_enum.value,
                status=RunStatus.FAILED.value,
                hot_date=hot_date,
                totals=totals,
                per_source=[],
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        outcomes = fetch_all_sources(resolved, client=client)

        stats: list[SourceStoreStats] = []
        for outcome in outcomes:
            if outcome.status is SourceStatus.SUCCESS:
                # 每源独立 SessionLocal（不跨线程共享 session）
                with SessionLocal() as db:
                    new, updated = crud_hotspot.upsert_many(
                        db, outcome.items, hot_date=hot_date
                    )
                stats.append(
                    SourceStoreStats(
                        source=outcome.source,
                        source_name=outcome.source_name,
                        status=SourceStatus.SUCCESS,
                        fetched=outcome.fetched,
                        new=new,
                        updated=updated,
                    )
                )
            else:
                stats.append(
                    SourceStoreStats(
                        source=outcome.source,
                        source_name=outcome.source_name,
                        status=outcome.status,
                        fetched=0,
                        failed=1 if outcome.status is SourceStatus.FAILED else 0,
                        blocked=1 if outcome.status is SourceStatus.BLOCKED else 0,
                        error=outcome.error,
                    )
                )

        totals = _aggregate(stats)
        status = _compute_status(stats)
        outputs = {
            "hot_date": hot_date.isoformat(),
            "totals": totals.model_dump(),
            "per_source": [s.model_dump(mode="json") for s in stats],
        }
        for stat in stats:
            recorder.add_node_trace(
                node=stat.source,
                status=stat.status.value,
                fetched=stat.fetched,
                new=stat.new,
                updated=stat.updated,
                error=stat.error,
            )
        recorder.set_outputs(outputs)
        latency_ms = int((time.perf_counter() - started) * 1000)
        recorder.finish(status=RunStatus(status))

        logger.info(
            "热搜采集完成：%s（源 %d，新增 %d，更新 %d，失败源 %d，耗时 %dms）",
            status,
            totals.sources,
            totals.new,
            totals.updated,
            totals.failed,
            latency_ms,
        )
        return CollectSummary(
            run_id=str(recorder.run_id),
            trigger=trigger_enum.value,
            status=status,
            hot_date=hot_date,
            totals=totals,
            per_source=stats,
            latency_ms=latency_ms,
        )
    except Exception as exc:  # noqa: BLE001 - 审计记 failed 后继续向上抛
        recorder.finish(
            status=RunStatus.FAILED, error_message=f"{type(exc).__name__}: {exc}"
        )
        raise
    finally:
        client.close()


# ---------------------------------------------------------------------------
# 查询辅助（供路由 /sources、/runs 使用）
# ---------------------------------------------------------------------------
def list_recent_runs(db: Session, *, limit: int = 10) -> list[AgentRun]:
    """最近 ``run_type=hotspot_fetch`` 的运行记录。"""
    stmt = (
        select(AgentRun)
        .where(AgentRun.run_type == RunType.HOTSPOT_FETCH)
        .order_by(AgentRun.created_at.desc())
        .limit(max(1, limit))
    )
    return list(db.execute(stmt).scalars().all())


def source_status_snapshot(db: Session) -> list[dict[str, Any]]:
    """各启用源的信息快照（配置清单 + 最近一次运行的各源统计）。

    聚合来源：最近一条 ``hotspot_fetch`` 的 ``outputs.per_source``；
    若从未采集过，则统计字段为 ``None``，``last_fetched_at`` 回退到
    ``hotspots`` 表的最大 ``fetched_at``。
    """
    keys = adapters.resolve_enabled_sources()
    latest = list_recent_runs(db, limit=1)
    latest_run = latest[0] if latest else None

    per_source: dict[str, dict[str, Any]] = {}
    if latest_run is not None and isinstance(latest_run.outputs, dict):
        for entry in latest_run.outputs.get("per_source", []) or []:
            if isinstance(entry, dict) and entry.get("source"):
                per_source[str(entry["source"])] = entry

    last_fetched_at = None
    if latest_run is not None:
        last_fetched_at = latest_run.finished_at or latest_run.started_at
    if last_fetched_at is None:
        last_fetched_at = crud_hotspot.latest_fetched_at(db)

    snapshot: list[dict[str, Any]] = []
    for key in keys:
        entry = per_source.get(key, {})
        snapshot.append(
            {
                "source": key,
                "name": adapters.source_name_of(key),
                "enabled": True,
                "last_fetched_at": last_fetched_at,
                "last_status": entry.get("status"),
                "last_new": entry.get("new"),
                "last_updated": entry.get("updated"),
                "last_failed": entry.get("failed"),
            }
        )
    return snapshot


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------
def _to_trigger(trigger: str) -> RunTrigger:
    for member in RunTrigger:
        if member.value == (trigger or "").strip().lower():
            return member
    return RunTrigger.MANUAL


def _aggregate(stats: list[SourceStoreStats]) -> CollectTotals:
    """汇总每源统计。``failed`` / ``blocked`` 记的是**源个数**。"""
    return CollectTotals(
        sources=len(stats),
        fetched=sum(s.fetched for s in stats),
        new=sum(s.new for s in stats),
        updated=sum(s.updated for s in stats),
        failed=sum(1 for s in stats if s.status is SourceStatus.FAILED),
        blocked=sum(1 for s in stats if s.status is SourceStatus.BLOCKED),
    )


def _compute_status(stats: list[SourceStoreStats]) -> str:
    """整次采集状态：全部成功=success；部分成功=partial；全失败=failed。"""
    succeeded = sum(1 for s in stats if s.status is SourceStatus.SUCCESS)
    failed = sum(1 for s in stats if s.status is SourceStatus.FAILED)
    if succeeded == 0:
        return RunStatus.FAILED.value
    if failed == 0:
        return RunStatus.SUCCESS.value
    return RunStatus.PARTIAL.value


__all__ = [
    "FetchInProgress",
    "collect_and_store",
    "is_running",
    "list_recent_runs",
    "source_status_snapshot",
]
