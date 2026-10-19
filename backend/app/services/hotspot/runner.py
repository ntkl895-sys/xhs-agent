"""多源并发拉取 + 故障隔离（``ThreadPoolExecutor``）。

设计要点：

1. **并发**：把启用的源分发给线程池同时拉取；每个源各自超时、各自重试。
2. **故障隔离**：任何单源异常都在 :func:`fetch_one` 内被捕获成 ``failed`` /
   ``blocked`` 结果，**绝不向上抛**，从而保证「一个坏源不拖垮整次采集」。
3. **顺序稳定**：返回值顺序与入参 ``sources`` 一致，便于人工核对。
4. **合规兜底**：万一有小红书源绕过上游过滤，这里再拦一次（返回 ``blocked``，不发请求）。

本模块**只负责拉取与归一化**（不接触数据库）；落库与统计在
:mod:`app.services.hotspot.service` 完成。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from app.core.config import settings
from app.services.hotspot.adapters import (
    is_forbidden_source,
    normalize_items,
    source_name_of,
)
from app.services.hotspot.client import DailyHotClient, HotspotClientError, HotspotRetryableError
from app.services.hotspot.schemas import SourceFetchOutcome, SourceStatus

logger = logging.getLogger("xhs-agent.hotspot.runner")

#: 单次采集的最大并发度（对上游保持克制，避免被限流）
MAX_FETCH_WORKERS = 4


def fetch_one(
    source: str,
    *,
    client: DailyHotClient,
    keep_extra_fields: bool,
) -> SourceFetchOutcome:
    """拉取并归一化单个源，**永不抛异常**（异常一律收敛为结果状态）。"""
    name = source_name_of(source)

    if is_forbidden_source(source):
        reason = f"源 {source!r} 属于合规禁用范围，已跳过（不发起任何请求）"
        logger.warning("[%s] %s", name, reason)
        return SourceFetchOutcome(
            source=source, source_name=name, status=SourceStatus.BLOCKED, error=reason
        )

    try:
        payload = client.get_list(source)
        items = normalize_items(
            payload,
            source,
            per_source_limit=client.per_source_limit,
            keep_extra_fields=keep_extra_fields,
        )
    except (HotspotClientError, HotspotRetryableError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        logger.error("[%s] 采集失败：%s", name, reason)
        return SourceFetchOutcome(
            source=source, source_name=name, status=SourceStatus.FAILED, error=reason
        )
    except Exception as exc:  # noqa: BLE001 - 故障隔离：任何异常都不得拖垮整体
        reason = f"{type(exc).__name__}: {exc}"
        logger.error("[%s] 采集异常：%s", name, reason)
        return SourceFetchOutcome(
            source=source, source_name=name, status=SourceStatus.FAILED, error=reason
        )

    if not items:
        logger.warning("[%s] 采集完成但没有有效条目", name)
    else:
        logger.info("[%s] 采集成功：%d 条", name, len(items))

    return SourceFetchOutcome(
        source=source, source_name=name, status=SourceStatus.SUCCESS, items=items
    )


def fetch_all_sources(
    sources: list[str],
    *,
    client: DailyHotClient,
    keep_extra_fields: bool | None = None,
) -> list[SourceFetchOutcome]:
    """并发拉取全部启用源，返回与入参等长且**同序**的结果列表。"""
    if not sources:
        return []

    keep_extra = (
        settings.HOTSPOT_KEEP_EXTRA_FIELDS if keep_extra_fields is None else keep_extra_fields
    )
    max_workers = max(1, min(len(sources), MAX_FETCH_WORKERS))
    logger.info("开始并发采集 %d 个源（并发度 %d）", len(sources), max_workers)

    outcomes: dict[str, SourceFetchOutcome] = {}
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="hotspot") as pool:
        future_map = {
            pool.submit(
                fetch_one, source, client=client, keep_extra_fields=keep_extra
            ): source
            for source in sources
        }
        for future, source in future_map.items():
            try:
                outcomes[source] = future.result()
            except Exception as exc:  # noqa: BLE001 - 理论上不会发生（fetch_one 已兜底）
                outcomes[source] = SourceFetchOutcome(
                    source=source,
                    source_name=source_name_of(source),
                    status=SourceStatus.FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                )

    return [outcomes[source] for source in sources]


__all__ = ["MAX_FETCH_WORKERS", "fetch_all_sources", "fetch_one"]
