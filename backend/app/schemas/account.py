"""accounts 的 Pydantic schema（Create / Update / Out）。

字段上限与第 7 步建表时的列宽严格一致（name≤100、xhs_red_id≤64、niche≤100），
避免「应用层放行、数据库截断/报错」的不一致。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, StringConstraints
from pydantic.functional_validators import AfterValidator
from typing import Annotated

from app.models.enums import AccountStatus
from app.schemas.common import OptText, blank_to_none

# name 必填：strip 后 1~100 字（空串会直接 422）
TrimmedName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
# 可空文本：先 strip 再限制长度，全空白归一为 None
OptRedId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=64),
    AfterValidator(blank_to_none),
] | None
OptNiche = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=100),
    AfterValidator(blank_to_none),
] | None


class AccountCreate(BaseModel):
    """新建账号。"""

    name: TrimmedName
    xhs_red_id: OptRedId = None
    niche: OptNiche = None
    remark: OptText | None = None


class AccountUpdate(BaseModel):
    """部分更新账号（未提供的字段不动）。

    - ``status`` 传 ``active`` / ``archived`` 即完成「恢复 / 归档」；
    - ``xhs_red_id`` / ``niche`` / ``remark`` 传 ``null`` 或 ``""`` 表示清空。
    """

    name: TrimmedName | None = None
    xhs_red_id: OptRedId = None
    niche: OptNiche = None
    remark: OptText | None = None
    status: AccountStatus | None = None


class AccountOut(BaseModel):
    """账号输出模型。时间字段由 Pydantic 序列化为 ISO 8601。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    xhs_red_id: str | None
    niche: str | None
    status: AccountStatus
    remark: str | None
    created_at: datetime
    updated_at: datetime
