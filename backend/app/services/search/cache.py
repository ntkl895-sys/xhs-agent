"""搜索结果缓存（Redis；读写失败一律降级，**绝不影响搜索本身**）。

缓存口径：

- key 含「归一化 query + sort + 条数上限」，形如
  ``search:{source}:{sort}:{limit}:{sha1(归一化 query)[:16]}``；
- TTL 由 ``SEARCH_CACHE_TTL_SECONDS`` 控制（默认 30 分钟）——尊重上游限额、
  避免反复触发限速；响应中的 ``cached`` 字段如实标注是否命中；
- Redis 不可达 / 序列化失败：只记 WARNING 并当作「无缓存」继续回源。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Protocol

from app.db.redis_client import redis_client

logger = logging.getLogger("xhs-agent.search.cache")

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_query(query: str) -> str:
    """归一化查询串：去首尾空白 + 折叠连续空白 + 小写。

    （GitHub 仓库搜索本身对大小写不敏感，此处归一让缓存键更稳定。）
    """
    return _WHITESPACE_RE.sub(" ", (query or "").strip()).lower()


def build_cache_key(*, source: str, query: str, sort: str, limit: int) -> str:
    """构造缓存 key（query 取哈希，避免超长 / 特殊字符污染键空间）。"""
    digest = hashlib.sha1(normalize_query(query).encode("utf-8")).hexdigest()[:16]
    return f"search:{source}:{sort}:{int(limit)}:{digest}"


class SearchCache(Protocol):
    """缓存读写协议（测试可注入内存实现）。"""

    def get(self, key: str) -> list[dict[str, Any]] | None:
        """读取缓存条目列表；未命中 / 失败返回 ``None``。"""
        ...

    def set(self, key: str, items: list[dict[str, Any]], ttl_seconds: int) -> None:
        """写入缓存（失败静默降级）。"""
        ...


class RedisSearchCache:
    """基于既有 ``redis_client`` 的实现（decode_responses=True 直接读字符串）。"""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client if client is not None else redis_client

    def get(self, key: str) -> list[dict[str, Any]] | None:
        try:
            raw = self._client.get(key)
        except Exception as exc:  # noqa: BLE001 - 缓存不可用必须降级而非拖垮搜索
            logger.warning("搜索缓存读取失败（忽略，直接回源）：%s", type(exc).__name__)
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning("搜索缓存反序列化失败（忽略，直接回源）")
            return None
        if not isinstance(data, list):
            return None
        return [item for item in data if isinstance(item, dict)]

    def set(self, key: str, items: list[dict[str, Any]], ttl_seconds: int) -> None:
        try:
            self._client.set(
                key,
                json.dumps(items, ensure_ascii=False),
                ex=max(1, int(ttl_seconds)),
            )
        except Exception as exc:  # noqa: BLE001 - 写失败只降级（下次请求自然回源）
            logger.warning("搜索缓存写入失败（忽略）：%s", type(exc).__name__)


__all__ = ["RedisSearchCache", "SearchCache", "build_cache_key", "normalize_query"]
