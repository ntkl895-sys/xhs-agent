"""轻量泛型 CRUD 基类（第 8 步范式，后续模块复用）。

设计取舍：
- **不引入** 额外抽象框架（如 fastapi-crudrouter）——只有 2 个实体，泛型基类足够；
- 每个方法都接收 ``Session``，由 FastAPI 依赖注入，便于在路由里组合多步操作；
- ``archive()`` 是**软归档**原语：置 ``status=archived``，全项目**不做物理 DELETE**；
- 唯一约束冲突等数据库错误统一由子类覆盖 ``_commit()`` 处理（见 ``crud/account.py``）。
"""

from __future__ import annotations

import uuid
from typing import Any, Generic, Sequence, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.db.base import Base

ModelType = TypeVar("ModelType", bound=Base)


class CRUDBase(Generic[ModelType]):
    """通用 CRUD：get / get_multi / count / create / update / archive。"""

    #: 子类覆盖为各自的「归档」状态值（枚举成员或字符串）
    archived_status: Any = "archived"

    def __init__(self, model: type[ModelType]) -> None:
        self.model = model

    # ------------------------------------------------------------------ 查询
    def get(self, db: Session, obj_id: uuid.UUID) -> ModelType | None:
        """按主键取单个对象，不存在返回 None。"""
        return db.get(self.model, obj_id)

    def _apply_criteria(self, stmt: Select, criteria: Sequence[Any]) -> Select:
        return stmt.where(*criteria) if criteria else stmt

    def count(self, db: Session, *criteria: Any) -> int:
        """满足条件（且未被删除）的条数。"""
        stmt = self._apply_criteria(select(func.count()).select_from(self.model), criteria)
        return int(db.execute(stmt).scalar_one())

    def get_multi(
        self,
        db: Session,
        *,
        criteria: Sequence[Any] = (),
        offset: int = 0,
        limit: int = 20,
    ) -> list[ModelType]:
        """分页查询，默认按 ``created_at`` 倒序。"""
        stmt = self._apply_criteria(select(self.model), criteria)
        stmt = (
            stmt.order_by(self.model.created_at.desc())  # type: ignore[attr-defined]
            .offset(offset)
            .limit(limit)
        )
        return list(db.execute(stmt).scalars().all())

    # ------------------------------------------------------------------ 写入
    def _commit(self, db: Session) -> None:
        """提交事务。子类可覆盖以把数据库异常翻译成业务错误。"""
        db.commit()

    def _refresh(self, db: Session, obj: ModelType) -> ModelType:
        db.refresh(obj)
        return obj

    def create(self, db: Session, data: dict[str, Any]) -> ModelType:
        """新建对象（``data`` 需已通过 schema 校验）。"""
        obj = self.model(**data)
        db.add(obj)
        self._commit(db)
        return self._refresh(db, obj)

    def update(self, db: Session, db_obj: ModelType, data: dict[str, Any]) -> ModelType:
        """部分更新。

        特殊处理 ``status``：等于本类 ``archived_status`` 时走 :meth:`archive`，
        保证「归档」这一动作始终由同一个原语完成。
        """
        payload = dict(data)
        status_value = payload.pop("status", None)

        for field, value in payload.items():
            setattr(db_obj, field, value)

        if status_value is not None:
            if status_value == self.archived_status:
                return self.archive(db, db_obj, status_value)
            setattr(db_obj, "status", status_value)

        self._commit(db)
        return self._refresh(db, db_obj)

    def archive(self, db: Session, db_obj: ModelType, archived_value: Any = None) -> ModelType:
        """软归档：置 ``status`` 为归档值，不删除任何数据。

        调用方若已在同一 session 里改了其它字段，会与本操作一起提交（同一事务）。
        """
        setattr(db_obj, "status", archived_value if archived_value is not None else self.archived_status)
        self._commit(db)
        return self._refresh(db, db_obj)
