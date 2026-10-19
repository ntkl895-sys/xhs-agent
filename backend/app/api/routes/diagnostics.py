"""单篇诊断路由：``/api/v1/diagnostics``（第 16 步）。

契约：

    POST  /api/v1/diagnostics/generate   对单篇笔记跑一次诊断并落库（同步，201）
    GET   /api/v1/diagnostics?note_id=   某笔记的诊断历史（created_at 倒序）
    GET   /api/v1/diagnostics/{id}       单条诊断详情

要点：

- **只增不改**：本步**不提供** PUT / PATCH / DELETE —— 诊断历史一经生成不可修改，
  随笔记 ``ON DELETE CASCADE`` 清理；
- **静态路径在前**：``/generate`` 必须先于 ``/{diagnostic_id}`` 注册；
- 诊断 = 只读 + 建议：落库只新增 ``diagnostics`` 一条，绝不修改 notes / topics /
  note_metrics，也不触发任何发布动作；
- 错误统一为 ``{status, code, message, details}``：400（LLM 未就绪 / 结构重试超限 /
  其它运行失败）/ 404（笔记或诊断不存在）/ 422（入参不合法）；**响应不含 Traceback
  与 ``llm_raw``**（统一错误体由 service 翻译，app.main 渲染）。

⚠️ 路由层不写任何 SQLAlchemy 表达式（第 8 步分层纪律，``selftest_api`` 静态断言）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.agent.diagnosis.service import diagnose_note
from app.api.deps import get_db
from app.core.errors import bad_request, not_found
from app.crud.diagnostic import crud_diagnostic
from app.crud.note import crud_note
from app.schemas.common import ErrorOutSpec
from app.schemas.diagnostic import (
    DiagnosticGenerateRequest,
    DiagnosticListOut,
    DiagnosticOut,
)

router = APIRouter(prefix="/api/v1/diagnostics", tags=["diagnostics"])


# ---------------------------------------------------------------------------
# POST /generate（静态路径，先注册）
# ---------------------------------------------------------------------------
@router.post(
    "/generate",
    response_model=DiagnosticOut,
    status_code=201,
    summary="对单篇笔记生成一次诊断（同步，落库）",
    responses={
        **ErrorOutSpec.BAD_REQUEST,
        **ErrorOutSpec.NOT_FOUND,
        **ErrorOutSpec.VALIDATION,
    },
)
def generate_diagnosis(
    payload: DiagnosticGenerateRequest, db: Session = Depends(get_db)
) -> DiagnosticOut:
    """跑一次诊断并落库一条 ``diagnostics``，返回该条记录。

    - 笔记不存在 → 404；``diagnosis_type`` 不合法 → 422；不是合法 UUID → 422；
    - LLM 未就绪 → 400（**不发起任何对外请求、不产生运行记录**）；
    - 结构校验连续超限 → 400（可重试）；
    - 数据不足（无快照等）**不是错误**：正常落库，``score=null`` 并在
      ``llm_meta.data_gaps`` 如实声明。
    """
    summary = diagnose_note(
        note_id=payload.note_id,
        diagnosis_type=payload.diagnosis_type,
    )
    row = crud_diagnostic.get_by_id(db, uuid.UUID(str(summary["diagnostic_id"])))
    if row is None:
        # 理论上不可达（刚提交的事务可见）；防御：给出可识别错误
        raise bad_request(
            "诊断已生成但读取失败，请刷新列表查看",
            details={"diagnostic_id": summary.get("diagnostic_id")},
        )
    return DiagnosticOut.model_validate(row)


# ---------------------------------------------------------------------------
# GET ""（诊断历史；note_id 必填）
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=DiagnosticListOut,
    summary="某笔记的诊断历史",
    responses={**ErrorOutSpec.BAD_REQUEST, **ErrorOutSpec.NOT_FOUND},
)
def list_diagnostics(
    note_id: uuid.UUID | None = Query(None, description="所属笔记 id（必填）"),
    db: Session = Depends(get_db),
) -> DiagnosticListOut:
    """按生成时间倒序返回某笔记的全部诊断（``id`` 兜底保证顺序稳定）。"""
    if note_id is None:
        raise bad_request("note_id 为必填查询参数", details={"field": "note_id"})
    if crud_note.get(db, note_id) is None:
        raise not_found("笔记不存在", details={"note_id": str(note_id)})
    items = crud_diagnostic.list_by_note(db, note_id)
    return DiagnosticListOut(
        note_id=note_id,
        items=[DiagnosticOut.model_validate(item) for item in items],
    )


# ---------------------------------------------------------------------------
# GET /{diagnostic_id}（UUID 路径，最后注册）
# ---------------------------------------------------------------------------
@router.get(
    "/{diagnostic_id}",
    response_model=DiagnosticOut,
    summary="诊断详情",
    responses=ErrorOutSpec.NOT_FOUND,
)
def get_diagnostic(diagnostic_id: uuid.UUID, db: Session = Depends(get_db)) -> DiagnosticOut:
    """按主键取单条诊断（不存在 → 404）。"""
    row = crud_diagnostic.get_by_id(db, diagnostic_id)
    if row is None:
        raise not_found(
            "诊断记录不存在", details={"diagnostic_id": str(diagnostic_id)}
        )
    return DiagnosticOut.model_validate(row)
