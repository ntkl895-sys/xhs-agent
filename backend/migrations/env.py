"""Alembic 运行环境配置（第 7 步定制）。

要点：

1. **连接串不写在 alembic.ini 里**（该文件会入库），统一从 ``app.core.config.settings``
   读取 ``DATABASE_URL``（最终来自环境变量 / ``backend/.env``，绝不硬编码密钥）。
2. ``target_metadata`` 指向 ``app.db.base.Base.metadata``，并**先 import ``app.models``**
   以注册全部 10 张表，保证 autogenerate 能发现完整模型。
3. 支持 offline（``--sql`` 生成 SQL 脚本）与 online 两种模式。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 把 backend/ 加入 sys.path，保证无论从哪个目录调用都能 import app.*
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402
from app.db.base import Base  # noqa: E402
import app.models  # noqa: E402,F401  —— 关键：导入全部模型以注册到 metadata

# Alembic Config 对象
config = context.config

# 用 settings.DATABASE_URL 覆盖 sqlalchemy.url（ini 中该项留空/占位）
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

# 日志配置
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 比对基准
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：仅根据 URL 生成 SQL，不建立真实连接。"""
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：建立真实连接执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
