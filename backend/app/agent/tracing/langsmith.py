"""LangSmith 观测（**可选** SaaS，默认关闭）。

纪律：

- **默认关闭**。未显式开启时，本模块不做任何事，也不会向第三方上报任何数据；
- 开启意味着把运行轨迹**外发到第三方**，并可能产生费用 —— 由使用者自行评估与承担；
- API Key 只从本地 ``.env`` 读取，绝不写入代码 / 日志 / 数据库。

开启方式（backend/.env）：

.. code-block:: ini

    AGENT_LANGSMITH_TRACING=true
    AGENT_LANGSMITH_API_KEY=<你的 LangSmith key>
    AGENT_LANGSMITH_PROJECT=xhs-agent
"""

from __future__ import annotations

import logging
import os

from app.core.config import settings

logger = logging.getLogger("xhs-agent.agent.langsmith")

#: LangSmith 依赖的环境变量名
ENV_TRACING = "LANGSMITH_TRACING"
ENV_API_KEY = "LANGSMITH_API_KEY"
ENV_PROJECT = "LANGSMITH_PROJECT"

_enabled = False


def is_enabled() -> bool:
    """当前是否会向 LangSmith 上报。"""
    return bool(_enabled)


def configure_from_settings() -> bool:
    """按配置决定是否启用 LangSmith；返回最终是否启用。

    未开启时**不设置任何环境变量**，确保 LangChain 内部不会有隐式上报。
    """
    global _enabled  # noqa: PLW0603

    if not settings.AGENT_LANGSMITH_TRACING:
        _enabled = False
        logger.info("LangSmith 未启用（默认关闭，不上报任何数据）")
        return False

    if not settings.AGENT_LANGSMITH_API_KEY.strip():
        _enabled = False
        logger.warning("AGENT_LANGSMITH_TRACING=true 但缺少 AGENT_LANGSMITH_API_KEY，已保持关闭")
        return False

    os.environ[ENV_TRACING] = "true"
    os.environ[ENV_API_KEY] = settings.AGENT_LANGSMITH_API_KEY
    os.environ[ENV_PROJECT] = settings.AGENT_LANGSMITH_PROJECT
    _enabled = True
    logger.warning(
        "LangSmith 已启用：运行轨迹将外发到第三方（project=%s）。"
        "请注意数据外发与费用由使用者自行承担。",
        settings.AGENT_LANGSMITH_PROJECT,
    )
    return True


def status() -> dict[str, object]:
    """给自检 / 文档用的状态快照（**不含 key 明文**）。"""
    return {
        "configured": bool(settings.AGENT_LANGSMITH_TRACING),
        "has_api_key": bool(settings.AGENT_LANGSMITH_API_KEY.strip()),
        "project": settings.AGENT_LANGSMITH_PROJECT,
        "enabled": is_enabled(),
    }


__all__ = ["configure_from_settings", "is_enabled", "status"]
