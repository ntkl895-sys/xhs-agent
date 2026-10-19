"""GitHub 仓库搜索 provider（官方 api.github.com，只读公开数据）。

上游契约（2026-10-15 本机实测，匿名）：

    GET {base}/search/repositories?q={q}&sort={sort}&order=desc&per_page={n}
    响应头含 ``X-RateLimit-Limit / Remaining / Reset``（epoch 秒）。

限速口径（**以响应头为准动态退让，不硬刷**）：

- ``429`` / ``403``（限速）/ ``X-RateLimit-Remaining=0`` ⇒ 记
  ``blocked_until = X-RateLimit-Reset`` 并抛 :class:`SearchRateLimitedError`
  （路由转友好 503）；Reset 前**不再触网**（本地快速失败）；
- ``5xx`` / 网络异常 ⇒ tenacity 指数退避重试（``SEARCH_MAX_ATTEMPTS`` 次），
  耗尽转 502；``4xx``（非限速）/ 非法 JSON ⇒ 立即失败（不重试），转 502。

合规与安全（任何改动都不得违反）：

- 只读 ``GITHUB_API_BASE``（默认官方 ``https://api.github.com``）；
- ``GITHUB_TOKEN`` 只出现在 Authorization 头，绝不打印 / 回显
  （错误消息统一过 :meth:`GitHubProvider._scrub` 兜底）；
- ``base_url`` 为 localhost / 127.0.0.1 时**绝不走代理**；外网地址读
  ``HTTPS_PROXY``（http 地址读 ``HTTP_PROXY``），行为完全由本模块控制
  （``trust_env=False``，不受隐式环境变量影响）。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.services.search.provider import (
    SearchProvider,
    SearchRateLimitedError,
    SearchUpstreamError,
)
from app.services.search.schemas import SearchResult

logger = logging.getLogger("xhs-agent.search.github")

#: 请求标识（GitHub API 要求带可辨识的 User-Agent）
GITHUB_USER_AGENT = "xhs-agent/0.1.0 (keyword-search-radar; local-only)"

#: 官方要求的 Accept（固定 API 版本，防响应结构漂移）
GITHUB_ACCEPT = "application/vnd.github+json"

#: 本地地址（绝不走代理）
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


class _RetryableUpstreamError(RuntimeError):
    """内部信号：5xx（交给 tenacity 重试；不对外暴露）。"""


def _as_int(raw: Any) -> int | None:
    """宽松取整数（bool 排除；失败返回 None）。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _resolve_proxy(base_url: str) -> str | None:
    """按 ``base_url`` 决议代理地址。

    - localhost / 127.0.0.1 / ::1 ⇒ ``None``（**绝不走代理**，
      供自建或镜像场景的本地地址使用）；
    - https 外网地址 ⇒ 读 ``HTTPS_PROXY`` / ``https_proxy``；
    - http 外网地址 ⇒ 读 ``HTTP_PROXY`` / ``http_proxy``。
    """
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return None
    if parsed.scheme == "https":
        return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None
    return os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or None


class GitHubProvider(SearchProvider):
    """GitHub 仓库搜索（``/search/repositories``，按 star / 更新时间排序）。"""

    source = "github"
    display_name = "GitHub"

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        max_attempts: int,
        token: str = "",
        limit_max: int = 30,
        fallback_backoff_seconds: int = 60,
        retry_waiter: Callable[[], Any] | None = None,
        transport: httpx.BaseTransport | None = None,
        now_func: Callable[[], float] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_attempts = max(1, int(max_attempts))
        self.limit_max = max(1, int(limit_max))
        self._fallback_backoff_seconds = max(1, int(fallback_backoff_seconds))
        self._retry_waiter = retry_waiter or self._default_waiter
        self._now: Callable[[], float] = now_func or time.time
        #: 限速退避截止时刻（epoch 秒）；None = 未处于退避窗口
        self._rate_limited_until: float | None = None
        #: 令牌仅存于内存用于请求头与脱敏，绝不外泄
        self._token = (token or "").strip()

        headers = {
            "User-Agent": GITHUB_USER_AGENT,
            "Accept": GITHUB_ACCEPT,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                connect=min(5.0, float(timeout)),
                read=float(timeout),
                write=min(5.0, float(timeout)),
                pool=min(5.0, float(timeout)),
            ),
            headers=headers,
            transport=transport,
            proxy=_resolve_proxy(self.base_url),
            trust_env=False,  # 代理决议完全由 _resolve_proxy 控制
        )

    # ------------------------------------------------------------------ 构造 / 生命周期
    @classmethod
    def from_settings(
        cls,
        *,
        transport: httpx.BaseTransport | None = None,
        retry_waiter: Callable[[], Any] | None = None,
        now_func: Callable[[], float] | None = None,
    ) -> "GitHubProvider":
        """按当前 ``Settings`` 构造（测试可注入 transport / 假时钟）。"""
        return cls(
            base_url=settings.GITHUB_API_BASE,
            timeout=settings.SEARCH_TIMEOUT_SECONDS,
            max_attempts=settings.SEARCH_MAX_ATTEMPTS,
            token=settings.GITHUB_TOKEN,
            limit_max=settings.SEARCH_LIMIT_MAX,
            fallback_backoff_seconds=settings.SEARCH_BACKOFF_FALLBACK_SECONDS,
            transport=transport,
            retry_waiter=retry_waiter,
            now_func=now_func,
        )

    @staticmethod
    def _default_waiter() -> Any:
        """默认退避：指数退避，1s 起、8s 封顶（与热搜客户端口径一致）。"""
        return wait_exponential(multiplier=1, min=1, max=8)

    def close(self) -> None:
        """关闭底层连接池。"""
        self._client.close()

    def __enter__(self) -> "GitHubProvider":
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    # ------------------------------------------------------------------ 主入口
    def search(self, query: str, *, limit: int, sort: str) -> list[SearchResult]:
        """执行一次仓库搜索；失败抛 :class:`SearchError` 子类。

        Args:
            query: 关键词（非空；调用方已做长度 / 空白校验）。
            limit: 返回条数（1 ~ ``limit_max``，本地再截断兜底）。
            sort: ``stars`` / ``updated``（调用方已做白名单校验）。
        """
        self._ensure_not_rate_limited()

        per_page = max(1, min(int(limit), self.limit_max))
        params = {
            "q": query,
            "sort": sort,
            "order": "desc",
            "per_page": str(per_page),
        }
        payload = self._request_with_retry(params)
        return self._map_items(payload)[:per_page]

    # ------------------------------------------------------------------ 限速退避
    def _ensure_not_rate_limited(self) -> None:
        """处于退避窗口内则**本地快速失败**（不触网）。"""
        until = self._rate_limited_until
        if until is None:
            return
        now = self._now()
        if now >= until:
            self._rate_limited_until = None
            return
        wait_seconds = max(1, int(until - now + 0.999))
        raise SearchRateLimitedError(
            "GitHub 搜索接口仍处于限速退避窗口内（在达到上游限额后按 "
            f"X-RateLimit-Reset 本地退避），预计 {_format_clock(until)} 后自动恢复；"
            "本次未发起请求。可稍后再试，或为 backend/.env 配置 GITHUB_TOKEN 提升限额。",
            reset_at=until,
        )

    def _block_until(self, reset_at: float) -> None:
        """记录退避截止时刻（Reset 前不再触网）。"""
        self._rate_limited_until = reset_at
        logger.warning(
            "GitHub 搜索触发限速：本地退避至 %s（该时刻前不发起任何请求）",
            _format_clock(reset_at),
        )

    def _parse_reset(self, response: httpx.Response) -> float:
        """解析 ``X-RateLimit-Reset``（epoch 秒）；缺失/异常回退兜底退避。"""
        reset = _as_int(response.headers.get("X-RateLimit-Reset"))
        if reset is None:
            return self._now() + self._fallback_backoff_seconds
        return max(float(reset), self._now() + 1.0)

    @staticmethod
    def _is_rate_limit_response(response: httpx.Response) -> bool:
        """403 是否为限速（``Remaining=0`` 或响应体 message 含 rate limit）。"""
        if response.headers.get("X-RateLimit-Remaining") == "0":
            return True
        try:
            body = response.json()
        except ValueError:
            return False
        if not isinstance(body, dict):
            return False
        return "rate limit" in str(body.get("message") or "").lower()

    def _learn_rate_limit(self, response: httpx.Response) -> None:
        """从任意响应头学习限额：``Remaining=0`` ⇒ 记退避截止时刻。"""
        remaining = _as_int(response.headers.get("X-RateLimit-Remaining"))
        if remaining is not None and remaining <= 0:
            self._block_until(self._parse_reset(response))

    # ------------------------------------------------------------------ 请求（重试语义）
    def _request_with_retry(self, params: dict[str, str]) -> dict[str, Any]:
        """带 tenacity 重试的 GET；仅 5xx / 网络异常可重试。"""
        retryer = Retrying(
            stop=stop_after_attempt(self.max_attempts),
            wait=self._retry_waiter(),
            retry=retry_if_exception_type((httpx.TransportError, _RetryableUpstreamError)),
            reraise=True,
        )
        try:
            return retryer(self._request_once, params)
        except (httpx.TransportError, _RetryableUpstreamError) as exc:
            raise SearchUpstreamError(
                self._scrub(
                    f"GitHub 搜索上游暂不可用（{type(exc).__name__}），"
                    f"已重试 {self.max_attempts} 次仍失败，请稍后再试。"
                )
            ) from None

    def _request_once(self, params: dict[str, str]) -> dict[str, Any]:
        """单次 GET + 状态码分类（可重试 / 限速 / 不可重试分别抛出）。"""
        # httpx.TransportError 原样抛出，交给 tenacity 重试
        response = self._client.get("/search/repositories", params=params)
        # 无论状态码都先学习限额（Remaining=0 时即便本次成功也提前退避）
        self._learn_rate_limit(response)

        status = response.status_code
        if status == 429 or (status == 403 and self._is_rate_limit_response(response)):
            reset_at = self._parse_reset(response)
            self._block_until(reset_at)
            raise SearchRateLimitedError(
                "GitHub 搜索接口已达当前时间窗限额（匿名约 10 次/分钟、配置 "
                "GITHUB_TOKEN 后约 30 次/分钟；一律以响应头 X-RateLimit-* 为准）。"
                f"已按上游 Reset 时刻本地退避，预计 {_format_clock(reset_at)} 后自动恢复；"
                "期间不再发起请求。",
                reset_at=reset_at,
            )
        if status >= 500:
            raise _RetryableUpstreamError(f"上游返回 HTTP {status}")
        if status != 200:
            raise SearchUpstreamError(
                f"GitHub 搜索上游返回 HTTP {status}（请求被拒绝，未重试）。"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise SearchUpstreamError("GitHub 搜索上游返回了非 JSON 响应。") from exc
        if not isinstance(payload, dict):
            raise SearchUpstreamError("GitHub 搜索上游响应结构异常（顶层非对象）。")
        if not isinstance(payload.get("items"), list):
            raise SearchUpstreamError("GitHub 搜索上游响应缺少 items 数组。")
        return payload

    # ------------------------------------------------------------------ 映射
    def _map_items(self, payload: dict[str, Any]) -> list[SearchResult]:
        """把 GitHub ``items[]`` 映射为标准化 :class:`SearchResult`。

        缺 ``full_name`` / ``html_url`` 的脏数据直接丢弃（前端外链仅 github.com）。
        """
        results: list[SearchResult] = []
        for row in payload.get("items", []):
            if not isinstance(row, dict):
                continue
            full_name = str(row.get("full_name") or "").strip()
            html_url = str(row.get("html_url") or "").strip()
            if not full_name or not html_url:
                continue

            owner = row.get("owner")
            owner_login: str | None = None
            if isinstance(owner, dict) and owner.get("login"):
                owner_login = str(owner["login"])

            description = row.get("description")
            language = row.get("language")
            updated_at = row.get("updated_at")

            results.append(
                SearchResult(
                    title=full_name,
                    url=html_url,
                    source=self.source,
                    snippet=(str(description).strip() or None) if description else None,
                    score=_as_int(row.get("stargazers_count")),
                    meta={
                        "language": (str(language).strip() or None) if language else None,
                        "updated_at": (str(updated_at) or None) if updated_at else None,
                        "owner": owner_login,
                        "forks_count": _as_int(row.get("forks_count")),
                    },
                )
            )
        return results

    # ------------------------------------------------------------------ 脱敏
    def _scrub(self, text: str) -> str:
        """纵深防御：消息中出现令牌时一律打码（正常情况下不会命中）。"""
        if self._token and self._token in text:
            return text.replace(self._token, "***")
        return text


def _format_clock(epoch_seconds: float) -> str:
    """把 epoch 秒格式化为本地时钟（HH:MM:SS，供人读提示）。"""
    try:
        return time.strftime("%H:%M:%S", time.localtime(epoch_seconds))
    except (OverflowError, OSError, ValueError):
        return "稍后"


__all__ = ["GITHUB_ACCEPT", "GITHUB_USER_AGENT", "GitHubProvider"]
