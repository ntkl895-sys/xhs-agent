"""DailyHotApi 同步客户端（httpx + tenacity 重试）。

上游契约（迁自 ``hotspot_spike`` 的适配器，2026-10-09 实测，本地 dailyhot-api 2.0.8）：

    GET {base_url}/{key}          -> {"code":200, ..., "data":[{id,title,url,mobileUrl,hot,...}]}
    GET {base_url}/all            -> {"code":200, "routes":[{"name","path"}, ...]}

支持的查询参数：
    * ``limit=N``     ：服务端截断到第 N 条（本地仍会再截断兜底）；
    * ``cache=false`` ：**唯一**生效的强刷参数（``cache=true`` / ``nocache=1`` 实测无效）。

重试语义（与全项目一致）：
    仅对 **429 / 5xx / 网络传输异常** 指数退避重试，最多 ``max_attempts`` 次；
    **4xx（含 404）/ 非法 JSON / 业务 code 非 200** 一律立即失败，不重试。

合规：本客户端只访问 ``HOTSPOT_API_BASE_URL``（默认本机 ``127.0.0.1:6688``），
不携带任何登录态 / Cookie；小红书源由 :mod:`adapters` 在上游就拦掉。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import httpx
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings

logger = logging.getLogger("xhs-agent.hotspot.client")

#: 常见浏览器 UA，避免被上游按默认 UA 直接拒绝
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class HotspotClientError(RuntimeError):
    """**不可重试**的上游错误（4xx / 非法 JSON / 业务 code 异常）。"""


class HotspotRetryableError(RuntimeError):
    """**可重试**的上游错误（HTTP 429 / 5xx）。"""


def _default_waiter() -> Any:
    """默认退避策略：指数退避，1s 起、8s 封顶。"""
    return wait_exponential(multiplier=1, min=1, max=8)


class DailyHotClient:
    """DailyHotApi 的同步客户端。

    ``httpx.Client`` 是**线程安全**的，因此一个实例可在 ``ThreadPoolExecutor``
    的多个 worker 间共享（由 :mod:`runner` 负责），无需每源新建连接。
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        per_source_limit: int,
        force_refresh: bool,
        max_attempts: int,
        retry_waiter: Callable[[], Any] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.per_source_limit = per_source_limit
        self.force_refresh = force_refresh
        self.max_attempts = max(1, int(max_attempts))
        self._retry_waiter = retry_waiter or _default_waiter
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
            transport=transport,
        )

    # ------------------------------------------------------------------ 生命周期
    def close(self) -> None:
        """关闭底层连接池。"""
        self._client.close()

    def __enter__(self) -> "DailyHotClient":
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_settings(
        cls,
        *,
        force_refresh: bool | None = None,
        per_source_limit: int | None = None,
        transport: httpx.BaseTransport | None = None,
        retry_waiter: Callable[[], Any] | None = None,
    ) -> "DailyHotClient":
        """按当前 ``Settings`` 构造客户端（显式参数优先）。"""
        return cls(
            base_url=settings.HOTSPOT_API_BASE_URL,
            timeout=settings.HOTSPOT_TIMEOUT_SECONDS,
            per_source_limit=(
                settings.HOTSPOT_PER_SOURCE_LIMIT if per_source_limit is None else per_source_limit
            ),
            force_refresh=settings.HOTSPOT_FORCE_REFRESH if force_refresh is None else force_refresh,
            max_attempts=settings.HOTSPOT_MAX_ATTEMPTS,
            transport=transport,
            retry_waiter=retry_waiter,
        )

    # ------------------------------------------------------------------ 请求
    def _request(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """带重试的 GET，返回已校验的响应体 dict。"""
        retryer = Retrying(
            stop=stop_after_attempt(self.max_attempts),
            wait=self._retry_waiter(),
            retry=retry_if_exception_type((httpx.TransportError, HotspotRetryableError)),
            reraise=True,
        )
        return retryer(self._request_once, path, params)

    def _request_once(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """执行单次 GET 并做状态码 / JSON / 业务 code 校验。"""
        # httpx.TransportError 会原样抛出，交给 tenacity 重试
        response = self._client.get(path, params=params)

        if response.status_code == 429 or response.status_code >= 500:
            raise HotspotRetryableError(f"上游返回 HTTP {response.status_code}")
        if response.status_code != 200:
            raise HotspotClientError(
                f"上游返回 HTTP {response.status_code}（该源可能不存在或已下线）"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise HotspotClientError(f"响应不是合法 JSON：{exc}") from exc

        if not isinstance(payload, dict):
            raise HotspotClientError("响应体顶层不是 JSON 对象")
        if payload.get("code") not in (200, None):
            raise HotspotClientError(f"上游返回业务错误 code={payload.get('code')!r}")
        return payload

    def get_list(self, source: str) -> dict[str, Any]:
        """拉取单个源的榜单响应体。"""
        params: dict[str, str] = {"limit": str(self.per_source_limit)}
        if self.force_refresh:
            # 实测这是 DailyHotApi 唯一生效的强刷写法
            params["cache"] = "false"
        return self._request(f"/{source}", params)

    def get_routes(self) -> set[str]:
        """尽力获取上游 ``/all`` 的路由清单；失败返回空集合（不抛异常）。"""
        try:
            payload = self._request("/all", {})
        except Exception as exc:  # noqa: BLE001 - 路由清单只是锦上添花，不该拖垮采集
            logger.warning("获取 DailyHotApi 路由清单失败（忽略）：%s", exc)
            return set()
        routes = payload.get("routes")
        if not isinstance(routes, list):
            return set()
        return {
            str(item.get("name"))
            for item in routes
            if isinstance(item, dict) and item.get("name")
        }


__all__ = [
    "DEFAULT_USER_AGENT",
    "DailyHotClient",
    "HotspotClientError",
    "HotspotRetryableError",
]
