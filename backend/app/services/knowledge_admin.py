"""知识库人工维护服务（第 19 步）：手动创建 / 编辑重向量化 / 文档导入。

职责与边界：

- :func:`create_manual_snippet`：人工录入知识块的**唯一入口** ——
  ``forbidden_word`` 用于精确匹配、**不强制向量**（embedding 留空，离线可维护）；
  其它类型默认经 ``embed_texts`` 向量化，**向量化失败 → 400 且不落库**
  （不静默丢向量）；``ref_type=persona`` 时校验人设存在（不存在 → 400）；
- :func:`update_snippet_admin`：PATCH 扩展 —— 仅改 status 时行为与第 18 步一致；
  编辑 ``title`` / ``content`` 时：``content`` 变化则重算 ``meta.content_hash`` 并
  **重新向量化**；重向量化失败 → ``embedding`` 置空 + ``warnings`` 提示，
  **正文照常保存（不阻断、不丢正文）**；
- :func:`import_document`：文档导入 —— ``doc_splitter`` 切分 → 逐块按
  ``(chunk_type, content_hash)`` **查重先于向量化** → 单事务批量入库；
  总块数 / 写入数 / 跳过数 / 失败数如实计数，**部分块失败不影响其它块**；
- :func:`decode_upload`：上传文件校验（扩展名 / 大小 / UTF-8 编码）。

红线：响应与日志不含 embedding 原始向量 / llm_raw / API key / 服务端绝对路径
（异常原因经 :func:`_safe_reason` 过滤）；只增 / 归档知识块，无物理删除。
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.agent.embedding.base import EmbeddingError
from app.agent.embedding.service import (
    describe_embedding_blocker,
    embed_texts,
)
from app.agent.tracing.recorder import redact_secrets
from app.core.errors import bad_request
from app.crud.knowledge import crud_knowledge
from app.crud.persona import crud_persona
from app.models.enums import ChunkType, RefType, SnippetStatus
from app.models.knowledge import KnowledgeSnippet
from app.services import doc_splitter
from app.services.forbidden_source import DEFAULT_CATEGORY, word_hash

logger = logging.getLogger("xhs-agent.services.knowledge_admin")

#: 单条知识块正文长度上限（字符）
CONTENT_MAX_LEN = 20000

#: 标题长度上限（字符；与列 String(200) 一致）
TITLE_MAX_LEN = 200

#: 违禁词类别长度上限
CATEGORY_MAX_LEN = 64

#: 文档导入的单次文本长度上限（字符）
IMPORT_TEXT_MAX_CHARS = 200000

#: 上传文件字节上限（1MB）
MAX_IMPORT_FILE_BYTES = 1_000_000

#: 允许上传的扩展名
ALLOWED_UPLOAD_SUFFIXES = (".txt", ".md")

#: 允许导入的目标类型（任务书：默认 general，或 persona_knowledge 关联人设）
ALLOWED_IMPORT_CHUNK_TYPES = (
    ChunkType.GENERAL.value,
    ChunkType.PERSONA_KNOWLEDGE.value,
)

#: 绝对路径过滤（防服务端路径外泄到错误详情）
_WIN_PATH = re.compile(r"[A-Za-z]:\\[^\s\"']*")
_POSIX_HOME_PATH = re.compile(r"(?<![\w:/])/(?:Users|home|tmp|var|opt|root)[^\s\"']*")


def _safe_reason(exc: Exception) -> str:
    """异常 → 可安全外露的原因文本（脱敏 + 去绝对路径 + 截断）。"""
    text = str(redact_secrets(str(exc)))
    text = _WIN_PATH.sub("[路径已隐藏]", text)
    text = _POSIX_HOME_PATH.sub("[路径已隐藏]", text)
    return text[:300]


def _content_hash(text: str) -> str:
    """通用知识块指纹：正文原文的 sha256（与第 18 步规律块同口径）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 前置闸门
# ---------------------------------------------------------------------------
def _ensure_embedding_ready() -> None:
    """向量化就绪检查（**不加载模型 / 不发任何请求 / 零副作用**）。

    未就绪 → 400 + 安装 / 配置指引；调用方必须在「写库 / 向量化」之前执行。
    """
    blocker = describe_embedding_blocker()
    if blocker:
        raise bad_request("向量化未就绪，无法完成本次操作。", details=blocker)


# ---------------------------------------------------------------------------
# 引用校验
# ---------------------------------------------------------------------------
def _resolve_ref(
    db: Session,
    *,
    ref_type: str | None,
    ref_id: uuid.UUID | None,
) -> tuple[RefType | None, uuid.UUID | None]:
    """把请求里的 ``ref_type`` / ``ref_id`` 归一并做必要校验。

    - 两者都缺省 → ``(None, None)``；
    - ``persona``：``ref_id`` 必填且**人设必须存在**（否则 400）；
    - ``manual_doc``：弱关联，``ref_id`` 可选（手工文档无实体）；
    - 只给 ``ref_id`` 不给 ``ref_type`` → 400（无法判断关联语义）。
    """
    raw_type = (ref_type or "").strip() or None
    if raw_type is None:
        if ref_id is not None:
            raise bad_request("提供了 ref_id 就必须提供 ref_type。")
        return None, None
    try:
        ref_enum = RefType(raw_type)
    except ValueError as exc:
        raise bad_request(
            f"未知的 ref_type：{raw_type!r}",
            details={"allowed": [item.value for item in RefType]},
        ) from exc

    if ref_enum == RefType.PERSONA:
        if ref_id is None:
            raise bad_request("ref_type=persona 时必须提供 ref_id（人设 id）。")
        persona = crud_persona.get(db, ref_id)
        if persona is None:
            raise bad_request(
                "关联的人设不存在。",
                details={"ref_type": "persona", "ref_id": str(ref_id)},
            )
        return ref_enum, persona.id

    # manual_doc（及其它弱关联）：ref_id 可选，不做存在性校验
    return ref_enum, ref_id


# ---------------------------------------------------------------------------
# 手动创建
# ---------------------------------------------------------------------------
def create_manual_snippet(
    db: Session,
    *,
    chunk_type: str,
    content: str,
    title: str | None = None,
    category: str | None = None,
    ref_type: str | None = None,
    ref_id: uuid.UUID | None = None,
    embedder: Any | None = None,
) -> tuple[KnowledgeSnippet, list[str]]:
    """人工录入一条知识块（**唯一创建入口**）。

    - ``forbidden_word``：``meta={"category", "content_hash": word_hash(词)}``，
      **不向量化**（精确匹配无需向量；离线可维护）；
    - 其它类型：``meta={"content_hash": sha256(正文)}``，经 ``embed_texts`` 向量化，
      **失败 → 400 不落库**（``embedder=None`` 时先做就绪闸门，零副作用）。

    Returns:
        ``(snippet, warnings)``；创建路径 warning 恒为空（失败直接 400）。

    Raises:
        AppError(400)：向量化未就绪 / 向量化失败 / 参数或引用非法。
    """
    content_text = str(content or "").strip()
    if not content_text:
        raise bad_request("知识块正文不能为空。")
    if len(content_text) > CONTENT_MAX_LEN:
        raise bad_request(f"知识块正文超过 {CONTENT_MAX_LEN} 字上限。")

    title_text = (title or "").strip() or None
    if title_text and len(title_text) > TITLE_MAX_LEN:
        raise bad_request(f"标题超过 {TITLE_MAX_LEN} 字上限。")

    try:
        chunk_enum = ChunkType(chunk_type)
    except ValueError as exc:
        raise bad_request(
            f"未知的 chunk_type：{chunk_type!r}",
            details={"allowed": [item.value for item in ChunkType]},
        ) from exc

    ref_type_value, ref_id_value = _resolve_ref(db, ref_type=ref_type, ref_id=ref_id)
    if (
        chunk_enum == ChunkType.PERSONA_KNOWLEDGE
        and ref_type_value != RefType.PERSONA
    ):
        raise bad_request(
            "persona_knowledge 必须关联人设（ref_type=persona + ref_id）。"
            "未关联人设的块不会被任何反哺检索纳入。"
        )

    embedding: list[float] | None = None
    if chunk_enum == ChunkType.FORBIDDEN_WORD:
        category_text = (category or "").strip() or DEFAULT_CATEGORY
        if len(category_text) > CATEGORY_MAX_LEN:
            raise bad_request(f"类别超过 {CATEGORY_MAX_LEN} 字上限。")
        meta: dict[str, Any] = {
            "category": category_text,
            "content_hash": word_hash(content_text),
        }
    else:
        meta = {"content_hash": _content_hash(content_text), "origin": "manual"}
        if embedder is None:
            _ensure_embedding_ready()
        try:
            embedding = embed_texts([content_text], embedder=embedder)[0]
        except EmbeddingError as exc:
            raise bad_request(
                "知识块向量化失败，未写入。",
                details={
                    "reason": _safe_reason(exc),
                    "hint": "请确认已安装向量化依赖并完成模型下载"
                    "（可运行 scripts/check_embedding_local.py 预检）；"
                    "违禁词（forbidden_word）无需向量，可离线维护。",
                },
            ) from exc

    row: dict[str, Any] = {
        "chunk_type": chunk_enum,
        "ref_type": ref_type_value,
        "ref_id": ref_id_value,
        "title": title_text,
        "content": content_text,
        "embedding": embedding,
        "meta": meta,
    }
    snippet = crud_knowledge.insert_many(db, [row])[0]
    return snippet, []


# ---------------------------------------------------------------------------
# 编辑（PATCH 扩展）
# ---------------------------------------------------------------------------
def update_snippet_admin(
    db: Session,
    snippet_id: uuid.UUID,
    *,
    status: str | None = None,
    title: str | None = None,
    content: str | None = None,
    embedder: Any | None = None,
) -> tuple[KnowledgeSnippet | None, list[str]]:
    """编辑知识块：``status``（与第 18 步一致）/ ``title`` / ``content``。

    ``content`` 变化的处理（**不阻断、不丢正文**）：

    - ``forbidden_word``：重算 ``meta.content_hash = word_hash(新正文)``，不动 embedding
      （精确匹配无需向量，保持留空口径）；
    - 其它类型：重新向量化 → 成功则更新 embedding；**失败则置空 embedding + warning**，
      正文与 content_hash 照常保存（检索暂不可用，可再次编辑重试）。

    Returns:
        ``(snippet, warnings)``；行不存在 → ``(None, [])``（路由层翻译 404）。
    """
    row = crud_knowledge.get_snippet(db, snippet_id)
    if row is None:
        return None, []

    updates: dict[str, Any] = {}
    warnings: list[str] = []
    status_enum: SnippetStatus | None = None

    if status is not None:
        # 合法域校验在写库之前完成（与第 18/19 步一致：status 只经显式参数传入，
        # 不混入 update_fields 白名单 —— 状态失效仍可被断言发现）
        status_enum = SnippetStatus(status)

    if title is not None:
        title_text = title.strip()
        if len(title_text) > TITLE_MAX_LEN:
            raise bad_request(f"标题超过 {TITLE_MAX_LEN} 字上限。")
        updates["title"] = title_text or None

    if content is not None:
        content_text = content.strip()
        if not content_text:
            raise bad_request("知识块正文不能为空。")
        if len(content_text) > CONTENT_MAX_LEN:
            raise bad_request(f"知识块正文超过 {CONTENT_MAX_LEN} 字上限。")
        if content_text != row.content:
            chunk_value = row.chunk_type.value if row.chunk_type else "general"
            old_meta = row.meta if isinstance(row.meta, dict) else {}
            new_meta = dict(old_meta)
            if chunk_value == ChunkType.FORBIDDEN_WORD.value:
                new_meta["content_hash"] = word_hash(content_text)
            else:
                new_meta["content_hash"] = _content_hash(content_text)
                try:
                    updates["embedding"] = embed_texts(
                        [content_text], embedder=embedder
                    )[0]
                except EmbeddingError as exc:
                    updates["embedding"] = None
                    warnings.append(
                        "向量重新化失败：已保存正文并置空向量（检索暂不可用）。"
                        f"原因类型：{type(exc).__name__}；"
                        "请确认向量化依赖与模型就绪后再次编辑以重试。"
                    )
            updates["content"] = content_text
            updates["meta"] = new_meta

    if status_enum is not None or updates:
        # 第 20 步收口：状态切换与字段更新合并为【单事务】提交
        # （原先 set_status / update_fields 分两次提交，存在半成品窗口）
        row = crud_knowledge.apply_admin_update(
            db, snippet_id, status=status_enum, values=updates
        )
        if row is None:
            return None, []
    return row, warnings


# ---------------------------------------------------------------------------
# 文档导入
# ---------------------------------------------------------------------------
def decode_upload(raw: bytes, *, filename: str | None) -> str:
    """校验并解码上传文件（扩展名 / 大小 / UTF-8 编码），返回文本。

    Raises:
        AppError(400)：非 .txt/.md / 空文件 / 超大 / 非 UTF-8 编码。
    """
    name = str(filename or "").strip()
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        raise bad_request(
            "仅支持 .txt / .md 文本文件（本步不做 .docx / .pdf 解析）。",
            details={"filename": name, "allowed": list(ALLOWED_UPLOAD_SUFFIXES)},
        )
    if not raw:
        raise bad_request("文件内容为空。", details={"filename": name})
    if len(raw) > MAX_IMPORT_FILE_BYTES:
        raise bad_request(
            f"文件过大（{len(raw)} 字节 > {MAX_IMPORT_FILE_BYTES} 字节上限）。",
            details={"filename": name},
        )
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise bad_request(
            "文件不是 UTF-8 编码，已拒绝。",
            details={
                "filename": name,
                "hint": "请用 UTF-8 编码另存后重试（不支持 GBK 等其他编码）。",
            },
        ) from exc


def import_document(
    db: Session,
    *,
    text: str,
    chunk_type: str = ChunkType.GENERAL.value,
    ref_id: uuid.UUID | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    embedder: Any | None = None,
) -> dict[str, Any]:
    """文本 → 切分 → 逐块查重 → 逐块向量化 → 单事务批量入库。

    - 切分：``doc_splitter.split_text``（官方 RecursiveCharacterTextSplitter 优先，
      缺失时内置等效实现；参数默认 800 / 100，可覆盖并做边界校验）；
    - ``chunk_type``：``general``（默认）或 ``persona_knowledge``
      （后者必须带 ``ref_id`` 人设，校验存在性）；
    - 查重**先于向量化**（``(chunk_type, content_hash)`` 命中 active 即跳过，计 skipped；
      批内重复同样计 skipped）；
    - 向量化**逐块**进行：单块失败计 failed（如实计数、不影响其它块）；
    - 成功的块**单事务**批量入库（``insert_many``，失败整体回滚）。

    Returns:
        ``{total_chunks, inserted, skipped, failed, snippet_ids, warnings,
        chunk_size, chunk_overlap, splitter}``。
    """
    content_text = str(text or "")
    if not content_text.strip():
        raise bad_request("导入内容为空。")
    if len(content_text) > IMPORT_TEXT_MAX_CHARS:
        raise bad_request(
            f"导入文本超过 {IMPORT_TEXT_MAX_CHARS} 字上限，请拆分后分次导入。"
        )

    chunk_value = str(chunk_type or ChunkType.GENERAL.value)
    if chunk_value not in ALLOWED_IMPORT_CHUNK_TYPES:
        raise bad_request(
            f"导入不支持的类型：{chunk_value!r}",
            details={"allowed": list(ALLOWED_IMPORT_CHUNK_TYPES)},
        )
    chunk_enum = ChunkType(chunk_value)

    ref_type_value: RefType | None = None
    ref_id_value: uuid.UUID | None = None
    if chunk_enum == ChunkType.PERSONA_KNOWLEDGE:
        if ref_id is None:
            raise bad_request("导入人设知识必须提供 ref_id（人设 id）。")
        ref_type_value, ref_id_value = _resolve_ref(
            db, ref_type=RefType.PERSONA.value, ref_id=ref_id
        )

    # 切分参数校验（非法 → 400）
    try:
        size, overlap = doc_splitter.resolve_params(chunk_size, chunk_overlap)
    except ValueError as exc:
        raise bad_request(str(exc)) from exc

    pieces = doc_splitter.split_text(
        content_text, chunk_size=size, chunk_overlap=overlap
    )
    if not pieces:
        raise bad_request("导入内容切分后为空（可能全是空白字符）。")

    # 就绪闸门：任何写库 / 向量化之前（未就绪 → 400 零副作用）
    if embedder is None:
        _ensure_embedding_ready()

    pending: list[dict[str, Any]] = []
    snippet_hashes: set[str] = set()
    skipped = 0
    failed = 0
    warnings: list[str] = []

    for index, piece in enumerate(pieces, start=1):
        digest = _content_hash(piece)
        if digest in snippet_hashes:
            skipped += 1  # 批内重复
            continue
        snippet_hashes.add(digest)

        existing = crud_knowledge.find_active_by_hash(
            db, chunk_type=chunk_enum, content_hash=digest
        )
        if existing is not None:
            skipped += 1  # 库中已存在（幂等）
            continue

        try:
            vector = embed_texts([piece], embedder=embedder)[0]
        except EmbeddingError as exc:
            failed += 1
            warnings.append(
                f"第 {index} 块向量化失败（已跳过，未写入）：{type(exc).__name__}"
            )
            continue

        pending.append(
            {
                "chunk_type": chunk_enum,
                "ref_type": ref_type_value,
                "ref_id": ref_id_value,
                "content": piece,
                "embedding": vector,
                "meta": {"content_hash": digest, "origin": "import"},
            }
        )

    created: list[KnowledgeSnippet] = []
    if pending:
        created = crud_knowledge.insert_many(db, pending)

    return {
        "total_chunks": len(pieces),
        "inserted": len(created),
        "skipped": skipped,
        "failed": failed,
        "snippet_ids": [str(item.id) for item in created],
        "warnings": warnings,
        "chunk_size": size,
        "chunk_overlap": overlap,
        "splitter": doc_splitter.describe_splitter()["impl"],
    }


__all__ = [
    "ALLOWED_IMPORT_CHUNK_TYPES",
    "ALLOWED_UPLOAD_SUFFIXES",
    "CATEGORY_MAX_LEN",
    "CONTENT_MAX_LEN",
    "IMPORT_TEXT_MAX_CHARS",
    "MAX_IMPORT_FILE_BYTES",
    "TITLE_MAX_LEN",
    "create_manual_snippet",
    "decode_upload",
    "import_document",
    "update_snippet_admin",
]
