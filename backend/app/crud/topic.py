"""topics 的 CRUD（第 10 步）。

职责：

- 列表过滤：status / account_id / persona_id / hotspot_id（``source_hotspot_ids`` JSONB 包含匹配）
  / 标题模糊 / generated_at 日期；
- 关联展示：LEFT JOIN ``personas`` / ``accounts`` 取人名与账号名（**不新增数据库列**）；
- **单事务批量插入**：persist 节点一次写入 N 条选题，失败整体回滚，不留半成品；
- 判重辅助：给该人设取「未 discarded」选题的**归一化标题集合**与**已覆盖的热点 id 集合**。
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.account import Account
from app.models.enums import TopicStatus
from app.models.persona import Persona
from app.models.topic import Topic

#: 空白归一（含全角空格 U+3000）
_WHITESPACE = re.compile(r"\s+")

#: 允许的排序字段
SORT_SCORE = "score"
SORT_GENERATED_AT = "generated_at"


def normalize_title(value: str | None) -> str:
    """标题归一化：strip + 转小写 + 全角空格转半角 + 内部空白折叠。

    用于「同一人设内不重复」的判重（语义去重留待后续 RAG 步）。
    """
    text = (value or "").strip().lower().replace("\u3000", " ")
    return _WHITESPACE.sub(" ", text)


class CRUDTopic(CRUDBase[Topic]):
    """选题 CRUD。"""

    archived_status = TopicStatus.DISCARDED

    def __init__(self) -> None:
        super().__init__(Topic)

    # ------------------------------------------------------------------ 过滤
    def build_criteria(
        self,
        *,
        status: TopicStatus | None = None,
        account_id: uuid.UUID | None = None,
        persona_id: uuid.UUID | None = None,
        hotspot_id: uuid.UUID | None = None,
        q: str | None = None,
        generated_date: date | None = None,
    ) -> Sequence[Any]:
        """把查询参数翻译成 SQLAlchemy 过滤条件。"""
        criteria: list[Any] = []
        if status is not None:
            criteria.append(Topic.status == status)
        if account_id is not None:
            criteria.append(Topic.account_id == account_id)
        if persona_id is not None:
            criteria.append(Topic.persona_id == persona_id)
        if hotspot_id is not None:
            # JSONB 数组包含匹配：source_hotspot_ids @> '["<uuid>"]'
            criteria.append(Topic.source_hotspot_ids.contains([str(hotspot_id)]))
        if q:
            criteria.append(Topic.title.ilike(f"%{q.strip()}%"))
        if generated_date is not None:
            # generated_at 落库存 UTC；按「UTC 日历日」取整日区间（口径写入 docs）
            start = datetime.combine(generated_date, time.min, tzinfo=timezone.utc)
            criteria.append(Topic.generated_at >= start)
            criteria.append(Topic.generated_at < start + timedelta(days=1))
        return tuple(criteria)

    def get_multi_with_names(
        self,
        db: Session,
        *,
        criteria: Sequence[Any] = (),
        offset: int = 0,
        limit: int = 20,
        sort: str = SORT_SCORE,
        order: str = "desc",
    ) -> list[tuple[Topic, str | None, str | None]]:
        """分页查询并带出人设名 / 账号名（LEFT JOIN，可为 None）。"""
        stmt: Select = (
            select(Topic, Persona.name, Account.name)
            .outerjoin(Persona, Topic.persona_id == Persona.id)
            .outerjoin(Account, Topic.account_id == Account.id)
        )
        stmt = self._apply_criteria(stmt, criteria)
        column = Topic.generated_at if sort == SORT_GENERATED_AT else Topic.score
        direction = column.asc().nulls_last() if order == "asc" else column.desc().nulls_last()
        stmt = (
            stmt.order_by(direction, Topic.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return [(row[0], row[1], row[2]) for row in db.execute(stmt).all()]

    # ------------------------------------------------------------------ 写入
    def insert_many(self, db: Session, rows: list[dict[str, Any]]) -> list[Topic]:
        """**单事务**批量插入选题；任一条失败整体回滚。

        Returns:
            已刷新（含服务器默认值）的 ORM 对象列表。
        """
        objs = [Topic(**row) for row in rows]
        try:
            db.add_all(objs)
            db.flush()  # 通过 RETURNING 取回 gen_random_uuid() 主键
            db.commit()
        except Exception:
            db.rollback()
            raise
        for obj in objs:
            db.refresh(obj)
        return objs

    # ------------------------------------------------------------------ 判重
    def existing_normalized_titles(self, db: Session, persona_id: uuid.UUID) -> set[str]:
        """该人设**未 discarded** 选题的归一化标题集合。"""
        stmt = select(Topic.title).where(
            Topic.persona_id == persona_id,
            Topic.status != TopicStatus.DISCARDED,
        )
        return {normalize_title(title) for title in db.execute(stmt).scalars().all() if title}

    def covered_hotspot_ids(self, db: Session, persona_id: uuid.UUID) -> set[str]:
        """该人设**未 discarded** 选题已覆盖的热点 id 集合（来自 ``source_hotspot_ids``）。"""
        stmt = select(Topic.source_hotspot_ids).where(
            Topic.persona_id == persona_id,
            Topic.status != TopicStatus.DISCARDED,
        )
        covered: set[str] = set()
        for value in db.execute(stmt).scalars().all():
            if isinstance(value, list):
                covered.update(str(item) for item in value)
        return covered


crud_topic = CRUDTopic()

__all__ = [
    "CRUDTopic",
    "crud_topic",
    "normalize_title",
    "SORT_SCORE",
    "SORT_GENERATED_AT",
]
