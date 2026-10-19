"""运营看板服务层（第 17 步）——取数 + 组装（ORM 在此层，路由层零 ORM）。

职责：

- ``build_summary``：计数（账号 / 笔记分状态 / 在写选题 / 周期内新发布）
  + 全账号最新漏斗（对每篇 published 笔记取最新非空值 → 合计 → 六率）；
- ``build_trend``：指定指标的趋势点（按天 / 等间隔分桶；全账号求和、
  单笔记直取；空桶 None 不插值）；
- ``build_ranking``：published 笔记按指标最新值排行（缺失排最后）。

红线：**只读**——本模块不做任何写入；不返回 ``screenshot_path`` / ``evidence``
原图等敏感字段（响应 schema 逐字段白名单）；不请求任何外部域名。

**唯一出口**：``load_snapshots``（快照取数管道）同时被看板与周报
（``app.agent.weekly``）复用——**绝不重复实现第二套取数口径**。

全部算术在 :mod:`app.services.dashboard.aggregator` 的纯函数里完成（可单测）。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import not_found
from app.core.timeutil import now_local
from app.models.account import Account
from app.models.enums import NoteStatus, TopicStatus
from app.models.metric import NoteMetric
from app.models.note import Note
from app.models.topic import Topic
from app.services.dashboard.aggregator import (
    aggregate_trend,
    latest_values_per_note,
    rank_items,
    range_start_local,
    resolve_step_days,
    sum_funnel,
)

# ---------------------------------------------------------------------------
# 取数（内部）
# ---------------------------------------------------------------------------
def _snapshot_dict(row: NoteMetric) -> dict[str, Any]:
    """ORM 快照行 → 普通 dict（时间统一 ISO 字符串，与检查点序列化口径一致）。

    ⚠️ 刻意**不带出** ``evidence`` / ``screenshot_path``：看板只做数字聚合，
    不需要（也不应）经接口暴露这些明细。
    """
    return {
        "id": str(row.id),
        "note_id": str(row.note_id),
        "captured_at": row.captured_at.isoformat() if row.captured_at else None,
        "views": row.views,
        "likes": row.likes,
        "collects": row.collects,
        "comments": row.comments,
        "shares": row.shares,
        "new_followers": row.new_followers,
    }


def load_snapshots(db: Session, note_ids: list[uuid.UUID]) -> list[dict[str, Any]]:
    """按「note_id 升序 + captured_at 升序 + id 升序」取快照（顺序确定）。"""
    if not note_ids:
        return []
    rows = (
        db.execute(
            select(NoteMetric)
            .where(NoteMetric.note_id.in_(note_ids))
            .order_by(
                NoteMetric.note_id.asc(),
                NoteMetric.captured_at.asc(),
                NoteMetric.id.asc(),
            )
        )
        .scalars()
        .all()
    )
    return [_snapshot_dict(row) for row in rows]


def _published_note_ids(db: Session) -> list[uuid.UUID]:
    """全部已发布笔记 id（顺序确定：created_at 降序 + id 降序）。"""
    rows = (
        db.execute(
            select(Note.id)
            .where(Note.status == NoteStatus.PUBLISHED)
            .order_by(Note.created_at.desc(), Note.id.desc())
        )
        .all()
    )
    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# 1) summary
# ---------------------------------------------------------------------------
def build_summary(db: Session, *, range_days: int) -> dict[str, Any]:
    """运营看板汇总：计数 + 全账号最新漏斗。

    Args:
        range_days: 「周期内新发布数」的统计窗口（近 N 个自然日，含今天）。

    Returns:
        ``{counts, funnel, range_days, generated_at}``（骨架；schema 见
        ``app.schemas.dashboard.DashboardSummaryOut``）。
    """
    now = now_local()
    start = range_start_local(now, range_days)

    # ---- 计数 ----
    accounts_total = int(
        db.execute(select(func.count()).select_from(Account)).scalar() or 0
    )

    status_rows = db.execute(
        select(Note.status, func.count())
        .where(Note.status != NoteStatus.ARCHIVED)
        .group_by(Note.status)
    ).all()
    status_counts: dict[str, int] = {}
    for status_value, count in status_rows:
        key = status_value.value if hasattr(status_value, "value") else str(status_value)
        status_counts[key] = int(count)

    notes_published = status_counts.get(NoteStatus.PUBLISHED.value, 0)
    notes_total = sum(status_counts.values())

    published_in_range = int(
        db.execute(
            select(func.count())
            .select_from(Note)
            .where(
                Note.status == NoteStatus.PUBLISHED,
                Note.published_at.is_not(None),
                Note.published_at >= start,
                Note.published_at <= now,
            )
        ).scalar()
        or 0
    )

    topics_writing = int(
        db.execute(
            select(func.count())
            .select_from(Topic)
            .where(Topic.status == TopicStatus.WRITING)
        ).scalar()
        or 0
    )

    # ---- 全账号最新漏斗（只统计 published 笔记）----
    published_ids = _published_note_ids(db)
    snapshots = load_snapshots(db, published_ids)
    latest_by_note = latest_values_per_note(snapshots)
    funnel = sum_funnel(latest_by_note)
    funnel["published_notes"] = len(published_ids)

    return {
        "counts": {
            "accounts": accounts_total,
            "notes_total": notes_total,
            "notes_published": notes_published,
            "notes_ready": status_counts.get(NoteStatus.READY.value, 0),
            "notes_draft": status_counts.get(NoteStatus.DRAFT.value, 0),
            "topics_writing": topics_writing,
            "notes_published_in_range": published_in_range,
        },
        "funnel": funnel,
        "range_days": int(range_days),
        "generated_at": now,
    }


# ---------------------------------------------------------------------------
# 2) trend
# ---------------------------------------------------------------------------
def build_trend(
    db: Session,
    *,
    metric: str,
    range_days: int,
    note_id: uuid.UUID | None,
    bucket: str,
    max_points: int,
) -> dict[str, Any]:
    """指定指标的趋势点（时间桶聚合）。

    - ``note_id`` 为空 = 全账号（只统计 **published** 笔记）；
    - 指定 ``note_id`` 时校验存在性（不存在 → 404），且不限笔记状态
      （单笔记视图以该笔记自身数据为准）；
    - 空桶给 ``None``（禁止补 0 / 插值）；步长按点数上限自动推导。
    """
    now = now_local()

    if note_id is not None:
        note = db.get(Note, note_id)
        if note is None:
            raise not_found("笔记不存在", details={"note_id": str(note_id)})
        note_ids = [note_id]
    else:
        note_ids = _published_note_ids(db)

    snapshots = load_snapshots(db, note_ids)
    step_days, effective_bucket = resolve_step_days(range_days, bucket, max_points)
    result = aggregate_trend(
        snapshots,
        metric=metric,
        range_days=range_days,
        step_days=step_days,
        now=now,
    )

    return {
        "metric": metric,
        "bucket": effective_bucket,
        "points": result["points"],
        "coverage": {
            "notes": len(note_ids),
            "notes_with_data": result["notes_with_data"],
            "points_with_data": result["points_with_data"],
            "points_total": len(result["points"]),
            "step_days": result["step_days"],
            "range_days": result["range_days"],
        },
    }


# ---------------------------------------------------------------------------
# 3) ranking
# ---------------------------------------------------------------------------
def build_ranking(
    db: Session, *, metric: str, limit: int
) -> dict[str, Any]:
    """published 笔记按指标最新非空值降序排行（缺失值排最后、不臆造）。"""
    rows = (
        db.execute(
            select(Note.id, Note.title, Note.published_at)
            .where(Note.status == NoteStatus.PUBLISHED)
            .order_by(Note.created_at.desc(), Note.id.desc())
        )
        .all()
    )
    notes_meta = [
        {
            "note_id": str(row[0]),
            "title": row[1],
            "published_at": row[2].isoformat() if row[2] else None,
        }
        for row in rows
    ]

    note_ids = [uuid.UUID(str(row[0])) for row in rows]
    snapshots = load_snapshots(db, note_ids)
    latest_by_note = latest_values_per_note(snapshots)
    items = rank_items(latest_by_note, notes_meta, metric=metric, limit=limit)

    return {"metric": metric, "items": items}


__all__ = ["build_ranking", "build_summary", "build_trend", "load_snapshots"]
