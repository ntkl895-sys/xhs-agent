"""关键词搜索服务（第 23 步）。

可插拔搜索源：:class:`~app.services.search.provider.SearchProvider` 抽象 +
注册表（本期只注册 ``github``，其余源只留扩展位）。搜索是**确定性工具 / 服务层**：
不挂 LangGraph 图、不调用 LLM、不烧 token，服务函数可直接被未来 Agent 图调用。

合规红线（任何改动都不得违反）：

- 只接「公开、免登录、有官方 / 稳定接口」的源（本期 = 官方 ``api.github.com``）；
- **不请求任何内容平台**（小红书 / 抖音 / 微博 / 知乎等）的搜索域名；
- 不使用任何登录态 / Cookie / 账号；``GITHUB_TOKEN`` 只从 ``backend/.env``
  读取，绝不进日志 / 响应 / 前端产物。

模块职责：

- ``schemas.py``   ：标准化结果（``SearchResult``）与服务产物（``SearchOutcome``）；
- ``provider.py``  ：``SearchProvider`` 抽象 + 错误类型；
- ``registry.py``  ：provider 注册表（可插拔；未注册源由路由转 400）；
- ``github.py``    ：GitHub 仓库搜索（限速退避 / 重试 / 代理口径见模块注释）；
- ``cache.py``     ：Redis 缓存（读写失败降级，绝不影响搜索）；
- ``service.py``   ：单入口 ``search_repositories()``（缓存 → provider → 回写）。
"""

from __future__ import annotations

from app.services.search.provider import (
    SearchError,
    SearchProvider,
    SearchRateLimitedError,
    SearchUpstreamError,
)
from app.services.search.registry import (
    available_sources,
    get_provider,
    register_provider,
)
from app.services.search.schemas import SearchOutcome, SearchResult
from app.services.search.service import search_repositories

__all__ = [
    "SearchError",
    "SearchOutcome",
    "SearchProvider",
    "SearchRateLimitedError",
    "SearchResult",
    "SearchUpstreamError",
    "available_sources",
    "get_provider",
    "register_provider",
    "search_repositories",
]
