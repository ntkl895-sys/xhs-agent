"""personas 的 Pydantic schema（Create / Update / Out）。

``PersonaOut`` 额外带 ``account_name``：由服务层 LEFT JOIN ``accounts`` 取名称，
**不为此新增数据库列**（第 8 步「不改表结构」红线）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints
from pydantic.functional_validators import AfterValidator

from app.models.enums import PersonaStatus
from app.schemas.common import OptTagList, OptText, blank_to_none

TrimmedName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
OptTone = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=200),
    AfterValidator(blank_to_none),
] | None


class PersonaCreate(BaseModel):
    """新建人设。``account_id`` 为空 = 公共人设库。"""

    name: TrimmedName
    account_id: uuid.UUID | None = None
    positioning: OptText | None = None
    target_audience: OptText | None = None
    tone: OptTone = None
    content_pillars: OptTagList = None
    taboo: OptTagList = None
    signature_style: OptText | None = None
    system_prompt: OptText | None = None
    is_default: bool = False


class PersonaUpdate(BaseModel):
    """部分更新人设；字段与 Create 一致，另加 ``status``。"""

    name: TrimmedName | None = None
    account_id: uuid.UUID | None = None
    positioning: OptText | None = None
    target_audience: OptText | None = None
    tone: OptTone = None
    content_pillars: OptTagList = None
    taboo: OptTagList = None
    signature_style: OptText | None = None
    system_prompt: OptText | None = None
    is_default: bool | None = None
    status: PersonaStatus | None = None


class PersonaOut(BaseModel):
    """人设输出模型。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID | None
    name: str
    positioning: str | None
    target_audience: str | None
    tone: str | None
    content_pillars: list[str] | None
    taboo: list[str] | None
    signature_style: str | None
    system_prompt: str | None
    is_default: bool
    status: PersonaStatus
    created_at: datetime
    updated_at: datetime
    # 展示用：归属账号名（公共人设库为 None），非数据库列
    account_name: str | None = None

    @classmethod
    def from_persona(cls, persona: object, account_name: str | None = None) -> "PersonaOut":
        """由 ORM 对象 + 账号名组装输出模型。"""
        return cls.model_validate(persona).model_copy(update={"account_name": account_name})
