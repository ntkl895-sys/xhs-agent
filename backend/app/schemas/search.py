"""关键词搜索的 Pydantic schema（第 23 步 HTTP 出入参）。

对外结构与内部 ``app/services/search/schemas.py`` 同构但**独立**——
对外层不携带任何内部字段（不含令牌 / 内部状态），可独立演进。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SearchItemOut(BaseModel):
    """单条搜索结果（对外）。

    - ``title``   ：仓库全名（``owner/repo``）；
    - ``url``     ：仓库主页（GitHub 域）；
    - ``snippet`` ：仓库描述；
    - ``score``   ：star 数；
    - ``meta``    ：language / updated_at / owner / forks_count 等附加信息。
    """

    model_config = ConfigDict(extra="ignore")

    title: str
    url: str
    source: str
    snippet: str | None = None
    score: int | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class SearchResponseOut(BaseModel):
    """一次关键词搜索的响应（对外）。"""

    source: str
    query: str
    sort: str
    count: int
    cached: bool
    items: list[SearchItemOut]


__all__ = ["SearchItemOut", "SearchResponseOut"]
