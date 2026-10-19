"""多版本创作路由：``/api/v1/content-variants``（第 23 步 · V2 增强（二））。

契约（仅 GET / POST / PATCH，**无 DELETE / PUT**）：

    POST   /api/v1/content-variants/generate          为选题生成 N 个可对比版本（同步，长耗时）
    GET    /api/v1/content-variants?topic_id=...      某选题的版本批次列表（不含 llm_raw）
    PATCH  /api/v1/content-variants/{note_id}/adopt   采用某版本（单事务：同批次其余置未采用）

⚠️ 注册顺序：静态路径 ``/generate`` 必须**排在** UUID 路径 ``/{note_id}/adopt`` **之前**。
无物理删除接口 —— 未采用的版本只做软标记（``llm_meta.variant.adopted``），永不删除。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from app.agent.content_variants import service as variants_service
from app.agent.errors import AgentConfigError
from app.core.errors import bad_request, validation_error
from app.schemas.common import ErrorOutSpec
from app.schemas.content_variants import (
    AdoptVariantResponse,
    VariantsBatchesResponse,
    VariantsGenerateRequest,
    VariantsGenerateResponse,
)

router = APIRouter(prefix="/api/v1/content-variants", tags=["content-variants"])


# ---------------------------------------------------------------------------
# POST /generate（静态路径，先注册）
# ---------------------------------------------------------------------------
@router.post(
    "/generate",
    response_model=VariantsGenerateResponse,
    summary="为一个已通过评审的选题生成 N 个可对比版本（同步）",
    responses={**ErrorOutSpec.BAD_REQUEST, **ErrorOutSpec.VALIDATION, **ErrorOutSpec.NOT_FOUND},
)
def generate_variants(payload: VariantsGenerateRequest) -> VariantsGenerateResponse:
    """生成 N 个角度明显不同的版本并落库（同批次，``status=draft``，必须人工审核）。

    - 选题必须存在（否则 404）且 ``status=approved``（否则 400，与第 12 步同一闸门）；
    - LLM 未就绪时返回 400（不发起任何对外请求）；
    - 逐版判定：``qualified``（落库）/ ``blocked``（违禁命中，拦截不落库）/
      ``rejected``（缺标题/正文，不落库）；失败（如 JSON 连续失败、全部不合格）
      返回 ``{"status": "failed", "error_message": ...}``，不阻断调用方。
    """
    try:
        result = variants_service.generate_variants(
            topic_id=payload.topic_id,
            n=payload.n,
            angles=payload.angles,
            trigger="manual",
        )
    except AgentConfigError as exc:
        raise bad_request(
            "LLM 未就绪，无法生成多版本",
            details={"reason": str(exc)},
        ) from exc
    return VariantsGenerateResponse.model_validate(result)


# ---------------------------------------------------------------------------
# GET ""（按选题查批次；纯只读）
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=VariantsBatchesResponse,
    summary="某选题的版本批次列表",
    responses={**ErrorOutSpec.VALIDATION, **ErrorOutSpec.NOT_FOUND},
)
def list_variants(
    topic_id: str = Query(..., description="选题 id（UUID）"),
) -> VariantsBatchesResponse:
    """该选题的多版本批次（倒序；批次内按索引升序；**不含** llm_raw 与向量）。"""
    try:
        parsed = uuid.UUID(topic_id.strip())
    except (ValueError, AttributeError):
        raise validation_error(
            "topic_id 需为 UUID",
            details=[{"loc": ["query", "topic_id"], "msg": "非法取值", "type": "value_error"}],
        ) from None
    return VariantsBatchesResponse.model_validate(
        variants_service.list_topic_batches(parsed)
    )


# ---------------------------------------------------------------------------
# PATCH /{note_id}/adopt（UUID 路径，最后注册）
# ---------------------------------------------------------------------------
@router.patch(
    "/{note_id}/adopt",
    response_model=AdoptVariantResponse,
    summary="采用某版本（单事务：同批次其余置未采用）",
    responses={**ErrorOutSpec.BAD_REQUEST, **ErrorOutSpec.NOT_FOUND},
)
def adopt_variant(note_id: uuid.UUID) -> AdoptVariantResponse:
    """互斥采用一个版本（可反复切换；不删除任何版本，不改笔记 / 选题状态机）。"""
    return AdoptVariantResponse.model_validate(variants_service.adopt_variant(note_id))
