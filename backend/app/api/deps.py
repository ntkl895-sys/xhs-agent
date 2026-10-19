"""公共 API 依赖：分页参数、数据库会话。

``get_db`` 仍来自 ``app.db.session``，这里只是统一出口，避免路由到处 import 具体模块。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Query

from app.db.session import get_db  # noqa: F401  (对外作为依赖导出)

# 分页默认值（与 README 接口表一致）
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@dataclass(frozen=True)
class PageParams:
    """分页参数。"""

    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


def pagination(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(
        DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"每页条数，默认 {DEFAULT_PAGE_SIZE}，上限 {MAX_PAGE_SIZE}",
    ),
) -> PageParams:
    """分页依赖：``page ≥ 1``，``page_size`` 默认 20 / 上限 100。"""
    return PageParams(page=page, page_size=page_size)
