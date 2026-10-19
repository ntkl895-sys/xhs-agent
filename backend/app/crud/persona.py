"""personas 的 CRUD。

职责：
- 列表过滤：状态 / 归属账号（全部 / 只看公共 / 指定账号）/ 名称模糊；
- 关联展示：LEFT JOIN ``accounts`` 取 ``account_name``（不加数据库列）；
- 业务规则：归属账号必须存在；同一归属范围（``account_id`` 相同，NULL 视为
  「公共」一组）内默认人设唯一。
"""

from __future__ import annotations

import uuid
from typing import Any, Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.account import Account
from app.models.enums import PersonaStatus
from app.models.persona import Persona

# account_id 查询参数的三种语义
SCOPE_ALL = "all"
SCOPE_PUBLIC = "public"
SCOPE_ACCOUNT = "account"

AccountScope = tuple[str, uuid.UUID | None]


class CRUDPersona(CRUDBase[Persona]):
    """人设 CRUD。"""

    archived_status = PersonaStatus.ARCHIVED

    def __init__(self) -> None:
        super().__init__(Persona)

    # ------------------------------------------------------------------ 过滤
    def build_criteria(
        self,
        *,
        status: PersonaStatus | None,
        scope: AccountScope,
        q: str | None,
    ) -> Sequence[Any]:
        """把查询参数翻译成 SQLAlchemy 过滤条件。"""
        criteria: list[Any] = []
        if status is not None:
            criteria.append(Persona.status == status)
        mode, account_id = scope
        if mode == SCOPE_PUBLIC:
            criteria.append(Persona.account_id.is_(None))
        elif mode == SCOPE_ACCOUNT and account_id is not None:
            criteria.append(Persona.account_id == account_id)
        if q:
            criteria.append(Persona.name.ilike(f"%{q.strip()}%"))
        return tuple(criteria)

    def get_multi_with_account(
        self,
        db: Session,
        *,
        criteria: Sequence[Any] = (),
        offset: int = 0,
        limit: int = 20,
    ) -> list[tuple[Persona, str | None]]:
        """分页查询并一并取出归属账号名（LEFT JOIN，公共人设为 None）。"""
        stmt = (
            select(Persona, Account.name)
            .outerjoin(Account, Persona.account_id == Account.id)
            .order_by(Persona.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        stmt = self._apply_criteria(stmt, criteria)
        return [(row[0], row[1]) for row in db.execute(stmt).all()]

    def account_name_of(self, db: Session, persona: Persona) -> str | None:
        """取某条人设的归属账号名（新建 / 更新后的单对象响应用）。"""
        if persona.account_id is None:
            return None
        account = db.get(Account, persona.account_id)
        return account.name if account is not None else None

    def account_exists(self, db: Session, account_id: uuid.UUID) -> bool:
        """归属账号是否存在。"""
        return db.get(Account, account_id) is not None

    # ------------------------------------------------------------------ 写入
    def _enforce_single_default(self, db: Session, persona: Persona) -> None:
        """把同一归属范围内的其它人设 ``is_default`` 置为 False。

        必须在调用前 ``flush``，保证本条的 ``is_default`` / ``account_id`` 已落到事务里。
        """
        if not persona.is_default:
            return
        stmt = update(Persona).where(
            Persona.is_default.is_(True),
            Persona.id != persona.id,
        )
        if persona.account_id is None:
            stmt = stmt.where(Persona.account_id.is_(None))
        else:
            stmt = stmt.where(Persona.account_id == persona.account_id)
        stmt = stmt.values(is_default=False).execution_options(synchronize_session=False)
        db.execute(stmt)

    def create(self, db: Session, data: dict[str, Any]) -> Persona:
        """新建人设，并在同一事务内保证「同范围默认人设唯一」。"""
        obj = Persona(**data)
        db.add(obj)
        db.flush()  # 需要 id / account_id 已确定
        self._enforce_single_default(db, obj)
        self._commit(db)
        db.refresh(obj)
        return obj

    def update(self, db: Session, db_obj: Persona, data: dict[str, Any]) -> Persona:
        """部分更新，并在同一事务内维持「同范围默认人设唯一」。

        这里不直接复用基类的 archive 分支：本实体需要在提交前多插一步
        「清理同范围其它默认」，因此统一在末尾一次性提交，保证原子性。
        """
        for field, value in data.items():
            setattr(db_obj, field, value)
        db.flush()
        self._enforce_single_default(db, db_obj)
        self._commit(db)
        db.refresh(db_obj)
        return db_obj


crud_persona = CRUDPersona()
