"""统一大模型工厂（OpenAI 兼容）。

约定（红线）：

- 全项目**统一走 OpenAI 兼容**：``langchain_openai.ChatOpenAI`` 指向 DeepSeek，
  不引入 ``langchain-deepseek`` 专用包，以便随时替换到其他 OpenAI 兼容端点；
- ``base_url`` / ``api_key`` / ``model`` **全部**取自 :mod:`app.core.config`，
  代码里不得出现任何硬编码的连接信息；
- 模型 ID 使用占位符，真实名字以 DeepSeek 控制台当前列表为准；
- 未配置 key 或 ``AGENT_LLM_ENABLED=false`` 时，工厂**不发起任何网络请求**，
  并在真实调用入口抛出可识别的 :class:`AgentConfigError`。

第 15 步（数据回填）接通视觉：``get_chat_model(want_vision=True)`` 由原先的
「一律抛 ``AgentConfigError``」改为返回可多模态调用的 ``ChatOpenAI``——
模型名优先取 ``settings.LLM_VISION_MODEL``，为空时回退 ``settings.LLM_MODEL``；
开关 / key / 模型名闸门依旧先行（``_ensure_ready``）。本工厂**只构造实例、
不校验端点能力、不发任何请求**；「模型实际不支持图片」由识别 Agent 按接口
报错关键词转成友好中文错误。
"""

from __future__ import annotations

import logging

from langchain_openai import ChatOpenAI

from app.agent.errors import AgentConfigError
from app.core.config import settings

logger = logging.getLogger("xhs-agent.agent.llm")

#: config 里 ``LLM_MODEL`` 的出厂占位值 —— 落到这个值说明用户还没换成真模型名
PLACEHOLDER_MODEL = "把这里替换成DeepSeek控制台当前模型名"

#: 视觉文档（模型名以控制台为准；代码不硬编码任何模型名）
VISION_DOC_URL = "https://api-docs.deepseek.com/zh-cn/guides/vision"


def is_llm_configured() -> bool:
    """是否具备发起真实 LLM 调用的条件（开关打开 + key 非空 + 模型名已替换）。"""
    return (
        bool(settings.AGENT_LLM_ENABLED)
        and bool(settings.LLM_API_KEY.strip())
        and settings.LLM_MODEL.strip() != PLACEHOLDER_MODEL
        and bool(settings.LLM_MODEL.strip())
    )


def resolve_vision_model_name() -> str:
    """解析视觉调用的模型名：``LLM_VISION_MODEL`` 优先，为空回退 ``LLM_MODEL``。"""
    return settings.LLM_VISION_MODEL.strip() or settings.LLM_MODEL.strip()


def is_vision_configured() -> bool:
    """是否具备发起真实**视觉**调用的条件（开关 + key + 视觉模型名可用）。"""
    model = resolve_vision_model_name()
    return (
        bool(settings.AGENT_LLM_ENABLED)
        and bool(settings.LLM_API_KEY.strip())
        and bool(model)
        and model != PLACEHOLDER_MODEL
    )


def describe_llm_readiness() -> dict[str, object]:
    """给自检 / 排障用的「就绪状态」快照（**绝不包含 key 明文**）。"""
    vision_model = resolve_vision_model_name()
    return {
        "llm_enabled": bool(settings.AGENT_LLM_ENABLED),
        "has_api_key": bool(settings.LLM_API_KEY.strip()),
        "base_url": settings.LLM_BASE_URL,
        "model": settings.LLM_MODEL,
        "model_is_placeholder": settings.LLM_MODEL.strip() == PLACEHOLDER_MODEL,
        "ready": is_llm_configured(),
        # --- 第 15 步：视觉（截图识别）就绪位 ---
        "vision_model": vision_model,
        "vision_model_is_placeholder": vision_model == PLACEHOLDER_MODEL,
        "vision_ready": is_vision_configured(),
    }


def _ensure_ready(*, want_vision: bool = False) -> None:
    """真实调用前的闸门：不满足条件就抛出清晰、可识别的配置错误。

    ``want_vision=True`` 时校验的是**视觉模型名**（``LLM_VISION_MODEL``，
    空则回退 ``LLM_MODEL``）；开关与 key 两项校验两种模式完全一致。
    """
    if not settings.AGENT_LLM_ENABLED:
        raise AgentConfigError(
            "AGENT_LLM_ENABLED=false，已禁止一切对外 LLM 调用。"
            "如需真实调用，请在 backend/.env 设为 true 并填入 LLM_API_KEY；"
            "离线自检请改用 FakeListChatModel / GenericFakeChatModel。"
        )
    if not settings.LLM_API_KEY.strip():
        raise AgentConfigError(
            "缺少 LLM_API_KEY：请在 backend/.env 中填写 DeepSeek 密钥"
            "（真实密钥只允许放在本地 .env，绝不写入代码 / 文档 / 日志）。"
        )
    if want_vision:
        vision_model = resolve_vision_model_name()
        if not vision_model or vision_model == PLACEHOLDER_MODEL:
            raise AgentConfigError(
                "视觉模型名未配置：LLM_VISION_MODEL 为空时会回退 LLM_MODEL，"
                f"但 LLM_MODEL 仍是占位符（{settings.LLM_MODEL!r}）。"
                "请在 DeepSeek 控制台的模型列表复制【支持图片输入】的模型名"
                "（当前为 deepseek-flash，以控制台为准）填入 LLM_VISION_MODEL："
                f" {VISION_DOC_URL}"
            )
        return
    if not settings.LLM_MODEL.strip() or settings.LLM_MODEL.strip() == PLACEHOLDER_MODEL:
        raise AgentConfigError(
            f"LLM_MODEL 仍是占位符（{settings.LLM_MODEL!r}）。"
            "请以 DeepSeek 控制台当前模型列表为准填入真实模型名："
            " https://api-docs.deepseek.com/zh-cn/quick_start/pricing"
        )


def get_chat_model(
    temperature: float = 0.2,
    want_json: bool = False,
    want_vision: bool = False,
) -> ChatOpenAI:
    """构造一个指向 DeepSeek（OpenAI 兼容）的 ChatOpenAI 实例。

    Args:
        temperature: 采样温度。结构化抽取建议低温度（默认 0.2）。
        want_json: 是否启用 OpenAI 兼容的 ``response_format={"type":"json_object"}``。
            DeepSeek 支持 JSON Output；若端点不支持，上层
            :func:`app.agent.parsing.json_repair.invoke_json` 会自动降级为
            「提示词约束 + 文本解析」。
        want_vision: 是否构造**多模态（图片输入）**模型实例（第 15 步接通）。
            模型名优先取 ``settings.LLM_VISION_MODEL``，为空时回退
            ``settings.LLM_MODEL``。注意：本工厂**只构造实例**——端点是否真的
            支持图片由运行时决定；接口报错文本命中「vision / multimodal /
            不支持图片」等关键词时，由识别 Agent（``app/agent/metrics/``）
            转成友好中文错误，而不是抛原始堆栈。

    Returns:
        ChatOpenAI: 已配置 timeout 与 HTTP 层 max_retries 的模型实例。

    Raises:
        AgentConfigError: 开关关闭 / 缺 key / 模型名仍是占位符。
            构造过程**不发起任何网络请求**。
    """
    _ensure_ready(want_vision=want_vision)

    model_name = resolve_vision_model_name() if want_vision else settings.LLM_MODEL

    model_kwargs: dict[str, object] = {}
    if want_json:
        # DeepSeek 兼容 OpenAI 的 JSON Output；不支持时上层自动降级
        model_kwargs["response_format"] = {"type": "json_object"}

    model = ChatOpenAI(
        model=model_name,
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        temperature=temperature,
        timeout=settings.AGENT_NODE_TIMEOUT_SECONDS,
        # HTTP 层重试由 SDK 负责（网络抖动）；「逻辑重试」由 LangGraph 节点的
        # RetryPolicy 负责（429/5xx/JSON 解析失败）。两者分层，故意都配上。
        max_retries=settings.AGENT_NODE_MAX_RETRIES,
        model_kwargs=model_kwargs,
    )
    logger.info(
        "已构造 ChatOpenAI（model=%s base_url=%s json_mode=%s vision=%s）",
        model_name,
        settings.LLM_BASE_URL,
        want_json,
        want_vision,
    )
    return model
