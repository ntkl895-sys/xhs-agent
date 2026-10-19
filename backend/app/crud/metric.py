"""note_metrics 的 CRUD（第 15 步 · 数据回填）。

职责与边界：

- ``create_snapshot``：唯一落库入口；命中 ``uq_metric_snapshot`` 唯一约束
  （同笔记同 ``captured_at`` 只允许一条）时，由覆盖的 ``_commit`` 翻译成
  409 ``conflict``，避免冒泡成 500；
- ``list_by_note``：按 ``captured_at`` 倒序 + ``id`` 兜底排序，**顺序确定**
  （不依赖无序 SELECT 的行序 —— 第 12 步的教训）；
- 快照**不做更新、不做物理删除**（随笔记 ``ON DELETE CASCADE`` 清理）；
  也不做「归档」（本表没有 ``status`` 列）。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import conflict
from app.crud.base import CRUDBase
from app.models.metric import NoteMetric

#: 快照唯一约束名（`0001` 迁移建表时定义，勿改）
UNIQUE_CONSTRAINT_NAME = "uq_metric_snapshot"


def _is_snapshot_conflict(exc: IntegrityError) -> bool:
    """判断唯一冲突是否来自 ``note_metrics`` 的 (note_id, captured_at) 约束。"""
    text = str(getattr(exc, "orig", exc))
    return UNIQUE_CONSTRAINT_NAME in text or ("note_id" in text and "captured_at" in text)


class CRUDMetric(CRUDBase[NoteMetric]):
    """数据快照 CRUD（只增 + 只读；无更新 / 无删除）。"""

    def __init__(self) -> None:
        super().__init__(NoteMetric)

    # 覆盖基类：把唯一冲突翻译成业务错误，避免冒泡成 500
    def _commit(self, db: Session) -> None:
        try:
            db.commit()
        except IntegrityError as exc:  # noqa: PERF203 - 只此一处
            db.rollback()
            if _is_snapshot_conflict(exc):
                raise conflict(
                    "该笔记在此时间点已有一条快照（note_id + captured_at 唯一）。"
                    "请调整采集时刻后重试。",
                    details={"constraint": UNIQUE_CONSTRAINT_NAME},
                ) from exc
            raise

    # ------------------------------------------------------------------ 写入
    def create_snapshot(self, db: Session, data: dict[str, Any]) -> NoteMetric:
        """落库一条快照。

        调用方（路由层）负责：笔记存在性 404 校验、``screenshot_path`` 的
        服务端受控赋值（客户端不可传入）。本方法只做「插入 + 冲突翻译」。
        """
        return self.create(db, data)

    # ------------------------------------------------------------------ 查询
    def list_by_note(self, db: Session, note_id: uuid.UUID) -> list[NoteMetric]:
        """按采集时刻倒序返回某笔记的全部快照（``id`` 兜底保证顺序稳定）。"""
        stmt = (
            select(NoteMetric)
            .where(NoteMetric.note_id == note_id)
            .order_by(NoteMetric.captured_at.desc(), NoteMetric.id.desc())
        )
        return list(db.execute(stmt).scalars().all())


crud_metric = CRUDMetric()

__all__ = ["CRUDMetric", "crud_metric", "UNIQUE_CONSTRAINT_NAME"]
