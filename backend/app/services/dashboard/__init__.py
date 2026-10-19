"""运营看板（第 17 步）——确定性聚合服务，零 LLM、零网络。

对外：

- :func:`app.services.dashboard.service.build_summary` —— 计数 + 全账号最新漏斗；
- :func:`app.services.dashboard.service.build_trend` —— 指标趋势点（时间桶）；
- :func:`app.services.dashboard.service.build_ranking` —— 指标排行；
- :mod:`app.services.dashboard.aggregator` —— 纯函数（可离线单测）；
- :mod:`app.services.dashboard.aggregator` 的常量（``MIN_RANGE_DAYS`` /
  ``MAX_RANGE_DAYS`` / ``BUCKET_VALUES``）供路由层做参数校验。

红线：**只读**（不改任何业务表）、不请求任何外部域名、不做任何 LLM 调用；
所有算术由本地纯函数完成，前端与模型都不得参与计算。
"""

from __future__ import annotations

from app.services.dashboard.aggregator import (
    BUCKET_AUTO,
    BUCKET_DAY,
    BUCKET_VALUES,
    MAX_RANGE_DAYS,
    MIN_RANGE_DAYS,
    aggregate_trend,
    latest_values_per_note,
    range_start_local,
    rank_items,
    rates_from_sums,
    resolve_step_days,
    sum_funnel,
)
from app.services.dashboard.service import (
    build_ranking,
    build_summary,
    build_trend,
    load_snapshots,
)

__all__ = [
    "BUCKET_AUTO",
    "BUCKET_DAY",
    "BUCKET_VALUES",
    "MAX_RANGE_DAYS",
    "MIN_RANGE_DAYS",
    "aggregate_trend",
    "build_ranking",
    "build_summary",
    "build_trend",
    "latest_values_per_note",
    "load_snapshots",
    "range_start_local",
    "rank_items",
    "rates_from_sums",
    "resolve_step_days",
    "sum_funnel",
]
