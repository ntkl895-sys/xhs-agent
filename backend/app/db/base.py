"""SQLAlchemy 2.0 统一 Declarative Base。

全项目所有 ORM 模型都继承本文件的 ``Base``：

- 统一 metadata 与命名约定（约束 / 索引名可预测，便于 Alembic 迁移与人工审计）；
- **只定义 Base，不建表**。建表一律走 Alembic 迁移，
  严禁在任何地方调用 ``Base.metadata.create_all()``（见 ``scripts/selftest_db.py`` 的断言）。
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# 命名约定：让主键 / 外键 / 唯一约束 / 检查约束 / 索引的名字可预测。
# 显式命名的约束（如 uq_hotspot_hash）不受影响，仍以显式名为准。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """全项目统一的 Declarative Base。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
