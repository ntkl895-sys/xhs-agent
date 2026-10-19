"""健康检查路由。

- GET /health        存活探针：不依赖任何外部依赖，进程在即返回 ok
- GET /health/ready  就绪探针：检查 PostgreSQL / pgvector 扩展 / Redis

设计要点：任一检查失败都不抛未处理异常；/health/ready 在依赖异常时返回
HTTP 503，但响应体仍带完整 checks，方便前端与 healthcheck.py 精确展示。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.core.config import settings
from app.db.redis_client import ping as redis_ping
from app.db.session import engine

router = APIRouter(tags=["health"])


def _now_iso() -> str:
    """当前时间（带时区偏移的 ISO 8601 字符串）。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _check_database() -> bool:
    """执行 SELECT 1 探测 PostgreSQL 连通性。"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _check_pgvector() -> bool:
    """查询 pg_extension 判断 vector 扩展是否已启用。"""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            ).first()
        return row is not None
    except Exception:
        return False


@router.get("/health")
def health() -> dict:
    """存活探针：不访问数据库 / Redis。"""
    return {
        "status": "ok",
        "service": "backend",
        "version": settings.APP_VERSION,
        "time": _now_iso(),
    }


@router.get("/health/ready")
def health_ready(response: Response) -> dict:
    """就绪探针：分别检查 PostgreSQL、pgvector 扩展、Redis。

    三项全通过 -> 200 + status=ok；任一不通过 -> 503 + status=degraded，
    但响应体始终包含完整的 checks 明细。
    """
    database = _check_database()
    # 数据库都连不上时，扩展查询必然失败，直接置 False 以免无谓等待
    pgvector = _check_pgvector() if database else False
    redis_ok = redis_ping()

    checks = {
        "database": database,
        "pgvector": pgvector,
        "redis": redis_ok,
    }
    all_ok = all(checks.values())

    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if all_ok else "degraded",
        "checks": checks,
    }
