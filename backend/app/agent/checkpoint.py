"""LangGraph 检查点（Postgres / memory）的生命周期管理。

## 为什么检查点表要单独隔离

LangGraph 的检查点表（``checkpoints`` / ``checkpoint_blobs`` / ``checkpoint_writes`` /
``checkpoint_migrations``）是**第三方框架托管表**：

- 它们由 ``PostgresSaver.setup()`` **幂等**创建与升级，**不写进业务 Alembic 迁移**；
- 业务那 10 张表永远只走 Alembic，两套东西不能混。

因此本模块把框架表放进**独立的 ``langgraph`` schema**，让 ``public`` 里始终
「恰好 10 张业务表」。

## 实现说明（如实记录）

所装 ``langgraph-checkpoint-postgres 3.1.2`` 的 ``PostgresSaver`` **没有** ``schema``
构造参数（其 DDL 是 ``CREATE TABLE IF NOT EXISTS checkpoints (...)`` 这种**不带 schema
前缀**的写法，落点由连接的 ``search_path`` 决定）。

所以这里采用的做法是：

1. 用 psycopg 的 ``ConnectionPool`` 建一个**专用连接池**，通过 ``options='-c
   search_path=langgraph,public'`` 把该连接的默认 schema 指向 ``langgraph``；
2. 先 ``CREATE SCHEMA IF NOT EXISTS langgraph``（``autocommit=True``，立即提交），
   ——这一步必须有：若 ``langgraph`` 不存在，PostgreSQL 会**静默跳过** search_path 里
   不存在的 schema 而落到 ``public``，隔离就失效了；
3. 再调 ``PostgresSaver(pool).setup()``，框架表便落在 ``langgraph``。

## 纪律（务必遵守）

- 升级 ``langgraph-checkpoint-postgres`` 后，**重新执行一次 ``setup()``** 即可完成
  框架表升级（``checkpoint_migrations`` 记录版本，重复执行幂等）；
- **绝不**把 ``checkpoint*`` 表手写进业务迁移；
- 业务 10 张表与框架表互不干涉，两者的建表工具链彼此独立。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from app.core.config import settings

logger = logging.getLogger("xhs-agent.agent.checkpoint")

#: 框架检查点表所在的独立 schema
CHECKPOINT_SCHEMA = "langgraph"

#: LangGraph 检查点相关的**框架托管表**（不含业务表；供自检 / 文档登记用）
FRAMEWORK_CHECKPOINT_TABLES = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)

_lock = threading.Lock()
_saver: Any | None = None
_pool: Any | None = None


def use_memory() -> bool:
    """当前配置的检查点后端是否为 memory。"""
    return settings.AGENT_CHECKPOINTER.strip().lower() == "memory"


def _build_pool() -> Any:
    """创建指向业务库、但默认 schema 为 ``langgraph`` 的专用连接池。"""
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    # psycopg 只接受**原生** DSN，喂 SQLAlchemy URL（含 +psycopg）会直接报错
    dsn = settings.native_database_url
    kwargs: dict[str, Any] = {
        "autocommit": True,
        # 框架表用「不带 schema 前缀」的 DDL，落点完全由 search_path 决定。
        # 同时保留 public，避免影响可能用到的 public 对象（如 pgvector 类型解析）。
        "options": f"-c search_path={CHECKPOINT_SCHEMA},public",
        "row_factory": dict_row,
        # 与 LangGraph 官方 from_conn_string 对齐：便于走连接池中间件（pgbouncer）
        "prepare_threshold": 0,
    }
    pool = ConnectionPool(
        conninfo=dsn,
        kwargs=kwargs,
        min_size=1,
        max_size=4,
        open=False,
        timeout=settings.DB_CONNECT_TIMEOUT,
    )
    pool.open(wait=False)
    return pool


def _ensure_schema(pool: Any) -> None:
    """确保 ``langgraph`` schema 存在（幂等）。

    必须在 ``setup()`` **之前**执行：search_path 里不存在的 schema 会被 PostgreSQL
    静默忽略，导致框架表意外落到 ``public``。
    """
    with pool.connection() as conn:
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{CHECKPOINT_SCHEMA}"')


def init_checkpointer() -> Any:
    """启动时调用：建池 + 建 schema + ``setup()``（幂等），返回 saver 单例。

    memory 模式直接返回 ``MemorySaver``（不碰数据库，仅离线自测用）。
    """
    global _saver, _pool  # noqa: PLW0603

    with _lock:
        if _saver is not None:
            return _saver

        if use_memory():
            _saver = MemorySaver()
            logger.warning("检查点后端 = memory（仅限离线自测，进程退出即丢失）")
            return _saver

        from langgraph.checkpoint.postgres import PostgresSaver

        pool = _build_pool()
        try:
            _ensure_schema(pool)
            saver = PostgresSaver(pool)
            saver.setup()  # 幂等：首次建表，之后按 checkpoint_migrations 增量升级
            logger.info(
                "检查点已就绪：schema=%s 表=%s",
                CHECKPOINT_SCHEMA,
                ", ".join(FRAMEWORK_CHECKPOINT_TABLES),
            )
        except BaseException:
            pool.close()
            raise
        _pool = pool
        _saver = saver
        return _saver


def get_checkpointer() -> Any:
    """取检查点单例；未初始化时按需初始化（便于脚本直接调用）。"""
    if _saver is None:
        return init_checkpointer()
    return _saver


def close_checkpointer() -> None:
    """关闭时调用：关池并清空单例。"""
    global _saver, _pool  # noqa: PLW0603

    with _lock:
        if _pool is not None:
            try:
                _pool.close()
            except Exception as exc:  # pragma: no cover - 关闭失败不影响退出
                logger.warning("关闭检查点连接池时出错：%s", exc)
        _pool = None
        _saver = None


def reset_checkpointer() -> None:
    """仅供自检使用：把单例复位（便于在 memory / postgres 之间切换测试）。"""
    close_checkpointer()


def framework_tables_report() -> dict[str, Any]:
    """查询框架检查点表的实际落点（供自检 / 文档登记用，只读）。"""
    from sqlalchemy import text

    from app.db.session import engine

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_name = ANY(:names) ORDER BY table_schema, table_name"
            ),
            {"names": list(FRAMEWORK_CHECKPOINT_TABLES)},
        ).all()
    found = [{"schema": r[0], "table": r[1]} for r in rows]
    schemas = sorted({r[0] for r in rows})
    return {
        "schema": CHECKPOINT_SCHEMA,
        "tables": found,
        "schemas_seen": schemas,
        # 隔离成功 = 所有框架表都落在独立 schema，且 public 里一个都没有
        "isolated": bool(found) and schemas == [CHECKPOINT_SCHEMA],
        "any_in_public": any(r[0] == "public" for r in rows),
    }


__all__ = [
    "CHECKPOINT_SCHEMA",
    "FRAMEWORK_CHECKPOINT_TABLES",
    "init_checkpointer",
    "get_checkpointer",
    "close_checkpointer",
    "reset_checkpointer",
    "use_memory",
    "framework_tables_report",
]
