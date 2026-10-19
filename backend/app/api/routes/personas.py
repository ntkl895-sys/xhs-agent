"""人设路由：``/api/v1/personas``（与账号同构）。

契约：

    GET    /api/v1/personas            列表（page / page_size / status / account_id / q）
    POST   /api/v1/personas            新建 -> 201
    GET    /api/v1/personas/{id}       详情
    PATCH  /api/v1/personas/{id}       部分更新（含归档 / 恢复）

``account_id`` 查询参数语义：
    不传 / 空 / ``all`` / ``null`` -> 全部（含公共与各账号）
    ``public``                      -> 只看公共人设库（account_id IS NULL）
    <uuid>                          -> 只看该账号
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import PageParams, get_db, pagination
from app.core.errors import bad_request, not_found, validation_error
from app.crud.persona import SCOPE_ACCOUNT, SCOPE_ALL, SCOPE_PUBLIC, AccountScope, crud_persona
from app.models.enums import PersonaStatus
from app.schemas.common import ErrorOutSpec, Page
from app.schemas.persona import PersonaCreate, PersonaOut, PersonaUpdate

router = APIRouter(prefix="/api/v1/personas", tags=["personas"])


def _normalize_status(raw: str | None) -> PersonaStatus | None:
    """与账号一致：不传默认 ``active``；空 / ``all`` 表示不过滤。"""
    if raw is None:
        return PersonaStatus.ACTIVE
    value = raw.strip().lower()
    if value in ("", "all"):
        return None
    try:
        return PersonaStatus(value)
    except ValueError:
        raise validation_error(
            "status 只能是 active / archived，或留空 / all 表示全部",
            details=[{"loc": ["query", "status"], "msg": "非法取值", "type": "enum"}],
        ) from None


def _parse_account_scope(raw: str | None) -> AccountScope:
    """解析 ``account_id`` 查询参数（见模块 docstring）。"""
    if raw is None:
        return (SCOPE_ALL, None)
    value = raw.strip()
    lowered = value.lower()
    if value == "" or lowered in ("all", "null"):
        return (SCOPE_ALL, None)
    if lowered in ("public", "none"):
        return (SCOPE_PUBLIC, None)
    try:
        return (SCOPE_ACCOUNT, uuid.UUID(value))
    except ValueError:
        raise validation_error(
            "account_id 需为 UUID、'public'，或留空表示全部",
            details=[{"loc": ["query", "account_id"], "msg": "非法取值", "type": "value_error"}],
        ) from None


@router.get(
    "",
    response_model=Page[PersonaOut],
    summary="人设列表",
    responses=ErrorOutSpec.VALIDATION,
)
def list_personas(
    db: Session = Depends(get_db),
    page_params: PageParams = Depends(pagination),
    status: str | None = Query(None, description="active | archived；留空或 all 表示全部"),
    account_id: str | None = Query(
        None, description="UUID 精确匹配；public=只看公共；留空=全部"
    ),
    q: str | None = Query(None, description="按人设名模糊匹配"),
) -> Page[PersonaOut]:
    """分页查询人设，默认按 ``created_at`` 倒序，并带出归属账号名。"""
    scope = _parse_account_scope(account_id)
    criteria = crud_persona.build_criteria(
        status=_normalize_status(status), scope=scope, q=q
    )
    total = crud_persona.count(db, *criteria)
    rows = crud_persona.get_multi_with_account(
        db,
        criteria=criteria,
        offset=page_params.offset,
        limit=page_params.limit,
    )
    return Page[PersonaOut](
        items=[PersonaOut.from_persona(persona, account_name) for persona, account_name in rows],
        total=total,
        page=page_params.page,
        page_size=page_params.page_size,
    )


@router.post(
    "",
    response_model=PersonaOut,
    status_code=201,
    summary="新建人设",
    responses={**ErrorOutSpec.BAD_REQUEST, **ErrorOutSpec.VALIDATION},
)
def create_persona(payload: PersonaCreate, db: Session = Depends(get_db)) -> PersonaOut:
    """新建人设。``account_id`` 非空时必须存在，否则返回 400。"""
    if payload.account_id is not None and not crud_persona.account_exists(db, payload.account_id):
        raise bad_request(
            "指定的归属账号不存在",
            details={"field": "account_id", "account_id": str(payload.account_id)},
        )
    obj = crud_persona.create(db, payload.model_dump())
    return PersonaOut.from_persona(obj, crud_persona.account_name_of(db, obj))


@router.get(
    "/{persona_id}",
    response_model=PersonaOut,
    summary="人设详情",
    responses=ErrorOutSpec.NOT_FOUND,
)
def get_persona(persona_id: uuid.UUID, db: Session = Depends(get_db)) -> PersonaOut:
    """按 id 取人设详情，不存在返回 404。"""
    obj = crud_persona.get(db, persona_id)
    if obj is None:
        raise not_found("人设不存在", details={"persona_id": str(persona_id)})
    return PersonaOut.from_persona(obj, crud_persona.account_name_of(db, obj))


@router.patch(
    "/{persona_id}",
    response_model=PersonaOut,
    summary="更新人设（含归档 / 恢复）",
    responses={**ErrorOutSpec.NOT_FOUND, **ErrorOutSpec.BAD_REQUEST, **ErrorOutSpec.VALIDATION},
)
def update_persona(
    persona_id: uuid.UUID,
    payload: PersonaUpdate,
    db: Session = Depends(get_db),
) -> PersonaOut:
    """部分更新。把 ``is_default`` 置 true 会在同一事务内取消同范围其它默认。"""
    obj = crud_persona.get(db, persona_id)
    if obj is None:
        raise not_found("人设不存在", details={"persona_id": str(persona_id)})

    data = payload.model_dump(exclude_unset=True)
    for field in ("name", "status", "is_default"):
        if field in data and data[field] is None:
            raise validation_error(
                f"{field} 不能为 null",
                details=[{"loc": ["body", field], "msg": "该字段不能为 null", "type": "null_not_allowed"}],
            )
    if data.get("account_id") is not None and not crud_persona.account_exists(
        db, data["account_id"]
    ):
        raise bad_request(
            "指定的归属账号不存在",
            details={"field": "account_id", "account_id": str(data["account_id"])},
        )

    obj = crud_persona.update(db, obj, data)
    return PersonaOut.from_persona(obj, crud_persona.account_name_of(db, obj))
