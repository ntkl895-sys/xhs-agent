"""note_metrics —— 笔记数据快照（第 15 步截图识别 / 人工录入）。

原则：**缺失留空，严禁臆造**。取不到的指标一律 NULL，并可在 ``evidence`` 里标注
``missing`` / ``uncertain``；每字段的来源区域与置信度也记在 ``evidence``。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import DataSource, sa_enum


class NoteMetric(Base):
    """一次数据采集快照（同一 note 同一时刻只允许一条）。"""

    __tablename__ = "note_metrics"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    note_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("notes.id", ondelete="CASCADE"), nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="数据采集时刻"
    )
    data_source: Mapped[DataSource] = mapped_column(
        sa_enum(DataSource, length=16), nullable=False
    )
    # 以下指标全部可空：取不到就留空，绝不臆造
    views: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="浏览量")
    likes: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="点赞")
    collects: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="收藏")
    comments: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="评论")
    shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="分享")
    new_followers: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="新增粉丝")
    evidence: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="每字段的来源区域 / 置信度 / missing / uncertain",
    )
    screenshot_path: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="截图路径（默认不保存作者信息）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("note_id", "captured_at", name="uq_metric_snapshot"),
        Index("ix_note_metrics_note_id_captured_at", "note_id", desc("captured_at")),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NoteMetric id={self.id} note_id={self.note_id} captured_at={self.captured_at}>"
