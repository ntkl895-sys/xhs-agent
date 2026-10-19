"""knowledge_snippets 的 CRUD（第 18 步 · RAG 知识块）。

职责与边界：

- ``insert_many``：规律落库的**唯一写入入口**（单事务；失败整体回滚不留半截记录）；
- ``find_active_by_hash``：幂等查重 —— 按 ``(chunk_type, meta.content_hash)`` 找
  **active** 记录（重复提炼不新增）；
- ``set_status``：仅允许在 ``active`` / ``archived`` 间切换（归档 / 恢复）；
  **无物理删除、无 PUT / DELETE**；
- ``list_snippets`` / ``get_snippet``：列表（分页 + 类型 / 状态过滤，``created_at``
  倒序 + ``id`` 兜底）与详情；
- ``search_snippets``：pgvector 余弦 top-k（``embedding <=> :qvec``，走 HNSW；
  查询事务内 ``SET LOCAL hnsw.ef_search``）；**只返回相似度分数，不返回原始向量**；
  ``query_text`` 时经 embedding 服务向量化（异常向上抛可识别错误，由调用方降级）。

红线：本层不触碰 notes / topics / note_metrics / diagnostics；不做任何删除。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.crud.base import CRUDBase
from app.crud.retrieval_fusion import rrf_fuse
from app.models.enums import ChunkType, SnippetStatus
from app.models.knowledge import KnowledgeSnippet

logger = logging.getLogger("xhs-agent.crud.knowledge")

#: 支持的检索模式（新增模式必须同步这里，避免拼写错误被静默接受）
_RETRIEVAL_MODES = ("vector", "hybrid")

#: HNSW 查询阶段的 ef_search（事务级；越大召回越好、越慢。40 为经验值）
_HNSW_EF_SEARCH = 40

#: 检索返回条数上限（防御性；正常由接口层校验 1~20）
_SEARCH_MAX_K = 50


class CRUDKnowledge(CRUDBase[KnowledgeSnippet]):
    """知识块 CRUD（只增 / 只读 / 归档；无物理删除）。"""

    #: 「归档」状态值（CRUDBase.update 依赖）
    archived_status = SnippetStatus.ARCHIVED

    def __init__(self) -> None:
        super().__init__(KnowledgeSnippet)

    # ------------------------------------------------------------------ 写入
    def insert_many(
        self, db: Session, rows: list[dict[str, Any]]
    ) -> list[KnowledgeSnippet]:
        """**单事务**批量插入知识块；任一条失败整体回滚。

        Returns:
            已刷新（含服务器默认值）的 ORM 对象列表。
        """
        objs = [KnowledgeSnippet(**row) for row in rows]
        try:
            db.add_all(objs)
            db.flush()  # 通过 RETURNING 取回 gen_random_uuid() 主键
            db.commit()
        except Exception:
            db.rollback()
            raise
        for obj in objs:
            db.refresh(obj)
        return objs

    def find_active_by_hash(
        self,
        db: Session,
        *,
        chunk_type: ChunkType,
        content_hash: str,
    ) -> KnowledgeSnippet | None:
        """按 ``(chunk_type, meta.content_hash)`` 找 **active** 记录（幂等查重）。

        顺序确定（``created_at`` 升序 + ``id`` 升序），取第一条。
        """
        stmt = (
            select(KnowledgeSnippet)
            .where(
                KnowledgeSnippet.chunk_type == chunk_type,
                KnowledgeSnippet.status == SnippetStatus.ACTIVE,
                KnowledgeSnippet.meta["content_hash"].astext == str(content_hash),
            )
            .order_by(KnowledgeSnippet.created_at.asc(), KnowledgeSnippet.id.asc())
            .limit(1)
        )
        return db.execute(stmt).scalars().first()

    def set_status(
        self,
        db: Session,
        snippet_id: uuid.UUID,
        *,
        status: SnippetStatus,
    ) -> KnowledgeSnippet | None:
        """在 ``active`` / ``archived`` 间切换（归档 / 恢复）；不存在 → ``None``。"""
        obj = self.get(db, snippet_id)
        if obj is None:
            return None
        obj.status = status
        db.commit()
        db.refresh(obj)
        return obj

    def update_fields(
        self,
        db: Session,
        snippet_id: uuid.UUID,
        values: dict[str, Any],
    ) -> KnowledgeSnippet | None:
        """白名单字段更新（第 19 步：``title`` / ``content`` / ``embedding`` / ``meta``）。

        - 只更新白名单内且**显式传入**的键（``None`` 也会写入 —— 「置空向量」依赖此语义）；
        - 不改 ``status``（状态切换走 :meth:`set_status`）、不改 ``chunk_type`` / ``ref_*``；
        - 不存在 → ``None``（不新建、不报错）。
        """
        allowed = {"title", "content", "embedding", "meta"}
        updates = {key: value for key, value in values.items() if key in allowed}
        obj = self.get(db, snippet_id)
        if obj is None:
            return None
        for key, value in updates.items():
            setattr(obj, key, value)
        db.commit()
        db.refresh(obj)
        return obj

    def apply_admin_update(
        self,
        db: Session,
        snippet_id: uuid.UUID,
        *,
        status: SnippetStatus | None = None,
        values: dict[str, Any] | None = None,
    ) -> KnowledgeSnippet | None:
        """**单事务**合并更新：状态切换 + 白名单字段（第 20 步收口）。

        第 19 步的 PATCH 在「同时携带 status 与其它字段」时分为两次提交
        （``set_status`` 一次、``update_fields`` 一次），进程中断 / 写库异常
        可能留下「状态已变、正文未变」的半成品。本方法把两者合并为一次提交：

        - ``values`` 白名单与 :meth:`update_fields` 相同（``title`` / ``content`` /
          ``embedding`` / ``meta``；``None`` 照常写入，「置空向量」语义不变）；
        - ``status`` 只经显式参数传入（合法域与 :meth:`set_status` 一致），
          **不**混入 values 白名单 —— 状态失效仍可被断言发现，语义与第 18/19 步一致；
        - 任一失败整体 rollback；不存在 → ``None``。
        """
        allowed = {"title", "content", "embedding", "meta"}
        updates = {key: value for key, value in (values or {}).items() if key in allowed}
        obj = self.get(db, snippet_id)
        if obj is None:
            return None
        try:
            if status is not None:
                obj.status = status
            for key, value in updates.items():
                setattr(obj, key, value)
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(obj)
        return obj

    def list_active_snippets(
        self,
        db: Session,
        *,
        chunk_type: ChunkType | str,
        ref_id: uuid.UUID | None = None,
        limit: int = 200,
    ) -> list[KnowledgeSnippet]:
        """按类型取 **active** 全量（可选按 ``ref_id`` 过滤）；**不是**向量检索。

        第 19 步三个调用方共用：

        - 违禁词扫描（``chunk_type=forbidden_word``，全量精确匹配用）；
        - seed 幂等查重（取 active 违禁词的 ``meta.content_hash`` 集合）；
        - 人设知识纳入（``chunk_type=persona_knowledge`` + ``ref_id=persona_id``）。

        ``chunk_type`` 支持枚举成员或字符串值（``str, Enum`` 混合类的
        ``str(member)`` 不返回值，须先归一）。顺序确定：``created_at`` 升序 +
        ``id`` 升序（先建先出，稳定可复现）。
        """
        chunk_value = (
            chunk_type.value if isinstance(chunk_type, ChunkType) else str(chunk_type)
        )
        criteria: list[Any] = [
            KnowledgeSnippet.chunk_type == ChunkType(chunk_value),
            KnowledgeSnippet.status == SnippetStatus.ACTIVE,
        ]
        if ref_id is not None:
            criteria.append(KnowledgeSnippet.ref_id == ref_id)
        stmt = (
            select(KnowledgeSnippet)
            .where(*criteria)
            .order_by(KnowledgeSnippet.created_at.asc(), KnowledgeSnippet.id.asc())
            .limit(max(1, min(int(limit), 500)))
        )
        return list(db.execute(stmt).scalars().all())

    # ------------------------------------------------------------------ 查询
    def list_snippets(
        self,
        db: Session,
        *,
        chunk_type: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[KnowledgeSnippet], int]:
        """分页列表（``created_at`` 倒序 + ``id`` 兜底保证顺序稳定）。

        Returns:
            ``(rows, total)`` —— ``total`` 为**过滤后的总数**（不受分页影响）。
        """
        criteria: list[Any] = []
        if chunk_type is not None:
            criteria.append(KnowledgeSnippet.chunk_type == ChunkType(str(chunk_type)))
        if status is not None:
            criteria.append(KnowledgeSnippet.status == SnippetStatus(str(status)))

        total = int(
            db.execute(
                select(func.count()).select_from(KnowledgeSnippet).where(*criteria)
            ).scalar_one()
        )
        stmt = (
            select(KnowledgeSnippet)
            .where(*criteria)
            .order_by(KnowledgeSnippet.created_at.desc(), KnowledgeSnippet.id.desc())
            .offset(max(0, int(offset)))
            .limit(max(1, int(limit)))
        )
        return list(db.execute(stmt).scalars().all()), total

    def get_snippet(
        self, db: Session, snippet_id: uuid.UUID
    ) -> KnowledgeSnippet | None:
        """按主键取单条，不存在返回 ``None``。"""
        return self.get(db, snippet_id)

    # ------------------------------------------------------------------ 检索
    def search_snippets(
        self,
        db: Session,
        *,
        query_vec: list[float] | None = None,
        query_text: str | None = None,
        chunk_types: tuple[str, ...] | list[str] | None = None,
        k: int = 5,
        include_archived: bool = False,
        retrieval_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        """检索知识块：按 ``retrieval_mode`` 分派到纯向量或混合检索。

        Args:
            query_vec: 已算好的查询向量（与库内向量同维度）。
            query_text: 查询文本（``query_vec`` 为空时必填）。
            chunk_types: 限定知识块类型；``None`` / 空 → 不限定。
            k: 返回条数（1~50，超出截断）。
            include_archived: ``False``（默认）只检索 ``status='active'``。
            retrieval_mode: ``vector``（默认，与历史行为完全一致）/ ``hybrid``
                （向量 + pg_trgm 关键字两路，RRF 融合）；``None`` 时取
                ``settings.RAG_RETRIEVAL_MODE``。

        Returns:
            与 :meth:`_search_vector` 同构的命中列表（8 键，**不含原始向量**）。

        Raises:
            ValueError: 模式取值非法（不静默兜底，避免配置写错却以为生效）。
        """
        mode = (
            str(retrieval_mode or settings.RAG_RETRIEVAL_MODE or "vector").strip().lower()
        )
        if mode not in _RETRIEVAL_MODES:
            raise ValueError(
                f"retrieval_mode 只支持 {list(_RETRIEVAL_MODES)}，收到 {mode!r}"
            )
        if mode == "hybrid":
            return self._search_hybrid(
                db,
                query_vec=query_vec,
                query_text=query_text,
                chunk_types=chunk_types,
                k=k,
                include_archived=include_archived,
            )
        return self._search_vector(
            db,
            query_vec=query_vec,
            query_text=query_text,
            chunk_types=chunk_types,
            k=k,
            include_archived=include_archived,
        )

    def _search_vector(
        self,
        db: Session,
        *,
        query_vec: list[float] | None = None,
        query_text: str | None = None,
        chunk_types: tuple[str, ...] | list[str] | None = None,
        k: int = 5,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """pgvector 余弦 top-k 检索（``embedding <=> :qvec``，走 HNSW）。

        Args:
            query_vec: 已算好的查询向量（与库内向量同维度）；``None`` 时用
                ``query_text`` 经 embedding 服务向量化。
            query_text: 查询文本（``query_vec`` 为空时必填）。
            chunk_types: 限定知识块类型（本步典型：hit_pattern / review_insight）；
                ``None`` / 空 → 不限定。
            k: 返回条数（1~50，超出截断）。
            include_archived: ``False``（默认）只检索 ``status='active'``；
                ``True`` 时不过滤状态（含归档）。

        Returns:
            命中列表（按余弦距离升序 = 相似度降序；``created_at`` 倒序兜底）：
            ``{id, chunk_type, title, content, meta, status, created_at, similarity}``
            —— **绝不返回 embedding 原始向量**。
        """
        if query_vec is None:
            if not (query_text or "").strip():
                raise ValueError("search_snippets 需要 query_vec 或非空 query_text")
            # 函数内 import：避免 crud 与 agent.embedding 的循环依赖
            from app.agent.embedding.service import embed_texts

            query_vec = embed_texts([str(query_text)])[0]

        vector = [float(value) for value in query_vec]

        # HNSW 查询阶段的 ef_search（事务级；仅影响本次查询）
        try:
            db.execute(sa_text(f"SET LOCAL hnsw.ef_search = {int(_HNSW_EF_SEARCH)}"))
        except Exception:  # noqa: BLE001 —— 参数不被支持时忽略（走默认值）
            db.rollback()

        distance = KnowledgeSnippet.embedding.cosine_distance(vector)
        stmt = select(KnowledgeSnippet, distance.label("distance")).where(
            KnowledgeSnippet.embedding.is_not(None)
        )
        if not include_archived:
            stmt = stmt.where(KnowledgeSnippet.status == SnippetStatus.ACTIVE)
        if chunk_types:
            stmt = stmt.where(
                KnowledgeSnippet.chunk_type.in_(
                    [ChunkType(str(item)) for item in chunk_types]
                )
            )
        stmt = (
            stmt.order_by(
                distance.asc(),
                KnowledgeSnippet.created_at.desc(),
                KnowledgeSnippet.id.desc(),
            )
            .limit(max(1, min(int(k), _SEARCH_MAX_K)))
        )

        results: list[dict[str, Any]] = []
        for row, dist in db.execute(stmt).all():
            raw_distance = float(dist)
            similarity = max(-1.0, min(1.0, 1.0 - raw_distance))  # 数值稳定
            results.append(
                {
                    "id": str(row.id),
                    "chunk_type": row.chunk_type.value if row.chunk_type else None,
                    "title": row.title,
                    "content": row.content,
                    "meta": row.meta if isinstance(row.meta, dict) else {},
                    "status": row.status.value if row.status else None,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "similarity": round(similarity, 4),
                }
            )
        return results

    # ------------------------------------------------- 混合检索（第 24 步新增）
    def _search_hybrid(
        self,
        db: Session,
        *,
        query_vec: list[float] | None,
        query_text: str | None,
        chunk_types: tuple[str, ...] | list[str] | None,
        k: int,
        include_archived: bool,
    ) -> list[dict[str, Any]]:
        """向量 + pg_trgm 关键字两路召回，RRF 融合后取前 ``k`` 条。

        降级策略（任一步不可用都不抛错，保证主链路始终可用）：

        - 没有 ``query_text`` → 关键字路无从下手，退回纯向量；
        - ``pg_trgm`` 扩展不可用 → 退回纯向量（不新增迁移，运行时幂等创建）；
        - 关键字路零命中 → 直接返回纯向量结果（融合不会比单路更差）。

        注意：pg_trgm 给的是**三元组相似度**，不是 BM25（无 IDF、无词频饱和），
        报告与接口说明都不应写作 BM25。
        """
        text = str(query_text or "").strip()
        limit = max(1, min(int(k), _SEARCH_MAX_K))
        if not text:
            logger.info("hybrid 模式缺少 query_text，已降级为纯向量检索")
            return self._search_vector(
                db,
                query_vec=query_vec,
                query_text=query_text,
                chunk_types=chunk_types,
                k=k,
                include_archived=include_archived,
            )

        pool = max(limit, int(settings.RAG_HYBRID_POOL))
        vector_hits = self._search_vector(
            db,
            query_vec=query_vec,
            query_text=text,
            chunk_types=chunk_types,
            k=pool,
            include_archived=include_archived,
        )
        if not self._ensure_trgm(db):
            logger.info("pg_trgm 不可用，hybrid 已降级为纯向量检索")
            return vector_hits[:limit]

        trgm_hits = self._search_trgm(
            db,
            text=text,
            k=pool,
            chunk_types=chunk_types,
            include_archived=include_archived,
        )
        if not trgm_hits:
            return vector_hits[:limit]

        fused = rrf_fuse(
            [
                [item["id"] for item in vector_hits],
                [item["id"] for item in trgm_hits],
            ],
            k=int(settings.RAG_RRF_K),
            top_n=limit,
        )

        # 向量路优先（similarity 语义是余弦）；只在关键字路出现的条目用 trgm 分数
        by_id: dict[str, dict[str, Any]] = {}
        for item in trgm_hits:
            by_id[item["id"]] = item
        for item in vector_hits:
            by_id[item["id"]] = item

        merged: list[dict[str, Any]] = []
        for item in fused:
            row = by_id.get(item.doc_id)
            if row is not None:
                merged.append(row)
        return merged

    @staticmethod
    def _ensure_trgm(db: Session) -> bool:
        """幂等确保 ``pg_trgm`` 扩展可用；失败返回 ``False`` 由调用方降级。

        不新增 Alembic 迁移（保持「零迁移基线」）：扩展不是表结构，且缺失时
        自动降级为纯向量，不会让主链路报错。
        """
        try:
            installed = db.execute(
                sa_text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
            ).scalar()
            if installed:
                return True
            # 用 SAVEPOINT 隔离：建扩展失败不会把外层事务打成 aborted
            with db.begin_nested():
                db.execute(sa_text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            return True
        except Exception as exc:  # noqa: BLE001 —— 任何原因不可用都降级，不阻断检索
            logger.warning("pg_trgm 不可用（%s）；hybrid 降级为纯向量", exc)
            return False

    def _search_trgm(
        self,
        db: Session,
        *,
        text: str,
        k: int,
        chunk_types: tuple[str, ...] | list[str] | None,
        include_archived: bool,
    ) -> list[dict[str, Any]]:
        """关键字路：pg_trgm ``word_similarity`` 取 top-k（**不是 BM25**）。

        用 ``word_similarity(query, title+content)`` 而不是 ``similarity``：
        后者分母含文档长度，短 query 对长文档天然吃亏，几乎恒低于默认阈值。
        ``word_similarity`` 取「query 的 trigram 在文档任意连续片段中的最大覆盖率」，
        对短 query 更合适。
        """
        haystack = func.concat(
            func.coalesce(KnowledgeSnippet.title, ""), " ", KnowledgeSnippet.content
        )
        similarity = func.word_similarity(text, haystack)

        stmt = (
            select(KnowledgeSnippet, similarity.label("trgm_similarity"))
            .where(KnowledgeSnippet.embedding.is_not(None))
            .where(similarity > 0)
        )
        if not include_archived:
            stmt = stmt.where(KnowledgeSnippet.status == SnippetStatus.ACTIVE)
        if chunk_types:
            stmt = stmt.where(
                KnowledgeSnippet.chunk_type.in_(
                    [ChunkType(str(item)) for item in chunk_types]
                )
            )
        stmt = (
            stmt.order_by(
                similarity.desc(),
                KnowledgeSnippet.created_at.desc(),
                KnowledgeSnippet.id.desc(),
            ).limit(max(1, min(int(k), _SEARCH_MAX_K)))
        )

        results: list[dict[str, Any]] = []
        for row, trgm_score in db.execute(stmt).all():
            results.append(
                {
                    "id": str(row.id),
                    "chunk_type": row.chunk_type.value if row.chunk_type else None,
                    "title": row.title,
                    "content": row.content,
                    "meta": row.meta if isinstance(row.meta, dict) else {},
                    "status": row.status.value if row.status else None,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "similarity": round(max(0.0, min(1.0, float(trgm_score))), 4),
                }
            )
        return results


crud_knowledge = CRUDKnowledge()

__all__ = ["CRUDKnowledge", "crud_knowledge"]
