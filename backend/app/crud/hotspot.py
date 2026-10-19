"""hotspots 的 CRUD（第 9 步）。

核心是**幂等 upsert**：以 ``content_hash``（``source + source_item_id + hot_date``
归一化后哈希，对应唯一约束 ``uq_hotspot_hash``）为冲突键，走 PostgreSQL
``INSERT ... ON CONFLICT (content_hash) DO UPDATE``。这样同一天多次采集**不会产生重复行**，
只更新热度 / 名次 / 抓取时间等易变字段。

区分「新增」与「更新」用 PostgreSQL 的系统列技巧：``RETURNING (xmax = 0) AS inserted``——
新插入的行 ``xmax`` 为 0，被更新的行不为 0。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import Select, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.timeutil import today_local
from app.crud.base import CRUDBase
from app.models.enums import HotspotStatus
from app.models.hotspot import Hotspot
from app.services.hotspot.schemas import HotspotItem


def normalize_identity(value: object) -> str:
    """把标识 / 标题归一化：去首尾空白、统一小写、压缩内部空白。"""
    return " ".join(str(value or "").strip().lower().split())


def compute_content_hash(
    source: str,
    source_item_id: str | None,
    title: str,
    hot_date: date,
) -> str:
    """计算跨批次去重用的 ``content_hash``。

    构成：``sha256(source | identity | hot_date)``（均为归一化后的文本）。

    ``identity`` 取 ``source_item_id``；**若上游未给 id，则回退用标题**——
    否则同一源当天所有「无 id」条目会因哈希相同而被错误地合并成一行。
    """
    identity = normalize_identity(source_item_id) or normalize_identity(title)
    payload = f"{normalize_identity(source)}|{identity}|{hot_date.isoformat()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CRUDHotspot(CRUDBase[Hotspot]):
    """热搜快照 CRUD（查询过滤 + 幂等 upsert）。"""

    def __init__(self) -> None:
        super().__init__(Hotspot)

    # ------------------------------------------------------------------ 过滤
    def build_criteria(
        self,
        *,
        source: str | None = None,
        status: HotspotStatus | None = None,
        q: str | None = None,
        hot_date: date | None = None,
        include_all_dates: bool = False,
    ) -> tuple[Any, ...]:
        """把查询参数翻译成 SQLAlchemy 过滤条件。

        ``include_all_dates=True``（接口传 ``hot_date=all``）时不按日期过滤；
        否则按 ``hot_date or 今天`` 过滤。
        """
        criteria: list[Any] = []
        if source:
            criteria.append(Hotspot.source == source.strip())
        if status is not None:
            criteria.append(Hotspot.status == status)
        if q:
            criteria.append(Hotspot.title.ilike(f"%{q.strip()}%"))
        if not include_all_dates:
            criteria.append(Hotspot.hot_date == (hot_date or today_local()))
        return tuple(criteria)

    def get_multi_sorted(
        self,
        db: Session,
        *,
        criteria: Sequence[Any] = (),
        sort: str = "hot_score",
        order: str = "desc",
        offset: int = 0,
        limit: int = 20,
    ) -> list[Hotspot]:
        """分页查询并按 ``sort`` 排序（NULL 值恒排最后，二级键 ``created_at`` 倒序）。"""
        column = Hotspot.fetched_at if sort == "fetched_at" else Hotspot.hot_score
        primary = column.desc() if order != "asc" else column.asc()
        stmt: Select = (
            select(Hotspot)
            .order_by(primary.nulls_last(), Hotspot.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        stmt = self._apply_criteria(stmt, criteria)
        return list(db.execute(stmt).scalars().all())

    def latest_fetched_at(self, db: Session) -> datetime | None:
        """最近一次落库时间（``/sources`` 的兜底信息）。"""
        return db.execute(select(func.max(Hotspot.fetched_at))).scalar()

    # ------------------------------------------------------------------ 状态标记
    def set_status(
        self, db: Session, hotspot_id: uuid.UUID, status: HotspotStatus
    ) -> Hotspot | None:
        """人工标记处理状态（第 11 步 ``PATCH /{hotspot_id}``）。

        只 ``setattr`` 一个 ``status`` 字段并提交，**不物理删除、不改其它字段、不联动 topics**。
        刻意**不走** :meth:`CRUDBase.update` —— 其 ``status`` 分支会命中 ``archived_status``
        （对热点语义不成立）。

        Returns:
            更新后的对象；不存在返回 ``None``（由路由层翻译成 404）。
        """
        hotspot = self.get(db, hotspot_id)
        if hotspot is None:
            return None
        setattr(hotspot, "status", status)
        db.commit()
        db.refresh(hotspot)
        return hotspot

    # ------------------------------------------------------------------ 落库
    def upsert_many(
        self,
        db: Session,
        items: Iterable[HotspotItem],
        *,
        hot_date: date | None = None,
    ) -> tuple[int, int]:
        """幂等批量 upsert，返回 ``(new, updated)``。

        - 同一批次内 ``content_hash`` 重复的条目**先去重**（PostgreSQL 不允许
          ``ON CONFLICT`` 在同一语句里二次命中同一行）；
        - 冲突时更新易变字段（热度 / 名次 / 抓取时间 / URL / raw_extra / 可选列），
          新行 ``status`` 恒为 ``new``。
        """
        day = hot_date or today_local()
        now = datetime.now(timezone.utc)

        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            content_hash = compute_content_hash(
                item.source, item.source_item_id, item.title, day
            )
            if content_hash in seen:
                continue
            seen.add(content_hash)
            rows.append(
                {
                    "source": item.source,
                    "source_item_id": item.source_item_id,
                    "title": item.title,
                    "url": item.url,
                    "mobile_url": item.mobile_url,
                    "hot_score": item.hot_score,
                    "rank_position": item.rank_position,
                    "cover_url": item.cover_url,
                    "summary": item.summary,
                    "author": item.author,
                    "raw_extra": item.raw_extra or {},
                    "hot_date": day,
                    "fetched_at": now,
                    "content_hash": content_hash,
                    "status": HotspotStatus.NEW,
                }
            )

        if not rows:
            return (0, 0)

        stmt = pg_insert(Hotspot).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Hotspot.content_hash],
            set_={
                "source_item_id": stmt.excluded.source_item_id,
                "hot_score": stmt.excluded.hot_score,
                "rank_position": stmt.excluded.rank_position,
                "fetched_at": stmt.excluded.fetched_at,
                "url": stmt.excluded.url,
                "mobile_url": stmt.excluded.mobile_url,
                "raw_extra": stmt.excluded.raw_extra,
                "cover_url": stmt.excluded.cover_url,
                "summary": stmt.excluded.summary,
                "author": stmt.excluded.author,
            },
        ).returning(literal_column("(xmax = 0)").label("inserted"))

        flags = [bool(row[0]) for row in db.execute(stmt).all()]
        new = sum(1 for flag in flags if flag)
        updated = len(flags) - new
        db.commit()
        return (new, updated)

    def delete_by_title_prefix(self, db: Session, prefix: str) -> int:
        """按标题前缀物理删除（**仅供自检脚本清理 [SELFTEST] 数据**）。"""
        rows = (
            db.execute(select(Hotspot).where(Hotspot.title.like(f"{prefix}%")))
            .scalars()
            .all()
        )
        for row in rows:
            db.delete(row)
        db.commit()
        return len(rows)

    def count_by_content_hashes(self, db: Session, hashes: Sequence[str]) -> int:
        """统计给定 content_hash 的现有行数（自检用）。"""
        if not hashes:
            return 0
        return int(
            db.execute(
                select(func.count()).select_from(Hotspot).where(Hotspot.content_hash.in_(list(hashes)))
            ).scalar_one()
        )


crud_hotspot = CRUDHotspot()


__all__ = ["CRUDHotspot", "compute_content_hash", "crud_hotspot", "normalize_identity"]
