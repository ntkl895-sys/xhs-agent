"""accounts —— 小红书账号（人工发布）。

红线：**不存任何登录态 / Cookie / 密码 / Token**。这里只有用户自己填写的备注信息。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import AccountStatus, sa_enum


class Account(Base):
    """小红书账号（人工维护的账号档案）。"""

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="账号备注名（用户自定义，便于识别）"
    )
    xhs_red_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        unique=True,
        comment="用户手填的小红书号；不自动抓取；唯一但允许多个 NULL",
    )
    niche: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="账号领域，如「职场」「家居」"
    )
    status: Mapped[AccountStatus] = mapped_column(
        sa_enum(AccountStatus, length=20),
        nullable=False,
        default=AccountStatus.ACTIVE,
        server_default=AccountStatus.ACTIVE.value,
        comment="active / archived（归档代替物理删除）",
    )
    remark: Mapped[str | None] = mapped_column(Text, nullable=True, comment="备注")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Account id={self.id} name={self.name!r} status={self.status}>"
