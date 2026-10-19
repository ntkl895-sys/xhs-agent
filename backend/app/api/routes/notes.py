"""创作路由：``/api/v1/notes``（第 12 步；第 14 步增补 PATCH）。

契约：

    POST   /api/v1/notes/generate      触发内容创作（同步；LLM 未就绪 400）
    GET    /api/v1/notes/runs          创作 Agent 最近运行记录
    GET    /api/v1/notes               列表（分页 + 过滤）
    GET    /api/v1/notes/{note_id}     详情（404）
    PATCH  /api/v1/notes/{note_id}     人工编辑 / 状态流转 / 标记发布（第 14 步 · 创作台）

状态机（第 14 步，严格按此实现；非法跳转一律 400）：

    draft → ready（定稿）      ready → draft（退回修改）
    ready → published（人工已发布：回填链接 + 选题 writing→published）
    published → ready（撤销发布：清空发布字段 + 选题 published→writing）

禁止跳级（draft→published / published→draft）；``archived`` 本步不处理（400）。
**不提供** PUT / DELETE；发布与否完全由人决定，系统绝不自动发布。

⚠️ 注册顺序：静态路径 ``/generate``、``/runs`` 必须**排在** UUID 路径 ``/{note_id}`` **之前**，
否则 ``/runs`` 会被当作 ``note_id`` 解析而报 422。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.agent.content.service import create_note_for_topic, list_recent_runs
from app.agent.errors import AgentConfigError
from app.api.deps import PageParams, get_db, pagination
from app.core.errors import AppError, bad_request, not_found, validation_error
from app.crud.note import SORT_CREATED_AT, SORT_UPDATED_AT, crud_note
from app.models.enums import NoteStatus
from app.models.note import Note
from app.schemas.common import ErrorOutSpec, Page
from app.schemas.note import (
    NoteCreateRequest,
    NoteListItem,
    NoteOut,
    NoteRunOut,
    NoteUpdateRequest,
)

router = APIRouter(prefix="/api/v1/notes", tags=["notes"])


def _normalize_status(raw: str | None) -> NoteStatus | None:
    """笔记列表的 ``status`` 过滤：留空 / ``all`` 表示全部。"""
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in ("", "all"):
        return None
    try:
        return NoteStatus(value)
    except ValueError:
        raise validation_error(
            "status 只能是 draft / ready / published / archived，或留空 / all",
            details=[{"loc": ["query", "status"], "msg": "非法取值", "type": "enum"}],
        ) from None


def _parse_uuid(raw: str | None, field: str) -> uuid.UUID | None:
    """解析可空 UUID 过滤参数（留空 / ``all`` 表示不过滤）。"""
    if raw is None or raw.strip() == "" or raw.strip().lower() == "all":
        return None
    try:
        return uuid.UUID(raw.strip())
    except ValueError:
        raise validation_error(
            f"{field} 需为 UUID，或留空 / all 表示全部",
            details=[{"loc": ["query", field], "msg": "非法取值", "type": "value_error"}],
        ) from None


def _normalize_sort(raw: str | None) -> str:
    """排序字段：created_at（默认）/ updated_at。"""
    value = (raw or SORT_CREATED_AT).strip().lower()
    if value not in (SORT_CREATED_AT, SORT_UPDATED_AT):
        raise validation_error(
            "sort 只能是 created_at 或 updated_at",
            details=[{"loc": ["query", "sort"], "msg": "非法取值", "type": "enum"}],
        )
    return value


def _normalize_order(raw: str | None) -> str:
    """排序方向：desc（默认）/ asc。"""
    value = (raw or "desc").strip().lower()
    if value not in ("asc", "desc"):
        raise validation_error(
            "order 只能是 asc 或 desc",
            details=[{"loc": ["query", "order"], "msg": "非法取值", "type": "enum"}],
        )
    return value


def _names_of(db: Session, note: object) -> tuple[str | None, str | None]:
    """取笔记关联的人设名 / 账号名（单对象响应用；不新增数据库列）。"""
    from app.models.account import Account
    from app.models.persona import Persona

    persona_name = None
    account_name = None
    persona_id = getattr(note, "persona_id", None)
    account_id = getattr(note, "account_id", None)
    if persona_id is not None:
        persona = db.get(Persona, persona_id)
        persona_name = persona.name if persona is not None else None
    if account_id is not None:
        account = db.get(Account, account_id)
        account_name = account.name if account is not None else None
    return persona_name, account_name


# ---------------------------------------------------------------------------
# POST /generate（静态路径，先注册）
# ---------------------------------------------------------------------------
@router.post(
    "/generate",
    summary="为一个已通过评审的选题生成笔记草稿（同步）",
    responses={**ErrorOutSpec.BAD_REQUEST, **ErrorOutSpec.VALIDATION, **ErrorOutSpec.NOT_FOUND},
)
def generate_note(payload: NoteCreateRequest) -> dict:
    """生成并落库一篇笔记草稿（``status=draft``，必须人工审核）。

    - 选题必须存在（否则 404）且 ``status=approved``（否则 400）；
    - LLM 未就绪时返回 400（不发起任何对外请求）；
    - 生成失败（如 JSON 连续失败）返回 ``{"status": "failed", "error_message": ...}``，
      与 ``POST /api/v1/topics/generate`` 的批量语义保持一致。
    """
    try:
        return create_note_for_topic(
            topic_id=payload.topic_id,
            account_id=payload.account_id,
            trigger="manual",
        )
    except AgentConfigError as exc:
        raise bad_request(
            "LLM 未就绪，无法生成笔记",
            details={"reason": str(exc)},
        ) from exc


# ---------------------------------------------------------------------------
# GET /runs（静态路径，必须早于 /{note_id}）
# ---------------------------------------------------------------------------
@router.get("/runs", response_model=list[NoteRunOut], summary="创作 Agent 运行记录")
def list_note_runs(
    db: Session = Depends(get_db),
    limit: int = Query(10, ge=1, le=100, description="返回条数"),
) -> list[NoteRunOut]:
    """最近的 ``run_type=content_creation`` 运行记录（不含 ``llm_raw``）。"""
    rows = list_recent_runs(db, limit=limit)
    result: list[NoteRunOut] = []
    for row in rows:
        outputs = row.outputs if isinstance(row.outputs, dict) else {}
        result.append(
            NoteRunOut(
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
                note_id=outputs.get("note_id"),
                title=outputs.get("title"),
                compliance_status=outputs.get("compliance_status"),
                forbidden_hits=outputs.get("forbidden_hits"),
                tags_count=outputs.get("tags_count"),
                generated=outputs.get("generated"),
                degraded=outputs.get("degraded"),
            )
        )
    return result


# ---------------------------------------------------------------------------
# GET ""（列表）
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=Page[NoteListItem],
    summary="笔记列表",
    responses=ErrorOutSpec.VALIDATION,
)
def list_notes(
    db: Session = Depends(get_db),
    page_params: PageParams = Depends(pagination),
    account_id: str | None = Query(None, description="按账号 id 过滤"),
    persona_id: str | None = Query(None, description="按人设 id 过滤"),
    topic_id: str | None = Query(None, description="按来源选题 id 过滤"),
    status: str | None = Query(None, description="draft | ready | published | archived"),
    q: str | None = Query(None, description="按标题模糊匹配"),
    sort: str | None = Query(None, description="created_at（默认）| updated_at"),
    order: str | None = Query(None, description="desc（默认）| asc"),
) -> Page[NoteListItem]:
    """分页查询笔记草稿，默认按 ``created_at`` 倒序。"""
    criteria = crud_note.build_criteria(
        status=_normalize_status(status),
        account_id=_parse_uuid(account_id, "account_id"),
        persona_id=_parse_uuid(persona_id, "persona_id"),
        topic_id=_parse_uuid(topic_id, "topic_id"),
        q=q,
    )
    total = crud_note.count(db, *criteria)
    rows = crud_note.get_multi_with_names(
        db,
        criteria=criteria,
        offset=page_params.offset,
        limit=page_params.limit,
        sort=_normalize_sort(sort),
        order=_normalize_order(order),
    )
    return Page[NoteListItem](
        items=[NoteListItem.from_row(note, pname, aname) for note, pname, aname in rows],
        total=total,
        page=page_params.page,
        page_size=page_params.page_size,
    )


# ---------------------------------------------------------------------------
# GET /{note_id}（UUID 路径，最后注册）
# ---------------------------------------------------------------------------
@router.get(
    "/{note_id}",
    response_model=NoteOut,
    summary="笔记详情",
    responses=ErrorOutSpec.NOT_FOUND,
)
def get_note(note_id: uuid.UUID, db: Session = Depends(get_db)) -> NoteOut:
    """按 id 取笔记详情（含正文 / 合规自检信息；**不含** ``llm_raw``）。

    ``llm_meta`` 只返回白名单非敏感项（prompt_version / json_parse_attempts / model）。
    """
    note = crud_note.get(db, note_id)
    if note is None:
        raise not_found("笔记不存在", details={"note_id": str(note_id)})
    persona_name, account_name = _names_of(db, note)
    return NoteOut.from_row(note, persona_name, account_name)


# ---------------------------------------------------------------------------
# PATCH /{note_id}（第 14 步 · 创作台：人工编辑 / 状态流转 / 标记发布）
# ---------------------------------------------------------------------------
def _transition_error(current: NoteStatus, target: NoteStatus) -> AppError:
    """非法状态流转的统一 400（附允许清单，便于前端与自检对照）。"""
    return bad_request(
        f"不允许的状态流转：{current.value} → {target.value}",
        details={
            "current": current.value,
            "target": target.value,
            "allowed": [
                "draft -> ready",
                "ready -> draft",
                "ready -> published",
                "published -> ready",
            ],
        },
    )


def _apply_edit_fields(db: Session, note: Note, data: dict) -> NoteOut:
    """不带 ``status`` 的分支：仅编辑 ``title / body / tags``。"""
    if "publish_url" in data or "xhs_note_id" in data:
        raise bad_request(
            "publish_url / xhs_note_id 仅在标记发布（status=published）时可回填；"
            "编辑请求（不带 status）不能携带发布字段"
        )
    content = {key: data[key] for key in ("title", "body", "tags") if key in data}
    if not content:
        raise bad_request(
            "没有可更新的字段",
            details={"editable": ["title", "body", "tags"], "hint": "状态流转请携带 status"},
        )
    updated = crud_note.update_content(db, note.id, content)
    if updated is None:
        raise not_found("笔记不存在", details={"note_id": str(note.id)})
    return NoteOut.from_row(updated, *_names_of(db, updated))


def _apply_transition(db: Session, note: Note, data: dict) -> NoteOut:
    """带 ``status`` 的分支：严格状态机流转（见模块 docstring）。"""
    target = data["status"]
    current = note.status

    mixed = [key for key in ("title", "body", "tags") if key in data]
    if mixed:
        raise bad_request(
            "状态流转请求不能同时携带编辑字段（请先「保存修改」，再做状态流转）",
            details={"fields": mixed},
        )
    if target != NoteStatus.PUBLISHED and ("publish_url" in data or "xhs_note_id" in data):
        raise bad_request("publish_url / xhs_note_id 仅在标记发布（status=published）时可回填")
    if target == NoteStatus.ARCHIVED:
        raise bad_request("本步不支持归档（archived）", details={"status": "archived"})

    updated: Note | None = None
    try:
        if target == NoteStatus.READY and current == NoteStatus.DRAFT:
            updated = crud_note.set_status_draft_ready(db, note.id, NoteStatus.READY)
        elif target == NoteStatus.READY and current == NoteStatus.PUBLISHED:
            updated = crud_note.revert_to_ready(db, note.id)
        elif target == NoteStatus.DRAFT and current == NoteStatus.READY:
            updated = crud_note.set_status_draft_ready(db, note.id, NoteStatus.DRAFT)
        elif target == NoteStatus.PUBLISHED and current == NoteStatus.READY:
            updated = crud_note.mark_published(
                db,
                note.id,
                publish_url=data.get("publish_url"),
                xhs_note_id=data.get("xhs_note_id"),
            )
        else:
            raise _transition_error(current, target)
    except ValueError as exc:
        # crud 层的防御性校验（正常流程不触发；并发竞态时兜底为 400）
        raise bad_request(str(exc), details={"note_id": str(note.id)}) from exc

    if updated is None:
        raise not_found("笔记不存在", details={"note_id": str(note.id)})
    return NoteOut.from_row(updated, *_names_of(db, updated))


@router.patch(
    "/{note_id}",
    response_model=NoteOut,
    summary="编辑笔记 / 状态流转 / 标记发布（人工操作）",
    responses={**ErrorOutSpec.BAD_REQUEST, **ErrorOutSpec.VALIDATION, **ErrorOutSpec.NOT_FOUND},
)
def update_note(
    note_id: uuid.UUID,
    payload: NoteUpdateRequest,
    db: Session = Depends(get_db),
) -> NoteOut:
    """部分更新一篇笔记（第 14 步 · 创作台的唯一写接口）。

    分流规则：

    - **不带** ``status``：仅编辑 ``title / body / tags``（不触碰状态与选题）；
    - **带** ``status``：状态流转（``draft↔ready``、``ready→published``、``published→ready``），
      ``status=published`` 时可回填 ``publish_url`` / ``xhs_note_id``（均可留空）；
    - 非法跳转 / 混合请求 / 转 ``archived``：一律 400；笔记不存在 404。

    发布永远是**人工动作**：本接口只记录「我已在小红书完成发布」这一事实，
    不发起任何对外请求、不登录小红书、不自动发布。
    """
    note = crud_note.get(db, note_id)
    if note is None:
        raise not_found("笔记不存在", details={"note_id": str(note_id)})

    data = payload.model_dump(exclude_unset=True)

    # 非空列 / 语义字段的显式 null → 422（与 covers 的拦截口径一致）
    if "title" in data and data["title"] is None:
        raise validation_error(
            "title 不能为 null",
            details=[
                {
                    "loc": ["body", "title"],
                    "msg": "该字段为必填，不能为 null",
                    "type": "value_error",
                }
            ],
        )
    if "status" in data and data["status"] is None:
        raise validation_error(
            "status 不能为 null（不流转时请不要携带该字段）",
            details=[
                {"loc": ["body", "status"], "msg": "不能为 null", "type": "value_error"}
            ],
        )

    if "status" not in data:
        return _apply_edit_fields(db, note, data)
    return _apply_transition(db, note, data)
