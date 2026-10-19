"""covers 的 Pydantic schema（第 13 步 · 封面模块）。

- ``CoverCreateRequest``：新建候选（``note_id`` / ``template_key`` / ``color_scheme`` 必填）；
- ``CoverUpdateRequest``：部分更新（``is_selected=true`` 时路由走互斥选中）；
- ``CoverListOut``：某笔记的候选列表 + 当前选中 id（``GET /covers`` 的响应体）；
- ``CoverOut``：单个候选（**逐字段对齐 ``covers`` 表**；不含任何 llm / 密钥字段）。

字段长度与数据库列严格对齐（``template_key`` / ``color_scheme`` = varchar(32)，
``title_text`` / ``subtitle_text`` = varchar(100)），避免超长文本落库时报 500。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from pydantic.functional_validators import AfterValidator

from app.schemas.common import blank_to_none

#: ``template_key`` / ``color_scheme``：varchar(32)，strip 后非空
KeyText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)
]

#: ``title_text`` / ``subtitle_text``：varchar(100) 可空，全空白归一为 None
CoverText = (
    Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=100),
        AfterValidator(blank_to_none),
    ]
    | None
)


class CoverCreateRequest(BaseModel):
    """新建封面候选。

    ``width`` / ``height`` / ``is_selected`` / ``version`` 由后端默认值填充
    （1242 / 1656 / false / 1），请求体**不接受**这些字段（``extra=ignore``）。
    """

    note_id: uuid.UUID = Field(description="所属笔记 id（必填）")
    template_key: KeyText = Field(
        description="模板标识：gradient_hero / list_clean / bg_image / highlight"
    )
    color_scheme: KeyText = Field(
        description="配色方案：coral / cream / forest / neon / ocean"
    )
    title_text: CoverText = Field(default=None, description="封面主标题")
    subtitle_text: CoverText = Field(default=None, description="封面副标题 / 钩子")
    config_json: dict[str, Any] | None = Field(
        default=None,
        description="其余编辑字段（tags / bullets / author / page_no / highlight_word 等）",
    )


class CoverUpdateRequest(BaseModel):
    """部分更新封面候选；仅传需要变更的字段。

    ``is_selected=true`` 时由路由转 :meth:`crud.select_one` 在同一事务内互斥选中
    （同笔记其余候选全部置 false）。
    """

    template_key: KeyText | None = None
    color_scheme: KeyText | None = None
    title_text: CoverText = None
    subtitle_text: CoverText = None
    config_json: dict[str, Any] | None = None
    is_selected: bool | None = None


class CoverOut(BaseModel):
    """封面候选输出模型（逐字段对齐 ``covers`` 表）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    note_id: uuid.UUID
    template_key: str
    color_scheme: str
    title_text: str | None
    subtitle_text: str | None
    config_json: dict[str, Any] | None
    image_path: str | None
    width: int
    height: int
    is_selected: bool
    version: int
    created_at: datetime


class CoverListOut(BaseModel):
    """``GET /covers`` 响应：候选列表 + 当前选中 id。"""

    note_id: uuid.UUID
    selected_id: uuid.UUID | None = Field(
        default=None, description="当前选中候选 id；没有选中时为 null"
    )
    items: list[CoverOut]


__all__ = [
    "CoverCreateRequest",
    "CoverUpdateRequest",
    "CoverOut",
    "CoverListOut",
]
