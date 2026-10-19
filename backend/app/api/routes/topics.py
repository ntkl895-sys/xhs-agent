"""选题路由：``/api/v1/topics``（第 10 步）。

契约：

    POST   /api/v1/topics/generate         触发选题生成（同步）
    GET    /api/v1/topics                  列表（分页 + 过滤）
    GET    /api/v1/topics/runs             选题 Agent 最近运行记录
    GET    /api/v1/topics/{topic_id}       详情
    PATCH  /api/v1/topics/{topic_id}       人工微调（含 draft→approved / discarded）

⚠️ 注册顺序：静态路径 ``/generate``、``/runs`` 必须**排在** UUID 路径 ``/{topic_id}`` **之前**，
否则 ``/runs`` 会被当作 ``topic_id`` 解析而报 422。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.agent.errors import AgentConfigError
from app.agent.topics.service import generate_for_hotspots, list_recent_runs
from app.api.deps import PageParams, get_db, pagination
from app.core.errors import bad_request, not_found, validation_error
from app.crud.topic import SORT_GENERATED_AT, SORT_SCORE, crud_topic
from app.models.enums import TopicStatus
from app.schemas.common import ErrorOutSpec, Page
from app.schemas.topic import (
    TopicGenerateRequest,
    TopicListItem,
    TopicOut,
    TopicRunOut,
    TopicUpdateRequest,
)

router = APIRouter(prefix="/api/v1/topics", tags=["topics"])

#: 本步允许的人工状态流转（其余由后续笔记步接管）
_ALLOWED_TRANSITIONS = {
    TopicStatus.DRAFT: {TopicStatus.APPROVED, TopicStatus.DISCARDED},
}


def _normalize_status(raw: str | None) -> TopicStatus | None:
    """选题列表的 ``status`` 过滤：留空 / ``all`` 表示全部。"""
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in ("", "all"):
        return None
    try:
        return TopicStatus(value)
    except ValueError:
        raise validation_error(
            "status 只能是 draft / approved / writing / published / discarded，或留空 / all",
            details=[{"loc": ["query", "status"], "msg": "非法取值", "type": "enum"}],
        ) from None


def _parse_uuid(raw: str | None, field: str) -> uuid.UUID | None:
    if raw is None or raw.strip() == "" or raw.strip().lower() == "all":
        return None
    try:
        return uuid.UUID(raw.strip())
    except ValueError:
        raise validation_error(
            f"{field} 需为 UUID，或留空 / all 表示全部",
            details=[{"loc": ["query", field], "msg": "非法取值", "type": "value_error"}],
        ) from None


def _parse_generated_date(raw: str | None) -> date | None:
    """解析 ``generated_at`` 过滤参数（``YYYY-MM-DD``，按 UTC 日历日）。"""
    if raw is None or raw.strip() == "" or raw.strip().lower() == "all":
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise validation_error(
            "generated_at 需为 YYYY-MM-DD 日期，或留空 / all 表示全部",
            details=[{"loc": ["query", "generated_at"], "msg": "非法取值", "type": "date"}],
        ) from None


def _normalize_sort(raw: str | None) -> str:
    value = (raw or SORT_SCORE).strip().lower()
    if value not in (SORT_SCORE, SORT_GENERATED_AT):
        raise validation_error(
            "sort 只能是 score 或 generated_at",
            details=[{"loc": ["query", "sort"], "msg": "非法取值", "type": "enum"}],
        )
    return value


def _normalize_order(raw: str | None) -> str:
    value = (raw or "desc").strip().lower()
    if value not in ("asc", "desc"):
        raise validation_error(
            "order 只能是 asc 或 desc",
            details=[{"loc": ["query", "order"], "msg": "非法取值", "type": "enum"}],
        )
    return value


# ---------------------------------------------------------------------------
# POST /generate（静态路径，先注册）
# ---------------------------------------------------------------------------
@router.post(
    "/generate",
    summary="触发选题生成（同步）",
    responses={**ErrorOutSpec.BAD_REQUEST, **ErrorOutSpec.VALIDATION, **ErrorOutSpec.NOT_FOUND},
)
def generate_topics(payload: TopicGenerateRequest) -> dict:
    """为一个（或多个）热点生成选题并落库，返回本次汇总。

    真实调用需要 ``AGENT_LLM_ENABLED=true`` 且已配置 ``LLM_API_KEY`` / ``LLM_MODEL``；
    未就绪时返回 400 并给出明确提示（不发起任何对外请求）。
    """
    try:
        return generate_for_hotspots(
            persona_id=payload.persona_id,
            hotspot_ids=payload.hotspot_ids,
            per_hotspot=payload.per_hotspot,
            limit_hotspots=payload.limit_hotspots,
            force=payload.force,
            trigger="manual",
        )
    except AgentConfigError as exc:
        raise bad_request(
            "LLM 未就绪，无法生成选题",
            details={"reason": str(exc)},
        ) from exc


# ---------------------------------------------------------------------------
# GET /runs（静态路径，必须早于 /{topic_id}）
# ---------------------------------------------------------------------------
@router.get("/runs", response_model=list[TopicRunOut], summary="选题 Agent 运行记录")
def list_topic_runs(
    db: Session = Depends(get_db),
    limit: int = Query(10, ge=1, le=100, description="返回条数"),
) -> list[TopicRunOut]:
    """最近的 ``run_type=topic_generation`` 运行记录（默认不含 ``llm_raw``）。"""
    rows = list_recent_runs(db, limit=limit)
    result: list[TopicRunOut] = []
    for row in rows:
        outputs = row.outputs if isinstance(row.outputs, dict) else {}
        result.append(
            TopicRunOut(
                run_id=row.id,
                trigger=row.trigger.value if row.trigger else None,
                status=row.status.value if row.status else None,
                created_at=row.created_at,
                finished_at=row.finished_at,
                latency_ms=row.latency_ms,
                prompt_tokens=row.prompt_tokens,
                completion_tokens=row.completion_tokens,
                total_tokens=row.total_tokens,
                json_parse_attempts=row.json_parse_attempts,
                error_message=row.error_message,
                generated=outputs.get("generated"),
                persisted=outputs.get("persisted"),
                rejected=outputs.get("rejected"),
                skipped=outputs.get("skipped"),
                topic_ids=outputs.get("topic_ids"),
            )
        )
    return result


# ---------------------------------------------------------------------------
# GET ""（列表）
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=Page[TopicListItem],
    summary="选题列表",
    responses=ErrorOutSpec.VALIDATION,
)
def list_topics(
    db: Session = Depends(get_db),
    page_params: PageParams = Depends(pagination),
    persona_id: str | None = Query(None, description="按人设 id 过滤"),
    account_id: str | None = Query(None, description="按账号 id 过滤"),
    status: str | None = Query(None, description="draft | approved | writing | published | discarded"),
    hotspot_id: str | None = Query(None, description="按来源热点 id 过滤（JSONB 包含匹配）"),
    q: str | None = Query(None, description="按标题模糊匹配"),
    generated_at: str | None = Query(None, description="按生成日期过滤（YYYY-MM-DD）"),
    sort: str | None = Query(None, description="score（默认）| generated_at"),
    order: str | None = Query(None, description="desc（默认）| asc"),
) -> Page[TopicListItem]:
    """分页查询选题，默认按 ``score`` 倒序（V1 综合排序分 = 模型置信度）。"""
    parsed_generated = _parse_generated_date(generated_at)
    criteria = crud_topic.build_criteria(
        status=_normalize_status(status),
        account_id=_parse_uuid(account_id, "account_id"),
        persona_id=_parse_uuid(persona_id, "persona_id"),
        hotspot_id=_parse_uuid(hotspot_id, "hotspot_id"),
        q=q,
        generated_date=parsed_generated,
    )
    total = crud_topic.count(db, *criteria)
    rows = crud_topic.get_multi_with_names(
        db,
        criteria=criteria,
        offset=page_params.offset,
        limit=page_params.limit,
        sort=_normalize_sort(sort),
        order=_normalize_order(order),
    )
    return Page[TopicListItem](
        items=[TopicListItem.from_row(topic, pname, aname) for topic, pname, aname in rows],
        total=total,
        page=page_params.page,
        page_size=page_params.page_size,
    )


# ---------------------------------------------------------------------------
# GET /{topic_id}（UUID 路径，最后注册）
# ---------------------------------------------------------------------------
@router.get(
    "/{topic_id}",
    response_model=TopicOut,
    summary="选题详情",
    responses=ErrorOutSpec.NOT_FOUND,
)
def get_topic(topic_id: uuid.UUID, db: Session = Depends(get_db)) -> TopicOut:
    """按 id 取选题详情（含 content_outline / tags / source_trace / meta；不含 llm_raw）。"""
    topic = crud_topic.get(db, topic_id)
    if topic is None:
        raise not_found("选题不存在", details={"topic_id": str(topic_id)})
    persona_name, account_name = _names_of(db, topic)
    return TopicOut.from_row(topic, persona_name, account_name)


@router.patch(
    "/{topic_id}",
    response_model=TopicOut,
    summary="人工微调选题（含 draft→approved / discarded）",
    responses={**ErrorOutSpec.NOT_FOUND, **ErrorOutSpec.VALIDATION, **ErrorOutSpec.BAD_REQUEST},
)
def update_topic(
    topic_id: uuid.UUID,
    payload: TopicUpdateRequest,
    db: Session = Depends(get_db),
) -> TopicOut:
    """部分更新。本步 ``status`` **仅允许** ``draft → approved`` 与 ``draft → discarded``。

    其余流转（``writing`` / ``published``）属后续笔记步；非法流转返回 422。
    本接口**不联动** ``hotspot.status``。
    """
    topic = crud_topic.get(db, topic_id)
    if topic is None:
        raise not_found("选题不存在", details={"topic_id": str(topic_id)})

    data = payload.model_dump(exclude_unset=True)
    for field in ("title", "status"):
        if field in data and data[field] is None:
            raise validation_error(
                f"{field} 不能为 null",
                details=[{"loc": ["body", field], "msg": "该字段不能为 null", "type": "null_not_allowed"}],
            )

    new_status = data.get("status")
    if new_status is not None and new_status != topic.status:
        allowed = _ALLOWED_TRANSITIONS.get(topic.status, set())
        if new_status not in allowed:
            raise validation_error(
                f"不允许的状态流转：{topic.status.value} → {new_status.value}",
                details=[
                    {
                        "loc": ["body", "status"],
                        "msg": "本步仅允许 draft→approved、draft→discarded",
                        "type": "invalid_transition",
                    }
                ],
            )

    for field, value in data.items():
        setattr(topic, field, value)
    db.commit()
    db.refresh(topic)

    persona_name, account_name = _names_of(db, topic)
    return TopicOut.from_row(topic, persona_name, account_name)


def _names_of(db: Session, topic: object) -> tuple[str | None, str | None]:
    """取选题关联的人设名 / 账号名（单对象响应用）。"""
    from app.models.account import Account
    from app.models.persona import Persona

    persona_name = None
    account_name = None
    persona_id = getattr(topic, "persona_id", None)
    account_id = getattr(topic, "account_id", None)
    if persona_id is not None:
        persona = db.get(Persona, persona_id)
        persona_name = persona.name if persona is not None else None
    if account_id is not None:
        account = db.get(Account, account_id)
        account_name = account.name if account is not None else None
    return persona_name, account_name
