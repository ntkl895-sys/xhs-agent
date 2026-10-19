"""运营看板聚合（第 17 步）——**确定性纯函数**，零 LLM、零网络、不触库。

所有算术都在本模块内完成（**禁止把算术交给前端或模型**）；``service.py`` 负责
取数（ORM 查询）后把普通 dict 交给这里，便于离线单测。

口径（任务书第 2 部分，逐条落实）：

- 「最新值」= 每篇笔记按 ``captured_at`` 升序后每个指标的**最后一个非空值**
  ——直接复用第 16 步 :func:`app.agent.diagnosis.contracts.metric_latest`，
  **绝不重复实现第二套口径**；
- 漏斗 = 对每篇 published 笔记的最新六指标**按指标求和**（缺失按 0 处理，
  覆盖率经 ``metric_coverage`` / ``covered_notes`` 显式给出），再用**合计值**
  计算六率；views 合计 <= 0 时各率 None（绝不臆造）；比率统一 4 位小数；
- 趋势 = 时间桶聚合：全账号时同一时间桶内各笔记取「桶内最新非空值」再求和；
  空桶给 ``None``（前端断线，**禁止补 0 / 插值**）；
- 排行 = published 笔记按该指标最新非空值降序，**缺失值排最后、不臆造**。

时间口径：所有「天」均按本地时区（Asia/Shanghai，见 ``app.core.timeutil``）；
快照时间在模块内以 ISO 字符串流转（与状态/检查点的序列化口径一致）。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any

from app.agent.diagnosis.contracts import (
    METRIC_FIELDS,
    RATE_FIELDS,
    RATE_PRECISION,
    metric_latest,
)
from app.core.timeutil import LOCAL_TIMEZONE

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: trend / summary 的 range_days 允许区间（服务端校验；越界 → 422）
MIN_RANGE_DAYS = 1
MAX_RANGE_DAYS = 365

#: 趋势分桶模式：``day`` 按自然日；``auto`` 等间隔（天数由点数上限推导）
BUCKET_DAY = "day"
BUCKET_AUTO = "auto"
BUCKET_VALUES = (BUCKET_DAY, BUCKET_AUTO)

#: 六率 -> 单分子指标（engagement 特殊：四互动求和，单独处理）
_RATE_NUMERATORS: dict[str, str] = {
    "like_rate": "likes",
    "collect_rate": "collects",
    "comment_rate": "comments",
    "share_rate": "shares",
    "follow_rate": "new_followers",
}

#: engagement 的分子构成（缺失按 0 求和，与「合计值」口径一致）
_ENGAGEMENT_PARTS: tuple[str, ...] = ("likes", "collects", "comments", "shares")

#: 一天秒数（分桶用）
_SECONDS_PER_DAY = 86400


# ---------------------------------------------------------------------------
# 时间工具（看板与周报共用）
# ---------------------------------------------------------------------------
def as_datetime(value: Any) -> datetime | None:
    """datetime / ISO 字符串 → datetime（解析失败 → None；与第 16 步同语义）。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _ts(value: Any) -> float | None:
    """datetime / ISO 字符串 → epoch 秒（naive 视为 UTC；失败 → None）。"""
    dt = as_datetime(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _sort_row_key(row: dict[str, Any]) -> tuple[str, str]:
    """快照的稳定排序键：captured_at（ISO 字符串）+ id（并列兜底）。

    统一 ISO 字符串比较（同 offset 下与时间顺序一致）——避免 naive / aware
    混用的比较异常（第 12 步教训：顺序必须在本地确定）。
    """
    captured = row.get("captured_at")
    if isinstance(captured, datetime):
        captured = captured.isoformat()
    return (str(captured or ""), str(row.get("id") or ""))


def range_start_local(now: datetime, range_days: int) -> datetime:
    """「近 N 个自然日」的起点（本地时区，含今天在内共 N 天）。

    ``range_days=7``、今天 10-12 → 起点 = 10-06 00:00（本地）。
    """
    start_day = now.date() - timedelta(days=max(1, int(range_days)) - 1)
    return datetime.combine(start_day, time.min, tzinfo=LOCAL_TIMEZONE)


# ---------------------------------------------------------------------------
# 1) 每笔记最新值（复用第 16 步口径）
# ---------------------------------------------------------------------------
def latest_values_per_note(
    snapshots: list[dict[str, Any]],
) -> dict[str, dict[str, int | None]]:
    """按笔记分组取「最新值」（每指标最后一个非空值）。

    Args:
        snapshots: 快照普通 dict 列表（含 ``note_id`` / ``captured_at`` / 六指标；
            顺序不限，本函数内部按 ``captured_at`` 升序稳定排序后再取）。

    Returns:
        ``{note_id: {指标: 值}}``（无快照的笔记不在结果中）。
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in snapshots:
        note_id = str(row.get("note_id") or "")
        if not note_id:
            continue
        grouped.setdefault(note_id, []).append(row)

    result: dict[str, dict[str, int | None]] = {}
    for note_id, group in grouped.items():
        ordered = sorted(group, key=_sort_row_key)
        latest, _ = metric_latest(ordered)
        result[note_id] = latest
    return result


# ---------------------------------------------------------------------------
# 2) 合计漏斗（published 笔记）
# ---------------------------------------------------------------------------
def rates_from_sums(sums: dict[str, int | None]) -> dict[str, float | None]:
    """由**合计值**计算六率（合计口径；4 位小数）。

    - ``views`` 合计缺失 / <= 0 → 六率全部 ``None``（绝不臆造）；
    - 分子缺失按 0（与合计口径一致），单测直接传 ``None`` 时该率给 ``None``
      （防御分支；service 路径不会出现）。
    """
    rates: dict[str, float | None] = {key: None for key in RATE_FIELDS}
    views = sums.get("views")
    if views is None or int(views) <= 0:
        return rates
    views_value = int(views)
    for rate_key, field in _RATE_NUMERATORS.items():
        numerator = sums.get(field)
        if numerator is None:
            continue
        rates[rate_key] = round(int(numerator) / views_value, RATE_PRECISION)

    parts = [sums.get(field) for field in _ENGAGEMENT_PARTS]
    engagement = 0
    for item in parts:
        engagement += int(item) if item is not None else 0
    rates["engagement_rate"] = round(engagement / views_value, RATE_PRECISION)
    return rates


def sum_funnel(
    latest_by_note: dict[str, dict[str, int | None]],
) -> dict[str, Any]:
    """对多篇笔记的最新六指标**按指标求和**并算六率（published 口径由调用方过滤）。

    Returns:
        ``{latest_totals, rates, covered_notes, metric_coverage}``：

        - ``latest_totals``：六指标合计（缺失按 0）；
        - ``rates``：六率（views 合计 <= 0 时全 None）；
        - ``covered_notes``：至少有一个非空最新值的笔记数（覆盖率可识别）；
        - ``metric_coverage``：每个指标有非空值的笔记数。
    """
    totals: dict[str, int] = {field: 0 for field in METRIC_FIELDS}
    coverage: dict[str, int] = {field: 0 for field in METRIC_FIELDS}
    covered_notes = 0

    for latest in latest_by_note.values():
        has_any = False
        for field in METRIC_FIELDS:
            value = latest.get(field)
            if value is not None:
                totals[field] += int(value)
                coverage[field] += 1
                has_any = True
        if has_any:
            covered_notes += 1

    return {
        "latest_totals": totals,
        "rates": rates_from_sums(totals),
        "covered_notes": covered_notes,
        "metric_coverage": coverage,
    }


# ---------------------------------------------------------------------------
# 3) 趋势（时间桶聚合）
# ---------------------------------------------------------------------------
def resolve_step_days(range_days: int, bucket: str, max_points: int) -> tuple[int, str]:
    """解析实际步长与生效桶模式（保证点数 <= ``max_points`` 硬上限）。

    Returns:
        ``(step_days, effective_bucket)``：

        - ``bucket="auto"`` → 步长 = ceil(range_days / max_points)；
        - ``bucket="day"`` 且区间未超上限 → 步长 1 天（``"day"``）；
        - ``bucket="day"`` 但区间会超上限 → **自动收紧**为等间隔（``"auto"``），
          响应里如实返回生效模式（不静默撒谎）。
    """
    limit = max(1, int(max_points))
    needed = max(1, -(-int(range_days) // limit))  # ceil 除法
    if bucket == BUCKET_AUTO:
        return needed, BUCKET_AUTO
    if int(range_days) <= limit:
        return 1, BUCKET_DAY
    return needed, BUCKET_AUTO


def aggregate_trend(
    snapshots: list[dict[str, Any]],
    *,
    metric: str,
    range_days: int,
    step_days: int,
    now: datetime,
) -> dict[str, Any]:
    """把快照聚合成趋势点：``[{bucket, value}]``（空桶 → ``None``，不插值）。

    - 桶边界按本地自然日起算（``range_days`` 含今天）；
    - 同一时间桶内**每篇笔记取桶内最新非空值**（残缺快照不覆盖完整快照），
      再把有值的笔记值**求和**；
    - 桶内无任何值 → ``None``（前端断线展示，禁止补 0）。

    Returns:
        ``{points, notes_with_data, points_with_data, step_days, range_days}``。
    """
    start = range_start_local(now, range_days)
    step = max(1, int(step_days))
    n_buckets = max(1, -(-int(range_days) // step))  # ceil

    labels: list[str] = []
    for index in range(n_buckets):
        day = start.date() + timedelta(days=index * step)
        labels.append(day.isoformat())

    # (bucket_idx, note_id) -> (captured_at, value)：桶内取最新
    best: dict[tuple[int, str], tuple[datetime, int]] = {}
    notes_with_data: set[str] = set()

    for row in snapshots:
        value = row.get(metric)
        if value is None:
            continue
        captured = as_datetime(row.get("captured_at"))
        if captured is None:
            continue
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        if captured < start or captured > now:
            continue

        note_id = str(row.get("note_id") or "")
        if not note_id:
            continue
        notes_with_data.add(note_id)

        elapsed = (captured - start).total_seconds()
        index = int(elapsed // (_SECONDS_PER_DAY * step))
        index = min(max(index, 0), n_buckets - 1)
        key = (index, note_id)
        previous = best.get(key)
        if previous is None or captured >= previous[0]:
            best[key] = (captured, int(value))

    sums = [0] * n_buckets
    counts = [0] * n_buckets
    for (index, _note_id), (_captured, value) in best.items():
        sums[index] += value
        counts[index] += 1

    points: list[dict[str, Any]] = [
        {"bucket": labels[index], "value": sums[index] if counts[index] else None}
        for index in range(n_buckets)
    ]
    return {
        "points": points,
        "notes_with_data": len(notes_with_data),
        "points_with_data": sum(1 for count in counts if count),
        "step_days": step,
        "range_days": int(range_days),
    }


# ---------------------------------------------------------------------------
# 4) 排行
# ---------------------------------------------------------------------------
def rank_items(
    latest_by_note: dict[str, dict[str, int | None]],
    notes_meta: list[dict[str, Any]],
    *,
    metric: str,
    limit: int,
) -> list[dict[str, Any]]:
    """按指标最新非空值降序排行（缺失值排最后；tie 用发布时间降序兜底）。

    Args:
        latest_by_note: :func:`latest_values_per_note` 的产物。
        notes_meta: ``[{note_id, title, published_at}]``（published 笔记清单）。
        metric: 六指标之一。
        limit: 返回条数上限（>=1）。

    Returns:
        ``[{note_id, title, value, published_at}]``（value 可为 ``None``）。
    """
    decorated: list[dict[str, Any]] = []
    for meta in notes_meta:
        note_id = str(meta.get("note_id") or "")
        if not note_id:
            continue
        latest = latest_by_note.get(note_id) or {}
        value = latest.get(metric)
        decorated.append(
            {
                "note_id": note_id,
                "title": meta.get("title"),
                "value": int(value) if value is not None else None,
                "published_at": meta.get("published_at"),
            }
        )

    def sort_key(item: dict[str, Any]) -> tuple[int, int, float]:
        value = item["value"]
        ts = _ts(item.get("published_at"))
        return (
            0 if value is not None else 1,          # 有值在前
            -(value if value is not None else 0),   # 值大在前
            -(ts if ts is not None else -1.0),      # 发布晚在前（None 垫底）
        )

    decorated.sort(key=sort_key)
    return decorated[: max(1, int(limit))]


__all__ = [
    "BUCKET_AUTO",
    "BUCKET_DAY",
    "BUCKET_VALUES",
    "MAX_RANGE_DAYS",
    "MIN_RANGE_DAYS",
    "aggregate_trend",
    "as_datetime",
    "latest_values_per_note",
    "range_start_local",
    "rank_items",
    "rates_from_sums",
    "resolve_step_days",
    "sum_funnel",
]
