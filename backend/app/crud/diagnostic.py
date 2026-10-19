"""diagnostics 的 CRUD（第 16 步 · 单篇诊断）。

职责与边界：

- ``create_diagnostic``：唯一落库入口（**单事务**；由上层在单个 ``with SessionLocal()``
  内调用，失败整体回滚不留半截记录）；命中不了任何唯一约束——诊断历史只增不改；
- ``list_by_note``：按 ``created_at`` 倒序 + ``id`` 兜底排序，**顺序确定**
  （不依赖无序 SELECT 的行序 —— 第 12 步的教训）；
- ``get_by_id``：按主键取单条（详情接口用）；
- **不做更新、不做删除、不做归档**：诊断结论一经生成不可修改，随笔记
  ``ON DELETE CASCADE`` 清理；接口层也**不提供** PUT / PATCH / DELETE。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.diagnostic import Diagnostic


class CRUDDiagnostic(CRUDBase[Diagnostic]):
    """单篇诊断结论 CRUD（只增 + 只读；无更新 / 无删除）。"""

    def __init__(self) -> None:
        super().__init__(Diagnostic)

    # ------------------------------------------------------------------ 写入
    def create_diagnostic(self, db: Session, data: dict[str, Any]) -> Diagnostic:
        """落库一条诊断结论。

        调用方（图内 ``persist`` 节点）负责：笔记存在性校验、``based_on_metric_id``
        受控赋值（只取服务端算出的「最新快照 id」，客户端不可传入）。本方法只做
        「单事务插入 + 刷新」。
        """
        return self.create(db, data)

    # ------------------------------------------------------------------ 查询
    def list_by_note(self, db: Session, note_id: uuid.UUID) -> list[Diagnostic]:
        """按生成时间倒序返回某笔记的全部诊断（``id`` 兜底保证顺序稳定）。"""
        stmt = (
            select(Diagnostic)
            .where(Diagnostic.note_id == note_id)
            .order_by(Diagnostic.created_at.desc(), Diagnostic.id.desc())
        )
        return list(db.execute(stmt).scalars().all())

    def get_by_id(self, db: Session, diagnostic_id: uuid.UUID) -> Diagnostic | None:
        """按主键取单条，不存在返回 ``None``。"""
        return self.get(db, diagnostic_id)


crud_diagnostic = CRUDDiagnostic()

__all__ = ["CRUDDiagnostic", "crud_diagnostic"]
