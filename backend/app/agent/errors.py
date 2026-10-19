"""Agent Harness 的异常体系与「可重试 / 不可重试」分类。

为什么要单独分「可重试」与「不可重试」两类：
LangGraph 的 ``RetryPolicy`` 只应该对**瞬时性**故障重试（429 限流、5xx、
网络抖动、模型输出不是合法 JSON）。对 401 鉴权失败、400 参数错误、
模型名不存在这类问题重试毫无意义，只会浪费时间并放大错误——必须**立即失败**。

使用方式：

.. code-block:: python

    from langgraph.types import RetryPolicy
    from app.agent.errors import AgentRetryableError

    policy = RetryPolicy(max_attempts=2, retry_on=AgentRetryableError)
"""

from __future__ import annotations


class AgentError(Exception):
    """Agent Harness 所有异常的基类。"""


class AgentConfigError(AgentError):
    """配置缺失 / 开关关闭 / 依赖未装。

    这类错误**绝不可重试**：重试一万次结果都一样。
    典型场景：未填 ``LLM_API_KEY``、``AGENT_LLM_ENABLED=false``、
    ``LLM_MODEL`` 仍是占位符。
    """


class AgentRetryableError(AgentError):
    """明确的**可重试**错误。

    覆盖：HTTP 429 限流、5xx 服务端错误、网络连接 / 超时异常、
    以及「模型输出的 JSON 解析失败（还有重试额度）」。

    LangGraph 节点的 ``RetryPolicy(retry_on=AgentRetryableError)`` 只重试本类。
    """


class AgentNonRetryableError(AgentError):
    """明确的**不可重试**错误。

    覆盖：HTTP 400 / 401 / 403 / 404 / 422 等客户端错误与鉴权失败。
    立即失败，不消耗重试额度。
    """


class AgentJSONError(AgentRetryableError):
    """结构化输出在耗尽全部尝试次数后仍无法解析为合法 JSON。

    **继承自 :class:`AgentRetryableError`** —— 因为 Task 约定「JSON 解析失败」属于
    可重试错误，节点的 ``RetryPolicy`` 会重试它。

    重试是**分层**的，别混淆：

    1. :func:`app.agent.parsing.json_repair.invoke_json` 内部先做
       ``AGENT_JSON_MAX_ATTEMPTS`` 次「追加纠正提示后重问」；
    2. 仍失败才抛本异常，再由 LangGraph 节点的 ``RetryPolicy`` 重试
       ``AGENT_NODE_MAX_RETRIES`` 次（LangGraph 的 ``max_attempts`` = 重试次数 + 1）。

    所以最坏情况下的模型调用次数是
    ``AGENT_JSON_MAX_ATTEMPTS × (1 + AGENT_NODE_MAX_RETRIES)``（默认 3 × 3 = 9 次）。

    该异常携带原始文本（``raw_text``）便于排障；原始文本里不可能含 key
    （key 只在请求头里），但仍按纪律只做截断存储。
    """

    def __init__(self, message: str, raw_text: str = "") -> None:
        super().__init__(message)
        self.raw_text = raw_text


def classify_exception(exc: BaseException) -> BaseException:
    """把第三方异常归类成上面三类之一（返回新异常或原异常）。

    仅做「分类」，不改变原始消息（保留可读的报错内容便于排障）。
    识别不了的异常按**不可重试**处理——宁可快速失败，也不要盲目重试。
    """
    # openai SDK 的错误：按 HTTP 状态码语义分类
    try:  # pragma: no cover - 取决于是否装了 openai（本基座必然装）
        import openai
    except ImportError:  # pragma: no cover
        openai = None  # type: ignore[assignment]

    if openai is not None:
        retryable = (
            openai.RateLimitError,            # 429
            openai.InternalServerError,       # 5xx
            openai.APIConnectionError,        # 网络不可达
            openai.APITimeoutError,           # 超时
        )
        non_retryable = (
            openai.AuthenticationError,       # 401
            openai.PermissionDeniedError,     # 403
            openai.BadRequestError,           # 400
            openai.NotFoundError,             # 404
            openai.UnprocessableEntityError,  # 422
        )
        if isinstance(exc, retryable):
            return AgentRetryableError(f"{type(exc).__name__}: {exc}")
        if isinstance(exc, non_retryable):
            return AgentNonRetryableError(f"{type(exc).__name__}: {exc}")

    if isinstance(exc, (AgentError,)):
        # 本模块定义的异常已自带分类（如 AgentJSONError 属于可重试），原样返回
        return exc

    # 连接层面的底层异常（httpx / 内置）也算可重试
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return AgentRetryableError(f"{type(exc).__name__}: {exc}")

    return AgentNonRetryableError(f"{type(exc).__name__}: {exc}")
