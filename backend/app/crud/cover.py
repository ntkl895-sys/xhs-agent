"""covers 的 CRUD（第 13 步 · 封面模块）。

职责：

- ``create_with_defaults``：新建候选，显式补齐 ``width=1242`` / ``height=1656`` /
  ``is_selected=false`` / ``version=1``（与前端 ``lib/cover/constants.ts`` 对齐）；
- ``list_by_note``：按 ``created_at`` 正序返回某笔记的全部候选（``id`` 兜底，
  保证顺序确定——**不依赖无序 SELECT 的行序**）；
- ``update_fields``：PATCH 普通字段；
- ``select_one``：**单事务**互斥选中 —— 先把该笔记下全部候选置 ``is_selected=false``，
  再把目标置 ``true``，一起 commit；任一失败 rollback。

约定：封面候选**不做归档、不提供物理删除**（随笔记 ``ON DELETE CASCADE`` 清理）。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.note import Cover

#: 封面规格（与 covers 表 server_default 及前端常量三处一致）
COVER_WIDTH = 1242
COVER_HEIGHT = 1656


class CRUDCover(CRUDBase[Cover]):
    """封面候选 CRUD。"""

    def __init__(self) -> None:
        super().__init__(Cover)

    # ------------------------------------------------------------------ 写入
    def create_with_defaults(self, db: Session, data: dict[str, Any]) -> Cover:
        """新建候选：显式补齐默认列（宽高 / 未选中 / version=1）。"""
        payload: dict[str, Any] = {
            **data,
            "width": COVER_WIDTH,
            "height": COVER_HEIGHT,
            "is_selected": False,
            "version": 1,
        }
        return self.create(db, payload)

    def update_fields(self, db: Session, db_obj: Cover, data: dict[str, Any]) -> Cover:
        """部分更新普通字段（含显式取消选中；互斥选中请走 :meth:`select_one`）。"""
        if not data:
            return db_obj
        for field, value in data.items():
            setattr(db_obj, field, value)
        self._commit(db)
        return self._refresh(db, db_obj)

    def update_with_selection(
        self,
        db: Session,
        db_obj: Cover,
        data: dict[str, Any],
        *,
        select: bool | None,
    ) -> Cover:
        """**单事务**：字段更新 + 互斥选中 / 取消选中 一起提交（第 20 步收口）。

        第 13 步的 PATCH 在「同时更新普通字段与 is_selected」时分为两次提交
        （``update_fields`` 一次、``select_one`` 一次），进程中断可能留下
        「字段已改、选中未切换」的半成品。本方法把两者合并为一次提交：

        - ``select=True``：同笔记其余候选全部置 ``false`` → 目标置 ``true``
          （与 :meth:`select_one` 的互斥语义逐字一致）；
        - ``select=False``：目标显式取消选中（允许「没有选中封面」的中间态）；
        - ``select=None``：仅更新字段（等价于原 ``update_fields`` 路径）；
        - 任一失败整体 rollback，不留半成品。

        Returns:
            提交并刷新后的 ``Cover``。
        """
        try:
            for field, value in data.items():
                setattr(db_obj, field, value)
            if select is True:
                db.execute(
                    sa_update(Cover)
                    .where(Cover.note_id == db_obj.note_id)
                    .values(is_selected=False)
                    .execution_options(synchronize_session=False)
                )
                db.execute(
                    sa_update(Cover)
                    .where(Cover.id == db_obj.id)
                    .values(is_selected=True)
                    .execution_options(synchronize_session=False)
                )
            elif select is False:
                db_obj.is_selected = False
            db.commit()
        except Exception:
            db.rollback()
            raise
        return self._refresh(db, db_obj)

    def select_one(
        self,
        db: Session,
        note_id: uuid.UUID,
        cover_id: uuid.UUID,
    ) -> Cover | None:
        """**单事务**互斥选中：同笔记全部置 false → 目标置 true → commit。

        Returns:
            选中后的 ``Cover``；候选不存在或不属于该笔记时返回 ``None``
            （由调用方翻译为 404）。

        Raises:
            Exception: 数据库错误原样抛出（已 rollback，不留半成品状态）。
        """
        cover = db.get(Cover, cover_id)
        if cover is None or cover.note_id != note_id:
            return None
        try:
            db.execute(
                sa_update(Cover)
                .where(Cover.note_id == note_id)
                .values(is_selected=False)
                .execution_options(synchronize_session=False)
            )
            db.execute(
                sa_update(Cover)
                .where(Cover.id == cover_id)
                .values(is_selected=True)
                .execution_options(synchronize_session=False)
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        return self._refresh(db, cover)

    # ------------------------------------------------------------------ 查询
    def list_by_note(self, db: Session, note_id: uuid.UUID) -> list[Cover]:
        """按创建时间正序返回某笔记的全部候选。"""
        stmt = (
            select(Cover)
            .where(Cover.note_id == note_id)
            .order_by(Cover.created_at.asc(), Cover.id.asc())
        )
        return list(db.execute(stmt).scalars().all())


crud_cover = CRUDCover()

__all__ = ["CRUDCover", "crud_cover", "COVER_WIDTH", "COVER_HEIGHT"]
