"""Redis 客户端。

为第 9 步的定时任务 / 队列预留。本步只构造客户端并提供 ping 检查。
"""

from __future__ import annotations

import redis

from app.core.config import settings

# 连接池由 redis-py 内部管理，decode_responses=True 便于直接读取字符串。
# 显式设置连接 / 读写超时，避免 Redis 不可达时健康探针挂起。
redis_client = redis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT,
    socket_timeout=settings.REDIS_CONNECT_TIMEOUT,
)


def ping() -> bool:
    """探测 Redis 连通性。

    返回 True / False；任何异常都被吞掉并返回 False，绝不向上抛出，
    以保证健康检查路由在依赖不可用时仍能返回结构化结果。
    """
    try:
        return bool(redis_client.ping())
    except Exception:
        return False
