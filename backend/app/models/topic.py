"""topics —— 选题库。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import TopicDifficulty, TopicStatus, sa_enum


class Topic(Base):
    """选题。来源可以是热搜、也可以人工/模型自拟。"""

    __tablename__ = "topics"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    persona_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("personas.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    angle: Mapped[str | None] = mapped_column(Text, nullable=True, comment="切入角度")
    summary: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="为什么写 / 写给谁 / 核心观点"
    )
    pillar: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="内容支柱")
    content_outline: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="内容大纲（字符串数组 3~6 条）"
    )
    source_hotspot_ids: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="来源热点 id 数组"
    )
    source_trace: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="溯源：平台 + 热点标题"
    )
    target_keywords: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    tags: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="小红书 #话题标签数组（纯词 3~8 个，不含 #）"
    )
    heat_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    difficulty: Mapped[TopicDifficulty | None] = mapped_column(
        sa_enum(TopicDifficulty, length=10), nullable=True
    )
    score: Mapped[float | None] = mapped_column(Float, nullable=True, comment="综合排序分")
    llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    meta: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="生成元数据，含 json_parse_attempts 等"
    )
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True, comment="人工评审备注")
    status: Mapped[TopicStatus] = mapped_column(
        sa_enum(TopicStatus, length=20),
        nullable=False,
        default=TopicStatus.DRAFT,
        server_default=TopicStatus.DRAFT.value,
    )
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_topics_status_account_id", "status", "account_id"),
        Index("ix_topics_score", desc("score")),
        Index("ix_topics_generated_at", "generated_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Topic id={self.id} title={self.title!r} status={self.status}>"
