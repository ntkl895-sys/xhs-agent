"""notes —— 笔记作品（人工审核 + 手动发布）；covers —— 封面。

说明：本文件承载「笔记作品」这一领域的 **两张表**：

- ``notes``  笔记正文与发布状态；
- ``covers`` 该笔记的封面候选（规格 1242×1656 = 3:4，第 13 步写入）。

为什么 ``notes`` 里没有 ``cover_id``：避免 ``notes ↔ covers`` 循环外键。
当前选用哪张封面由 ``covers.is_selected`` 标记，**「一个 note 仅一个选中封面」
由应用层保证**（见 README「索引与约束决策」）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ComplianceStatus, NoteStatus, sa_enum


class Note(Base):
    """笔记作品。``account_id`` 用 RESTRICT：有业务数据引用的账号不允许物理删除。"""

    __tablename__ = "notes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
        comment="ON DELETE RESTRICT：引导用 archived 归档，禁止物理删除有数据的账号",
    )
    persona_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("personas.id", ondelete="SET NULL"), nullable=True
    )
    topic_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("topics.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True, comment="正文")
    tags: Mapped[list | None] = mapped_column(JSONB, nullable=True, comment="话题标签数组")
    content_check: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="违禁词命中与修改建议"
    )
    compliance_status: Mapped[ComplianceStatus] = mapped_column(
        sa_enum(ComplianceStatus, length=20),
        nullable=False,
        default=ComplianceStatus.PENDING,
        server_default=ComplianceStatus.PENDING.value,
    )
    status: Mapped[NoteStatus] = mapped_column(
        sa_enum(NoteStatus, length=20),
        nullable=False,
        default=NoteStatus.DRAFT,
        server_default=NoteStatus.DRAFT.value,
    )
    publish_url: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="人工发布后回填的作品链接"
    )
    xhs_note_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="手填或截图识别的小红书笔记 id"
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="人工标记的发布时间"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    llm_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
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
        Index("ix_notes_account_id_status", "account_id", "status"),
        Index("ix_notes_status_published_at", "status", "published_at"),
        Index("ix_notes_topic_id", "topic_id"),
        Index("ix_notes_xhs_note_id", "xhs_note_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Note id={self.id} title={self.title!r} status={self.status}>"


class Cover(Base):
    """封面候选。规格 1242×1656（3:4），第 13 步写入。"""

    __tablename__ = "covers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    note_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("notes.id", ondelete="CASCADE"), nullable=False
    )
    template_key: Mapped[str] = mapped_column(String(32), nullable=False, comment="模板标识")
    color_scheme: Mapped[str] = mapped_column(String(32), nullable=False, comment="配色方案")
    title_text: Mapped[str | None] = mapped_column(String(100), nullable=True)
    subtitle_text: Mapped[str | None] = mapped_column(String(100), nullable=True)
    config_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    image_path: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="导出 PNG 的本地路径 / 对象 key"
    )
    width: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1242, server_default=text("1242")
    )
    height: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1656, server_default=text("1656")
    )
    is_selected: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="是否当前选用；一个 note 仅一个 true，由应用层保证",
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_covers_note_id", "note_id"),)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Cover id={self.id} note_id={self.note_id} template={self.template_key!r}>"
