"""账号路由：``/api/v1/accounts``（第 8 步范式）。

契约（方法 / 路径 / 状态码固定）：

    GET    /api/v1/accounts            列表（page / page_size / status / q）
    POST   /api/v1/accounts            新建 -> 201
    GET    /api/v1/accounts/{id}       详情
    PATCH  /api/v1/accounts/{id}       部分更新（含归档 / 恢复）

没有物理 DELETE：删除 = ``PATCH {"status": "archived"}``。
路由层不写 SQLAlchemy 表达式，全部委托 ``app.crud``。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import PageParams, get_db, pagination
from app.core.errors import not_found, validation_error
from app.crud.account import crud_account
from app.models.enums import AccountStatus
from app.schemas.account import AccountCreate, AccountOut, AccountUpdate
from app.schemas.common import ErrorOutSpec, Page

router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])


def _normalize_status(raw: str | None) -> AccountStatus | None:
    """把 ``status`` 查询参数归一化。

    - 不传（None） -> ``active``（默认只看在用账号）；
    - 传空串 / ``all`` -> ``None``（不过滤，含已归档）；
    - ``active`` / ``archived`` -> 对应枚举；
    - 其它 -> 422。
    """
    if raw is None:
        return AccountStatus.ACTIVE
    value = raw.strip().lower()
    if value in ("", "all"):
        return None
    try:
        return AccountStatus(value)
    except ValueError:
        raise validation_error(
            "status 只能是 active / archived，或留空 / all 表示全部",
            details=[{"loc": ["query", "status"], "msg": "非法取值", "type": "enum"}],
        ) from None


@router.get(
    "",
    response_model=Page[AccountOut],
    summary="账号列表",
    responses=ErrorOutSpec.VALIDATION,
)
def list_accounts(
    db: Session = Depends(get_db),
    page_params: PageParams = Depends(pagination),
    status: str | None = Query(None, description="active | archived；留空或 all 表示全部"),
    q: str | None = Query(None, description="按账号名模糊匹配"),
) -> Page[AccountOut]:
    """分页查询账号，默认按 ``created_at`` 倒序。"""
    criteria = crud_account.build_criteria(status=_normalize_status(status), q=q)
    total = crud_account.count(db, *criteria)
    rows = crud_account.get_multi(
        db,
        criteria=criteria,
        offset=page_params.offset,
        limit=page_params.limit,
    )
    return Page[AccountOut](
        items=[AccountOut.model_validate(row) for row in rows],
        total=total,
        page=page_params.page,
        page_size=page_params.page_size,
    )


@router.post(
    "",
    response_model=AccountOut,
    status_code=201,
    summary="新建账号",
    responses={**ErrorOutSpec.CONFLICT, **ErrorOutSpec.VALIDATION},
)
def create_account(payload: AccountCreate, db: Session = Depends(get_db)) -> AccountOut:
    """新建账号。小红书号撞唯一约束时返回 409。"""
    obj = crud_account.create(db, payload.model_dump())
    return AccountOut.model_validate(obj)


@router.get(
    "/{account_id}",
    response_model=AccountOut,
    summary="账号详情",
    responses=ErrorOutSpec.NOT_FOUND,
)
def get_account(account_id: uuid.UUID, db: Session = Depends(get_db)) -> AccountOut:
    """按 id 取账号详情，不存在返回 404。"""
    obj = crud_account.get(db, account_id)
    if obj is None:
        raise not_found("账号不存在", details={"account_id": str(account_id)})
    return AccountOut.model_validate(obj)


@router.patch(
    "/{account_id}",
    response_model=AccountOut,
    summary="更新账号（含归档 / 恢复）",
    responses={**ErrorOutSpec.NOT_FOUND, **ErrorOutSpec.CONFLICT, **ErrorOutSpec.VALIDATION},
)
def update_account(
    account_id: uuid.UUID,
    payload: AccountUpdate,
    db: Session = Depends(get_db),
) -> AccountOut:
    """部分更新。未提供的字段保持不变；``status`` 传 ``archived`` 即归档。"""
    obj = crud_account.get(db, account_id)
    if obj is None:
        raise not_found("账号不存在", details={"account_id": str(account_id)})

    data = payload.model_dump(exclude_unset=True)
    # name / status 显式传 null 属于非法（列上有 NOT NULL 约束）
    for field in ("name", "status"):
        if field in data and data[field] is None:
            raise validation_error(
                f"{field} 不能为 null",
                details=[{"loc": ["body", field], "msg": "该字段不能为 null", "type": "null_not_allowed"}],
            )

    obj = crud_account.update(db, obj, data)
    return AccountOut.model_validate(obj)
