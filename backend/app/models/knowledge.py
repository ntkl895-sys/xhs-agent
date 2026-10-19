"""knowledge_snippets —— 向量知识块（第 18 / 19 步使用）。

第 7 步**只建向量列与索引，不真实生成向量**。写入的 ``embedding`` 一律由第 18 步
的 embedding 服务产生；本步的相似度测试只在事务内插入临时向量并回滚。

⚠️ 维度决策：``EMBEDDING_DIM = 1024`` 是**为第 18 步占位**。DeepSeek 不提供
embedding 接口，计划改用本地中文模型（如 bge 系列，多为 1024 维）。若第 18 步
最终选定的模型维度不是 1024，需**新增一次 Alembic 迁移**：修改列维度并重建
HNSW 索引（``ALTER TABLE ... ALTER COLUMN embedding TYPE vector(N)`` 后
``REINDEX`` / 重建索引）。

违禁词也存本表：``chunk_type='forbidden_word'``、``content`` 为词条本身、
``meta`` 带 ``category`` / ``replacement``。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector

from app.db.base import Base
from app.models.enums import ChunkType, RefType, SnippetStatus, sa_enum

# 向量维度占位值：第 18 步选定 embedding 模型后如需变更，走新迁移（见模块 docstring）
EMBEDDING_DIM = 1024


class KnowledgeSnippet(Base):
    """知识 / 爆款规律 / 违禁词库的向量块。"""

    __tablename__ = "knowledge_snippets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    chunk_type: Mapped[ChunkType] = mapped_column(
        sa_enum(ChunkType, length=32), nullable=False
    )
    ref_type: Mapped[RefType | None] = mapped_column(
        sa_enum(RefType, length=32), nullable=True, comment="多态来源类型（弱关联）"
    )
    ref_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="多态来源 id；不加强外键"
    )
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM),
        nullable=True,
        comment="第 18 步写入；本步只建列（EMBEDDING_DIM=1024 占位）",
    )
    meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[SnippetStatus] = mapped_column(
        sa_enum(SnippetStatus, length=20),
        nullable=False,
        default=SnippetStatus.ACTIVE,
        server_default=SnippetStatus.ACTIVE.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # HNSW + 余弦距离索引（pgvector 0.5+ 支持；库内扩展为 0.8.7）
        Index(
            "ix_knowledge_snippets_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_knowledge_snippets_chunk_type", "chunk_type"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<KnowledgeSnippet id={self.id} chunk_type={self.chunk_type} title={self.title!r}>"
