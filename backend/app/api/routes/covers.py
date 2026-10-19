"""封面路由：``/api/v1/covers``（第 13 步 · 封面模块）。

契约：

    GET    /api/v1/covers?note_id=<必填>   某笔记的候选列表 + 当前选中 id
    POST   /api/v1/covers                  新建候选（笔记不存在 404）
    PATCH  /api/v1/covers/{cover_id}       更新字段 / 互斥选中（封面不存在 404）

本步**不提供** PUT / DELETE —— 封面候选不物理删除（随笔记 CASCADE）。
选中封面**不改动** ``notes.status``（笔记定稿属后续步骤）。

⚠️ 本步零 LLM、零迁移：封面由前端程序化渲染，后端只做候选数据的存取。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.errors import bad_request, not_found, validation_error
from app.crud.cover import crud_cover
from app.crud.note import crud_note
from app.models.note import Cover
from app.schemas.common import ErrorOutSpec
from app.schemas.cover import (
    CoverCreateRequest,
    CoverListOut,
    CoverOut,
    CoverUpdateRequest,
)

router = APIRouter(prefix="/api/v1/covers", tags=["covers"])

#: 非空列：PATCH 显式传 null 会在数据库层报错，提前拦为 422（业务码 validation）
_NON_NULLABLE_FIELDS = ("template_key", "color_scheme")


def _ensure_note(db: Session, note_id: uuid.UUID) -> None:
    """所属笔记必须存在，否则 404。"""
    if crud_note.get(db, note_id) is None:
        raise not_found("笔记不存在", details={"note_id": str(note_id)})


def _get_cover(db: Session, cover_id: uuid.UUID) -> Cover:
    """取候选，不存在则 404。"""
    cover = crud_cover.get(db, cover_id)
    if cover is None:
        raise not_found("封面候选不存在", details={"cover_id": str(cover_id)})
    return cover


# ---------------------------------------------------------------------------
# GET ""（集合；note_id 必填）
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=CoverListOut,
    summary="某笔记的封面候选列表",
    responses={**ErrorOutSpec.BAD_REQUEST, **ErrorOutSpec.VALIDATION, **ErrorOutSpec.NOT_FOUND},
)
def list_covers(
    note_id: uuid.UUID | None = Query(None, description="所属笔记 id（必填）"),
    db: Session = Depends(get_db),
) -> CoverListOut:
    """按创建时间正序返回候选；``selected_id`` 为当前选中候选（没有则 null）。"""
    if note_id is None:
        raise bad_request("note_id 为必填查询参数", details={"field": "note_id"})
    _ensure_note(db, note_id)
    items = crud_cover.list_by_note(db, note_id)
    selected_id = next((cover.id for cover in items if cover.is_selected), None)
    return CoverListOut(
        note_id=note_id,
        selected_id=selected_id,
        items=[CoverOut.model_validate(cover) for cover in items],
    )


# ---------------------------------------------------------------------------
# POST ""（新建候选）
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=CoverOut,
    summary="新建封面候选",
    responses={**ErrorOutSpec.VALIDATION, **ErrorOutSpec.NOT_FOUND},
)
def create_cover(payload: CoverCreateRequest, db: Session = Depends(get_db)) -> CoverOut:
    """新建候选（``width=1242`` / ``height=1656`` / ``is_selected=false`` / ``version=1``）。

    ``image_path`` 一律留 NULL：本工程不把导出 PNG 落服务器磁盘，
    封面图由前端实时渲染并下载；候选缩略图也由前端按 ``config_json`` 实时渲染。
    """
    _ensure_note(db, payload.note_id)
    cover = crud_cover.create_with_defaults(db, payload.model_dump())
    return CoverOut.model_validate(cover)


# ---------------------------------------------------------------------------
# PATCH /{cover_id}（静态段无冲突，UUID 路径仅此一条）
# ---------------------------------------------------------------------------
@router.patch(
    "/{cover_id}",
    response_model=CoverOut,
    summary="更新封面候选（is_selected=true 时互斥选中）",
    responses={**ErrorOutSpec.VALIDATION, **ErrorOutSpec.NOT_FOUND},
)
def update_cover(
    cover_id: uuid.UUID,
    payload: CoverUpdateRequest,
    db: Session = Depends(get_db),
) -> CoverOut:
    """部分更新字段；``is_selected=true`` 时在同一事务内互斥选中。

    - 同笔记其余候选全部置 ``false``，目标置 ``true``（一个 note 仅一个选中封面）；
    - ``is_selected=false`` 为显式取消选中（允许「没有选中封面」的中间态）；
    - **不改动** ``notes.status``（笔记仍为 draft，状态定稿属后续步骤）；
    - 第 20 步收口：普通字段与选中/取消选中**合并为单事务提交**
      （原实现分两次提交，进程中断可能留下半成品）。
    """
    cover = _get_cover(db, cover_id)
    data = payload.model_dump(exclude_unset=True)

    for field in _NON_NULLABLE_FIELDS:
        if field in data and data[field] is None:
            raise validation_error(
                f"{field} 不能为 null",
                details=[{"loc": ["body", field], "msg": "该字段为必填，不能为 null", "type": "value_error"}],
            )

    want_select = data.pop("is_selected", None)
    if data or want_select is not None:
        cover = crud_cover.update_with_selection(db, cover, data, select=want_select)
    return CoverOut.model_validate(cover)
