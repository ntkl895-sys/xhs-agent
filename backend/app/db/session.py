"""SQLAlchemy 2 引擎与会话工厂。

本步只创建引擎与会话依赖，【不建表、不引入 Alembic】——核心表与迁移在第 7 步完成。
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

# pool_pre_ping=True：每次从连接池取连接前先探活，避免拿到已被服务端断开的连接
# connect_timeout：显式设置连接超时，保证数据库不可达时健康探针能在有限时间内返回
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    future=True,
    connect_args={"connect_timeout": settings.DB_CONNECT_TIMEOUT},
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：提供一个请求级数据库会话，请求结束后自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
