"""关键词搜索的内部数据模型（第 23 步）。

字段约定：``SearchResult`` 是**跨 provider 的标准化结构**——无论上游是
GitHub 还是未来扩展的源，均由各自 provider 映射到同一结构；``SearchOutcome``
是 service 层的最终产物（含缓存命中标志），由路由层映射为对外 schema
（``app/schemas/search.py``）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SearchResult(BaseModel):
    """标准化搜索结果（一条）。

    - ``title``   ：标题（GitHub = ``full_name``，如 ``owner/repo``）；
    - ``url``     ：详情页链接（GitHub = ``html_url``）；
    - ``snippet`` ：摘要（GitHub = ``description``）；
    - ``score``   ：热度分（GitHub = ``stargazers_count``）；
    - ``meta``    ：附加信息（language / updated_at / owner / forks_count 等）。
    """

    model_config = ConfigDict(extra="ignore")

    title: str
    url: str
    source: str
    snippet: str | None = None
    score: int | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class SearchOutcome(BaseModel):
    """一次搜索的完整结果（service → 路由）。"""

    source: str
    query: str
    sort: str
    count: int = 0
    cached: bool = False
    items: list[SearchResult] = Field(default_factory=list)


__all__ = ["SearchOutcome", "SearchResult"]
