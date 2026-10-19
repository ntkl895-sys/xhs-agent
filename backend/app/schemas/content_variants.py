"""多版本创作（第 23 步 · V2 增强（二））的 Pydantic schema。

安全约定：**任何响应模型都不含 ``llm_raw``、不含向量字段** —— ``llm_raw`` 只写进
``agent_runs``，接口一律不返回（与第 10 / 12 步同一口径）。出参含每版角度 /
版本级状态（``qualified`` / ``blocked`` / ``rejected``）/ 采用状态 / 批次号。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.content_variants.contracts import (
    ANGLE_MAX_LEN,
    DEFAULT_VARIANTS,
    MAX_VARIANTS,
    MIN_VARIANTS,
)

#: 版本级状态（与 contracts 的三值词表一致）
VariantOutcome = Literal["qualified", "blocked", "rejected"]


class VariantsGenerateRequest(BaseModel):
    """``POST /api/v1/content-variants/generate`` 请求体。

    - ``n``：版本数，1~5（默认 3）；越界由字段校验拒绝为 422；
    - ``angles``：可选。留空（或传 ``[]``）= 模型从内置角度池自选且互不重复；
      提供时：数量必须与 ``n`` 一致、单条 1~``ANGLE_MAX_LEN`` 字、不得重复（违反即 422）。
    """

    model_config = ConfigDict(extra="ignore")

    topic_id: uuid.UUID = Field(description="选题 id（必填；必须存在且 status=approved）")
    n: int = Field(
        default=DEFAULT_VARIANTS,
        ge=MIN_VARIANTS,
        le=MAX_VARIANTS,
        description=f"版本数（{MIN_VARIANTS}~{MAX_VARIANTS}，默认 {DEFAULT_VARIANTS}）",
    )
    angles: list[str] | None = Field(
        default=None,
        description="显式角度列表（可选；留空=自动从角度池自选）",
    )

    @model_validator(mode="after")
    def _check_angles(self) -> "VariantsGenerateRequest":
        """角度列表的归一与校验（空列表视同留空）。"""
        if self.angles is None or len(self.angles) == 0:
            self.angles = None
            return self
        cleaned = [item.strip() for item in self.angles]
        if any(not item for item in cleaned):
            raise ValueError("angles 不能包含空字符串")
        if any(len(item) > ANGLE_MAX_LEN for item in cleaned):
            raise ValueError(f"单个角度不超过 {ANGLE_MAX_LEN} 字")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("angles 不得重复")
        if len(cleaned) != self.n:
            raise ValueError("angles 的数量需与 n 一致（或留空自动选取）")
        self.angles = cleaned
        return self


class VariantItemOut(BaseModel):
    """生成响应中的单个版本（含被拦截 / 不合格的版本，供前端即时对比）。"""

    angle: str
    title: str
    body: str
    tags: list[str]
    status: VariantOutcome
    reason: str | None = Field(default=None, description="blocked / rejected 的可读原因")
    note_id: uuid.UUID | None = Field(
        default=None, description="仅 qualified 版本有值（已落库的笔记 id）"
    )
    hits: list[dict[str, Any]] | None = Field(
        default=None, description="仅 blocked 版本有值（命中词 / 类别 / 字段 / 片段）"
    )


class VariantsGenerateResponse(BaseModel):
    """``POST /generate`` 的返回：汇总计数 + 逐版明细（不含 llm_raw）。"""

    status: str = Field(description="success（全部合格）/ partial（部分合格）/ failed（全失败）")
    run_id: uuid.UUID | None = None
    topic_id: uuid.UUID
    variant_group_id: uuid.UUID | None = Field(
        default=None, description="批次号（= 本次运行的 run_id；无落库时仍返回便于对照）"
    )
    requested: int = Field(description="请求的版本数 n")
    generated: int | None = Field(default=None, description="实际解析出的版本数（≤ n）")
    persisted: int | None = Field(default=None, description="落库版本数（qualified）")
    blocked: int | None = Field(default=None, description="违禁命中被拦截的版本数")
    rejected: int | None = Field(default=None, description="缺标题/正文等不合格的版本数")
    items: list[VariantItemOut] = Field(default_factory=list)
    note_ids: list[uuid.UUID] = Field(default_factory=list)
    error_message: str | None = None


class VariantNoteOut(BaseModel):
    """批次内的一条版本笔记（对比 / 采用响应用；不含 llm_raw）。"""

    note_id: uuid.UUID
    angle: str
    index: int | None = None
    title: str
    body: str
    tags: list[str]
    compliance_status: str
    adopted: bool
    created_at: datetime


class VariantBatchOut(BaseModel):
    """一个批次（variant_group_id 下的一组版本）。"""

    group_id: uuid.UUID
    created_at: datetime
    variants: list[VariantNoteOut]


class VariantsBatchesResponse(BaseModel):
    """``GET /api/v1/content-variants?topic_id=`` 的返回（批次倒序）。"""

    topic_id: uuid.UUID
    batches: list[VariantBatchOut]


class AdoptVariantResponse(BaseModel):
    """``PATCH /{note_id}/adopt`` 的返回：采用后的整批最新状态。"""

    group_id: uuid.UUID
    adopted_note_id: uuid.UUID
    variants: list[VariantNoteOut]


__all__ = [
    "AdoptVariantResponse",
    "VariantBatchOut",
    "VariantItemOut",
    "VariantNoteOut",
    "VariantsBatchesResponse",
    "VariantsGenerateRequest",
    "VariantsGenerateResponse",
]
