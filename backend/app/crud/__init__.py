"""CRUD 包。

约定：路由层**不写 SQLAlchemy 表达式**，只调用这里暴露的 ``crud_*`` 单例。
"""

from __future__ import annotations

from app.crud.account import CRUDAccount, crud_account
from app.crud.base import CRUDBase
from app.crud.persona import CRUDPersona, crud_persona

__all__ = [
    "CRUDBase",
    "CRUDAccount",
    "crud_account",
    "CRUDPersona",
    "crud_persona",
]
