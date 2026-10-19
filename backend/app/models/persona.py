"""personas —— 写作人设（公共人设库 + 账号专属人设）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import PersonaStatus, sa_enum


class Persona(Base):
    """写作人设。``account_id`` 为 NULL 表示公共人设库。"""

    __tablename__ = "personas"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
        comment="归属账号；NULL = 公共人设库",
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="人设名")
    positioning: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="我是谁 / 目标读者 / 价值主张"
    )
    target_audience: Mapped[str | None] = mapped_column(Text, nullable=True, comment="目标读者")
    tone: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="语气风格")
    content_pillars: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="内容支柱数组"
    )
    taboo: Mapped[list | None] = mapped_column(JSONB, nullable=True, comment="人设禁忌数组")
    signature_style: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="口头禅 / 标志性表达"
    )
    system_prompt: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="第 12 步用的人设提示词模板"
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    status: Mapped[PersonaStatus] = mapped_column(
        sa_enum(PersonaStatus, length=20),
        nullable=False,
        default=PersonaStatus.ACTIVE,
        server_default=PersonaStatus.ACTIVE.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (Index("ix_personas_account_id", "account_id"),)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Persona id={self.id} name={self.name!r}>"
