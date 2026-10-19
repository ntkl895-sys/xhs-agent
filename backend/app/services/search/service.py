"""关键词搜索服务层（第 23 步）：缓存 → provider → 缓存写回。

设计要点：

- 服务函数**同步、确定性**，不挂 LangGraph 图、不调用 LLM、不烧 token——
  可直接被未来 Agent 图作为工具函数调用；
- provider 决策完全交给注册表（本层不 import 具体实现）；
- 缓存读写失败一律降级为「无缓存」，绝不影响搜索本身。
"""

from __future__ import annotations

from app.core.config import settings
from app.services.search import registry
from app.services.search.cache import RedisSearchCache, SearchCache, build_cache_key
from app.services.search.provider import SearchProvider
from app.services.search.schemas import SearchOutcome, SearchResult

#: 默认缓存（进程级单例；测试可注入内存实现）
_default_cache: SearchCache = RedisSearchCache()


def search_repositories(
    *,
    query: str,
    source: str = "github",
    sort: str = "stars",
    limit: int = 10,
    force_refresh: bool = False,
    provider: SearchProvider | None = None,
    cache: SearchCache | None = None,
) -> SearchOutcome:
    """执行一次关键词搜索（带缓存）。

    Args:
        query: 关键词（非空；长度 / 空白校验在路由层完成）。
        source: 源标识（必须已注册；路由层已校验，这里再兜底）。
        sort: 各源白名单内的排序键（github：``stars`` / ``updated``）。
        limit: 返回条数（provider 侧再截断兜底）。
        force_refresh: 跳过缓存强刷（预留；默认关闭）。
        provider / cache: 测试注入点；缺省用注册表与 Redis 缓存。

    Raises:
        ValueError: ``source`` 未注册（防直调误用；路由层应先拦为 400）。
        SearchError: provider 抛出的限速 / 上游异常（原样透传给调用方）。
    """
    active = provider if provider is not None else registry.get_provider(source)
    if active is None:
        raise ValueError(f"未注册的搜索来源：{source!r}")
    active_cache = cache if cache is not None else _default_cache

    key = build_cache_key(
        source=active.source, query=query, sort=sort, limit=limit
    )
    if not force_refresh:
        cached_items = active_cache.get(key)
        if cached_items is not None:
            items = [SearchResult.model_validate(item) for item in cached_items]
            return SearchOutcome(
                source=active.source,
                query=query,
                sort=sort,
                count=len(items),
                cached=True,
                items=items,
            )

    items = active.search(query, limit=limit, sort=sort)
    active_cache.set(
        key,
        [item.model_dump(mode="json") for item in items],
        ttl_seconds=settings.SEARCH_CACHE_TTL_SECONDS,
    )
    return SearchOutcome(
        source=active.source,
        query=query,
        sort=sort,
        count=len(items),
        cached=False,
        items=items,
    )


__all__ = ["search_repositories"]
