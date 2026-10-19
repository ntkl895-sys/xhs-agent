"""diagnostics —— 单篇诊断（第 16 步写入）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import DiagnosisType, sa_enum


class Diagnostic(Base):
    """针对单篇笔记的一次诊断结论。"""

    __tablename__ = "diagnostics"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    note_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("notes.id", ondelete="CASCADE"), nullable=False
    )
    based_on_metric_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("note_metrics.id", ondelete="SET NULL"),
        nullable=True,
        comment="所依据的数据快照",
    )
    diagnosis_type: Mapped[DiagnosisType | None] = mapped_column(
        sa_enum(DiagnosisType, length=32), nullable=True
    )
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    strengths: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    weaknesses: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    reasons: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    suggestions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    benchmark: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="同类 / 历史均值对比"
    )
    llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    llm_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_diagnostics_note_id_created_at", "note_id", desc("created_at")),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Diagnostic id={self.id} note_id={self.note_id} type={self.diagnosis_type}>"
