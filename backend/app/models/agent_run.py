"""agent_runs —— 每次 Agent 运行的审计记录（成本 / JSON 稳定性 / 排障）。

安全红线：``llm_base_url`` 记录 base_url，**绝不记录 API Key**。
成本核算只存 token 数，**不内置任何价格表**（价格随时会变，应由外部配置决定）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import RunStatus, RunTrigger, RunType, sa_enum


class AgentRun(Base):
    """一次 Agent 运行的审计行。

    ``trigger`` 在 PostgreSQL 中属保留字，这里显式 ``quote=True`` 保证渲染为 ``"trigger"``。
    """

    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    run_type: Mapped[RunType] = mapped_column(sa_enum(RunType, length=32), nullable=False)
    trigger: Mapped[RunTrigger] = mapped_column(
        "trigger",
        sa_enum(RunTrigger, length=16),
        nullable=False,
        default=RunTrigger.MANUAL,
        server_default=RunTrigger.MANUAL.value,
        quote=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    persona_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("personas.id", ondelete="SET NULL"), nullable=True
    )
    note_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("notes.id", ondelete="SET NULL"), nullable=True
    )
    topic_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("topics.id", ondelete="SET NULL"), nullable=True
    )
    inputs: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="运行入参快照")
    outputs: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="产出 id 列表 / 摘要"
    )
    llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    llm_base_url: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="模型 base_url；绝不记录 API Key"
    )
    llm_raw: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="模型原始输出；绝不记录 API Key"
    )
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    json_parse_attempts: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="JSON 解析尝试次数（理想恒为 1）"
    )
    status: Mapped[RunStatus | None] = mapped_column(
        sa_enum(RunStatus, length=16), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_agent_runs_run_type_created_at", "run_type", desc("created_at")),
        Index("ix_agent_runs_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AgentRun id={self.id} run_type={self.run_type} status={self.status}>"
