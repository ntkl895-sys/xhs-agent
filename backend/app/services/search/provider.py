"""``SearchProvider`` 抽象与错误类型（第 23 步）。

扩展位说明（本期只注册 github）：新的公开源只需实现本抽象 + 在
:mod:`app.services.search.registry` 注册，路由与前端**无需改动**。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.services.search.schemas import SearchResult


class SearchError(RuntimeError):
    """搜索层错误的基类（路由层据此映射统一错误体）。"""


class SearchRateLimitedError(SearchError):
    """上游限速（429 / 403-限速 / ``X-RateLimit-Remaining=0``）。

    语义：按上游 ``X-RateLimit-Reset`` **本地退避**——Reset 前不再触网，
    路由层转友好 **503**（``search_rate_limited``），绝不硬刷。
    """

    def __init__(self, message: str, *, reset_at: float | None = None) -> None:
        super().__init__(message)
        #: 预计恢复时刻（epoch 秒）；``None`` = 上游未给出可解析的 Reset
        self.reset_at = reset_at


class SearchUpstreamError(SearchError):
    """上游不可用 / 拒绝 / 响应异常（重试耗尽、4xx、非法 JSON）：路由层转 502。"""


class SearchProvider(ABC):
    """可插拔搜索源抽象：``search(query, limit, sort) -> list[SearchResult]``。

    实现约定：

    - 同步、确定性；不感知 HTTP / 响应模型 / 缓存（由 service 层编排）；
    - 一切失败收敛为 :class:`SearchError` 子类抛出（不抛裸异常给上层）；
    - ``sort`` 取值为各源自己的白名单（由路由层校验后传入）。
    """

    #: 源标识（注册表键，如 ``"github"``）
    source: str = ""

    #: 展示名（中文/品牌，如 ``"GitHub"``）
    display_name: str = ""

    @abstractmethod
    def search(self, query: str, *, limit: int, sort: str) -> list[SearchResult]:
        """执行一次搜索并返回标准化结果列表（可能为空）。"""


__all__ = [
    "SearchError",
    "SearchProvider",
    "SearchRateLimitedError",
    "SearchUpstreamError",
]
