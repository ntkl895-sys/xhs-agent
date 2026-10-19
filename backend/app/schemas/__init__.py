"""Pydantic v2 schema 包。

分层约定：``api/routes`` 只与 ``schemas`` 交换数据，``schemas`` 不依赖 ``crud``。
"""

from __future__ import annotations

from app.schemas.account import AccountCreate, AccountOut, AccountUpdate
from app.schemas.common import ErrorOut, Page
from app.schemas.persona import PersonaCreate, PersonaOut, PersonaUpdate

__all__ = [
    "Page",
    "ErrorOut",
    "AccountCreate",
    "AccountUpdate",
    "AccountOut",
    "PersonaCreate",
    "PersonaUpdate",
    "PersonaOut",
]
