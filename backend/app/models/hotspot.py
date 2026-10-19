"""hotspots —— 热搜快照（第 9 步才写入，本步只建表）。"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import HotspotStatus, sa_enum


class Hotspot(Base):
    """热搜榜单条目的一次采集快照。

    ``content_hash`` 由 ``source + source_item_id + hot_date`` 归一化后哈希生成，
    用于跨批次去重（``uq_hotspot_hash``）。
    """

    __tablename__ = "hotspots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="zhihu / douyin / bilibili / toutiao / qq-news ..."
    )
    source_item_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="上游条目标识"
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    mobile_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    hot_score: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="热度值")
    rank_position: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="榜单名次")
    cover_url: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="封面图（默认不采集）"
    )
    summary: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="上游摘要 desc（默认不采集）"
    )
    author: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="作者（默认不采集）"
    )
    raw_extra: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="仅保留最小集：id / mobileUrl / timestamp"
    )
    hot_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="归属日期 / 批次")
    fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="采集时间"
    )
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="source+source_item_id+hot_date 归一化哈希"
    )
    status: Mapped[HotspotStatus] = mapped_column(
        sa_enum(HotspotStatus, length=20),
        nullable=False,
        default=HotspotStatus.NEW,
        server_default=HotspotStatus.NEW.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_hotspot_hash"),
        Index("ix_hotspots_source_hot_date", "source", "hot_date"),
        Index("ix_hotspots_status", "status"),
        Index("ix_hotspots_fetched_at", "fetched_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Hotspot id={self.id} source={self.source!r} title={self.title!r}>"
