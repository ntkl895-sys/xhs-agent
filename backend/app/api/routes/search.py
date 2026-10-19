"""关键词搜索路由：``/api/v1/search``（第 23 步，纯只读）。

契约：

    GET /api/v1/search?q=&source=github&sort=stars&limit=10[&force_refresh=false]

- ``q`` 必填：strip 后为空 / 超长（> ``SEARCH_QUERY_MAX_LEN``）→ **422**；
- ``source`` 默认 ``github``；不在注册表 → **400**（本步仅注册 github）；
- ``sort`` ：``stars``（默认）/ ``updated``；其它 → **422**；
- ``limit`` ：1 ~ ``SEARCH_LIMIT_MAX``（30）；越界 → **422**；
- ``force_refresh`` ：跳过缓存强刷（预留；默认 false）；
- **纯只读**：无 POST / PUT / DELETE；不写业务库、不写任何平台。

错误体与既有接口一致（``AppError``）。限速 → **503** ``search_rate_limited``
（details 含 ``reset_at`` / ``retry_after_seconds``）；上游异常 → **502**
``search_upstream_error``。搜索来源的白名单校验在本层完成；实现细节一律
委托 ``app.services.search``，路由层不 import 任何具体 provider。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Query

from app.core.config import settings
from app.core.errors import AppError, bad_request, validation_error
from app.schemas.common import ErrorOutSpec
from app.schemas.search import SearchItemOut, SearchResponseOut
from app.services.search import registry
from app.services.search.provider import SearchRateLimitedError, SearchUpstreamError
from app.services.search.service import search_repositories

router = APIRouter(prefix="/api/v1/search", tags=["search"])

_SORT_KEYS = ("stars", "updated")


@router.get(
    "",
    response_model=SearchResponseOut,
    summary="关键词搜索（公开源，只读）",
    responses={**ErrorOutSpec.VALIDATION, **ErrorOutSpec.BAD_REQUEST},
)
def search_keyword(
    q: str = Query(
        ...,
        min_length=1,
        max_length=settings.SEARCH_QUERY_MAX_LEN,
        description=f"关键词（必填，strip 后非空，≤ {settings.SEARCH_QUERY_MAX_LEN} 字）",
    ),
    source: str = Query("github", description="搜索来源；当前仅注册 github"),
    sort: str = Query("stars", description="stars（默认）| updated（最近更新）"),
    limit: int = Query(
        10,
        ge=1,
        le=settings.SEARCH_LIMIT_MAX,
        description=f"返回条数，1~{settings.SEARCH_LIMIT_MAX}",
    ),
    force_refresh: bool = Query(False, description="跳过缓存强刷（预留；默认关闭）"),
) -> SearchResponseOut:
    """按关键词搜索公开源（GitHub 仓库），返回标准化结果。

    结果默认带 Redis 缓存（30 分钟），响应 ``cached`` 字段如实标注。
    未注册的来源 → 400；触发上游限速 → 503（按 Reset 本地退避，不硬刷）。
    """
    query = q.strip()
    if not query:
        raise validation_error(
            "q 不能为空",
            details=[{"loc": ["query", "q"], "msg": "关键词不能为空", "type": "value_error"}],
        )

    sort_value = (sort or "").strip().lower()
    if sort_value not in _SORT_KEYS:
        raise validation_error(
            "sort 只能是 stars / updated",
            details=[{"loc": ["query", "sort"], "msg": "非法取值", "type": "enum"}],
        )

    source_value = (source or "").strip().lower() or "github"
    if registry.get_provider(source_value) is None:
        available = "、".join(registry.available_sources()) or "（无）"
        raise bad_request(
            f"暂不支持的搜索来源：{source_value}（当前可用：{available}）",
            details={"available_sources": registry.available_sources()},
        )

    try:
        outcome = search_repositories(
            query=query,
            source=source_value,
            sort=sort_value,
            limit=limit,
            force_refresh=force_refresh,
        )
    except SearchRateLimitedError as exc:
        retry_after: int | None = None
        if exc.reset_at is not None:
            retry_after = max(1, int(exc.reset_at - time.time() + 0.999))
        raise AppError(
            "search_rate_limited",
            str(exc),
            status_code=503,
            details={"reset_at": exc.reset_at, "retry_after_seconds": retry_after},
        ) from exc
    except SearchUpstreamError as exc:
        raise AppError("search_upstream_error", str(exc), status_code=502) from exc

    return SearchResponseOut(
        source=outcome.source,
        query=outcome.query,
        sort=outcome.sort,
        count=outcome.count,
        cached=outcome.cached,
        items=[SearchItemOut.model_validate(item.model_dump()) for item in outcome.items],
    )
