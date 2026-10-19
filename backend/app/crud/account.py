"""accounts 的 CRUD。

职责：
- 构造列表过滤条件（status / 名称模糊）；
- 把「小红书号唯一」这类数据库唯一冲突翻译成 409（统一走 :meth:`_commit`）。
"""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import conflict
from app.crud.base import CRUDBase
from app.models.account import Account
from app.models.enums import AccountStatus


def _is_red_id_conflict(exc: IntegrityError) -> bool:
    """判断唯一冲突是否来自 ``accounts.xhs_red_id`` 的唯一约束。"""
    text = str(getattr(exc, "orig", exc))
    return "uq_accounts_xhs_red_id" in text or "xhs_red_id" in text


class CRUDAccount(CRUDBase[Account]):
    """账号 CRUD。"""

    archived_status = AccountStatus.ARCHIVED

    def __init__(self) -> None:
        super().__init__(Account)

    # 覆盖基类：把唯一冲突翻译成业务错误，避免冒泡成 500
    def _commit(self, db: Session) -> None:
        try:
            db.commit()
        except IntegrityError as exc:  # noqa: PERF203 - 只此一处
            db.rollback()
            if _is_red_id_conflict(exc):
                raise conflict(
                    "该小红书号已被其它账号占用",
                    details={"field": "xhs_red_id"},
                ) from exc
            raise

    # ------------------------------------------------------------------ 过滤
    def build_criteria(
        self,
        *,
        status: AccountStatus | None,
        q: str | None,
    ) -> Sequence[Any]:
        """把查询参数翻译成 SQLAlchemy 过滤条件。"""
        criteria: list[Any] = []
        if status is not None:
            criteria.append(Account.status == status)
        if q:
            criteria.append(Account.name.ilike(f"%{q.strip()}%"))
        return tuple(criteria)

    def get_by_red_id(self, db: Session, xhs_red_id: str) -> Account | None:
        """按小红书号查询（自检脚本与联调使用）。"""
        stmt = select(Account).where(Account.xhs_red_id == xhs_red_id)
        return db.execute(stmt).scalars().first()


crud_account = CRUDAccount()
