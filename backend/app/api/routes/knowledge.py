"""知识库路由：``/api/v1/knowledge``（第 18 步 · RAG 反哺；第 19 步 · 人工维护）。

契约（静态路径全部先于 ``/{id}`` 注册）：

    GET   /api/v1/knowledge                  知识块列表（分页；?chunk_type=&status=&limit=&offset=）
    POST  /api/v1/knowledge/extract          跑规律提炼图（201）
    POST  /api/v1/knowledge/search           检索测试（top-k；含 similarity）
    POST  /api/v1/knowledge/manual           手动创建知识块（201；第 19 步）
    POST  /api/v1/knowledge/seed-forbidden-words  幂等同步内置违禁词表（?dry_run=；第 19 步）
    POST  /api/v1/knowledge/import-text      文档导入·文本粘贴（201；第 19 步）
    POST  /api/v1/knowledge/import-file      文档导入·.txt/.md 上传（multipart；201；第 19 步）
    GET   /api/v1/knowledge/{id}             详情（不存在 404）
    PATCH /api/v1/knowledge/{id}             编辑（status / title / content；归档与恢复）

要点：

- **无 PUT、无 DELETE**：知识块只增 / 归档，不提供物理删除；
- **静态路径在前**：``/extract`` / ``/search`` / ``/manual`` / ``/seed-forbidden-words``
  / ``/import-text`` / ``/import-file`` 必须先于 ``/{id}`` 注册；
- 响应**不含 embedding 原始向量 / llm_raw / key / 绝对路径**（schema 逐字段白名单）；
- 错误统一为 ``{status, code, message, details}``：400（embedding 未就绪 /
  LLM 未配置 / 结构重试超限 / 向量化失败 / 引用校验失败 / 文件格式问题）/
  404（知识块不存在）/ 422（参数校验）。

⚠️ 路由层不写任何 SQLAlchemy 表达式（第 8 步分层纪律，``selftest_api`` 静态断言）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.orm import Session

from app.agent.insights.service import generate_insights
from app.api.deps import get_db
from app.core.errors import bad_request, not_found
from app.crud.knowledge import crud_knowledge
from app.models.enums import SnippetStatus
from app.models.knowledge import KnowledgeSnippet
from app.schemas.common import ErrorOutSpec
from app.schemas.knowledge import (
    ChunkTypeName,
    ExtractResultOut,
    ImportResultOut,
    ImportTextRequest,
    SearchHit,
    SeedForbiddenWordsOut,
    SnippetListOut,
    SnippetManualCreateRequest,
    SnippetOut,
    SnippetSearchOut,
    SnippetSearchRequest,
    SnippetStatusName,
    SnippetUpdateRequest,
    SnippetWriteOut,
)
from app.services.forbidden_source import seed_builtin_words
from app.services.knowledge_admin import (
    create_manual_snippet,
    decode_upload,
    import_document,
    update_snippet_admin,
)
from app.services.rag import search_for_api

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


def _snippet_out(row: KnowledgeSnippet) -> SnippetOut:
    """ORM 行 → 响应对象（**绝不带出 embedding 向量**）。"""
    return SnippetOut(
        id=row.id,
        chunk_type=row.chunk_type.value if row.chunk_type else "general",
        ref_type=row.ref_type.value if row.ref_type else None,
        title=row.title,
        content=row.content,
        meta=row.meta if isinstance(row.meta, dict) else None,
        status=row.status.value if row.status else SnippetStatus.ACTIVE.value,
        created_at=row.created_at,
    )


def _write_out(row: KnowledgeSnippet, warnings: list[str]) -> SnippetWriteOut:
    """写操作响应：知识块 + warnings（**不含 embedding 向量**）。"""
    base = _snippet_out(row)
    return SnippetWriteOut(**base.model_dump(), warnings=list(warnings))


# ---------------------------------------------------------------------------
# POST /extract（静态路径，先注册）
# ---------------------------------------------------------------------------
@router.post(
    "/extract",
    response_model=ExtractResultOut,
    status_code=201,
    summary="跑一次爆款规律提炼（第六个业务图；落 knowledge_snippets）",
    responses={
        **ErrorOutSpec.BAD_REQUEST,
        **ErrorOutSpec.VALIDATION,
    },
)
def extract_knowledge() -> ExtractResultOut:
    """跑一次规律提炼（真实 LLM + 本地向量化；同步，最长约 2 分钟）。

    - embedding 未就绪 / LLM 未配置 → 400（**零请求零运行记录**）；
    - **样本不足不是错误**：``insufficient_sample=True``、零写入，``data_gaps``
      给出「至少发布 N 篇并回填数据后再提炼」；run 记 ``partial``；
    - 结构校验连续超限 → 400（可重试）；
    - 幂等：与既有 active 知识块 ``content_hash`` 相同的规律不重复写入（计 reused）。
    """
    summary = generate_insights()
    return ExtractResultOut(
        run_id=uuid.UUID(str(summary["run_id"])),
        status=str(summary.get("status") or "success"),
        insufficient_sample=bool(summary.get("insufficient_sample")),
        sample_size=summary.get("sample_size"),
        min_required=summary.get("min_required"),
        generated=int(summary.get("generated") or 0),
        persisted=int(summary.get("persisted") or 0),
        reused=int(summary.get("reused") or 0),
        skipped=int(summary.get("skipped") or 0),
        snippet_ids=[str(item) for item in (summary.get("snippet_ids") or [])],
        rejected=int(summary.get("rejected") or 0),
        data_gaps=[str(item) for item in (summary.get("data_gaps") or [])],
        warnings=[str(item) for item in (summary.get("warnings") or [])],
        parse_attempts=summary.get("parse_attempts"),
        prompt_version=summary.get("prompt_version"),
    )


# ---------------------------------------------------------------------------
# POST /search（静态路径，先注册）
# ---------------------------------------------------------------------------
@router.post(
    "/search",
    response_model=SnippetSearchOut,
    summary="检索测试（pgvector 余弦 top-k；含 similarity）",
    responses=ErrorOutSpec.BAD_REQUEST,
)
def search_knowledge(
    payload: SnippetSearchRequest,
    db: Session = Depends(get_db),
) -> SnippetSearchOut:
    """向量检索测试（用于人工验收召回质量）。

    - 向量化未就绪（依赖未装 / 模型未下载）→ 400（如实告知，不假装无命中）；
    - 无命中 → 空列表（正常结果，不是错误）。
    """
    results = search_for_api(
        db,
        query_text=payload.query,
        chunk_types=tuple(payload.chunk_types) if payload.chunk_types else None,
        top_k=payload.k,
    )
    items: list[SearchHit] = []
    for item in results:
        items.append(
            SearchHit(
                id=uuid.UUID(str(item["id"])),
                chunk_type=item.get("chunk_type") or "general",
                title=item.get("title"),
                content=str(item.get("content") or ""),
                meta=item.get("meta") if isinstance(item.get("meta"), dict) else None,
                status=item.get("status") or SnippetStatus.ACTIVE.value,
                created_at=item.get("created_at"),
                similarity=float(item.get("similarity") or 0.0),
            )
        )
    return SnippetSearchOut(query=payload.query, items=items)


# ---------------------------------------------------------------------------
# POST /manual（静态路径，先注册；第 19 步 · 手动创建）
# ---------------------------------------------------------------------------
@router.post(
    "/manual",
    response_model=SnippetWriteOut,
    status_code=201,
    summary="手动创建知识块（forbidden_word 无需向量；其它类型向量化失败即 400）",
    responses={
        **ErrorOutSpec.BAD_REQUEST,
        **ErrorOutSpec.VALIDATION,
    },
)
def create_manual_knowledge(
    payload: SnippetManualCreateRequest,
    db: Session = Depends(get_db),
) -> SnippetWriteOut:
    """人工录入一条知识块（话术 / 风格 / 模板 / 违禁词 / 通用）。

    - ``forbidden_word``：用于精确匹配、**不向量化**（embedding 留空），
      未安装向量化依赖时也可正常维护；``category`` 缺省 ``manual``；
    - 其它类型：经 ``embed_texts`` 向量化，**失败 → 400 且不落库**；
    - ``ref_type=persona``：``ref_id`` 必填且人设必须存在（否则 400）；
    - 向量化未就绪（依赖未装 / 配置缺失）→ 400（**零副作用**）。
    """
    snippet, warnings = create_manual_snippet(
        db,
        chunk_type=payload.chunk_type,
        content=payload.content,
        title=payload.title,
        category=payload.category,
        ref_type=payload.ref_type,
        ref_id=payload.ref_id,
    )
    return _write_out(snippet, warnings)


# ---------------------------------------------------------------------------
# POST /seed-forbidden-words（静态路径，先注册；第 19 步 · 幂等同步内置词表）
# ---------------------------------------------------------------------------
@router.post(
    "/seed-forbidden-words",
    response_model=SeedForbiddenWordsOut,
    summary="幂等同步内置违禁词表（按 content_hash 查重；dry_run 可预检）",
    responses=ErrorOutSpec.VALIDATION,
)
def seed_forbidden_words_route(
    dry_run: bool = Query(False, description="仅预检将写入条数，不写库"),
    db: Session = Depends(get_db),
) -> SeedForbiddenWordsOut:
    """把内置词表写入知识库（``status=active``、``embedding`` 留空）。

    - 幂等：与既有 active 违禁词 ``content_hash`` 相同则跳过（计 ``reused``）；
    - ``dry_run=true``：只返回将写入 / 复用条数，**零写入**。
    """
    result = seed_builtin_words(db, dry_run=dry_run)
    return SeedForbiddenWordsOut(**result)


# ---------------------------------------------------------------------------
# POST /import-text（静态路径，先注册；第 19 步 · 文档导入·文本）
# ---------------------------------------------------------------------------
@router.post(
    "/import-text",
    response_model=ImportResultOut,
    status_code=201,
    summary="文档导入（文本粘贴）：切分 → 逐块向量化 → 单事务入库",
    responses={
        **ErrorOutSpec.BAD_REQUEST,
        **ErrorOutSpec.VALIDATION,
    },
)
def import_text_knowledge(
    payload: ImportTextRequest,
    db: Session = Depends(get_db),
) -> ImportResultOut:
    """把长文本切成知识块入库（默认 ``chunk_size=800`` / ``chunk_overlap=100``）。

    - 切分：官方 ``RecursiveCharacterTextSplitter`` 优先，缺失时内置等效实现；
    - 逐块查重（``(chunk_type, content_hash)``，**先于向量化**）→ 命中计 ``skipped``；
    - 单块向量化失败计 ``failed``（如实计数、不影响其它块，其余块照常入库）；
    - 向量化未就绪 → 400（**零写入**）。
    """
    result = import_document(
        db,
        text=payload.content,
        chunk_type=payload.chunk_type,
        ref_id=payload.ref_id,
        chunk_size=payload.chunk_size,
        chunk_overlap=payload.chunk_overlap,
    )
    return ImportResultOut(**result)


# ---------------------------------------------------------------------------
# POST /import-file（静态路径，先注册；第 19 步 · 文档导入·文件上传）
# ---------------------------------------------------------------------------
@router.post(
    "/import-file",
    response_model=ImportResultOut,
    status_code=201,
    summary="文档导入（.txt / .md 上传）：校验扩展名 / UTF-8 / 大小后与文本导入同流程",
    responses={
        **ErrorOutSpec.BAD_REQUEST,
        **ErrorOutSpec.VALIDATION,
    },
)
def import_file_knowledge(
    file: UploadFile = File(..., description="仅支持 UTF-8 编码的 .txt / .md 文件"),
    chunk_type: str = Form("general", description="general 或 persona_knowledge"),
    ref_id: str | None = Form(None, description="chunk_type=persona_knowledge 时必填"),
    chunk_size: int | None = Form(None, description="切分块大小（字符；缺省 800）"),
    chunk_overlap: int | None = Form(None, description="块间重叠（字符；缺省 100）"),
    db: Session = Depends(get_db),
) -> ImportResultOut:
    """上传单个 ``.txt`` / ``.md`` 文件导入（不做 .docx / .pdf 解析、不做 URL 抓取）。

    - 扩展名 / 大小（≤1MB）/ UTF-8 编码校验，不合法 → 400（明确提示）；
    - 切分 / 查重 / 逐块向量化 / 单事务入库与 ``/import-text`` 完全同流程。
    """
    raw = file.file.read()
    text = decode_upload(raw, filename=file.filename)
    ref_uuid: uuid.UUID | None = None
    if ref_id:
        try:
            ref_uuid = uuid.UUID(str(ref_id))
        except ValueError as exc:
            raise bad_request(
                "ref_id 不是合法的 UUID。", details={"ref_id": str(ref_id)}
            ) from exc
    result = import_document(
        db,
        text=text,
        chunk_type=chunk_type,
        ref_id=ref_uuid,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    return ImportResultOut(**result)


# ---------------------------------------------------------------------------
# GET /（列表）
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=SnippetListOut,
    summary="知识块列表（created_at 倒序）",
)
def list_knowledge(
    chunk_type: ChunkTypeName | None = Query(
        None, description="按知识块类型过滤（缺省 = 全部）"
    ),
    status: SnippetStatusName | None = Query(
        None, description="按状态过滤（缺省 = 全部；常规只看 active）"
    ),
    limit: int = Query(20, ge=1, le=100, description="每页条数，1~100"),
    offset: int = Query(0, ge=0, description="偏移量"),
    db: Session = Depends(get_db),
) -> SnippetListOut:
    """分页列表（``id`` 兜底保证顺序稳定；列表项**不含 embedding 向量**）。"""
    rows, total = crud_knowledge.list_snippets(
        db,
        chunk_type=chunk_type,
        status=status,
        limit=limit,
        offset=offset,
    )
    return SnippetListOut(
        items=[_snippet_out(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# GET /{id}（UUID 路径，最后注册）
# ---------------------------------------------------------------------------
@router.get(
    "/{snippet_id}",
    response_model=SnippetOut,
    summary="知识块详情",
    responses=ErrorOutSpec.NOT_FOUND,
)
def get_knowledge(
    snippet_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> SnippetOut:
    """按主键取单条（不存在 → 404）；**不含 embedding 向量**。"""
    row = crud_knowledge.get_snippet(db, snippet_id)
    if row is None:
        raise not_found("知识块不存在", details={"snippet_id": str(snippet_id)})
    return _snippet_out(row)


# ---------------------------------------------------------------------------
# PATCH /{id}（编辑：status / title / content；无 PUT / DELETE）
# ---------------------------------------------------------------------------
@router.patch(
    "/{snippet_id}",
    response_model=SnippetWriteOut,
    summary="编辑知识块（status 切换 / title / content 重向量化）",
    responses={**ErrorOutSpec.NOT_FOUND, **ErrorOutSpec.VALIDATION},
)
def update_knowledge(
    snippet_id: uuid.UUID,
    payload: SnippetUpdateRequest,
    db: Session = Depends(get_db),
) -> SnippetWriteOut:
    """编辑知识块（无物理删除）。

    - 仅传 ``status``：在 ``active`` / ``archived`` 间切换（与第 18 步行为一致）；
    - ``title`` / ``content``：显式传入才更新；``content`` 变化时重算
      ``meta.content_hash`` 并重新向量化；**重向量化失败 → embedding 置空 +
      ``warnings`` 提示，正文照常保存（不阻断、不丢正文）**。
    """
    row, warnings = update_snippet_admin(
        db,
        snippet_id,
        status=payload.status,
        title=payload.title,
        content=payload.content,
    )
    if row is None:
        raise not_found("知识块不存在", details={"snippet_id": str(snippet_id)})
    return _write_out(row, warnings)
