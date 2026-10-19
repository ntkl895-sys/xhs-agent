"""稳健 JSON 解析与结构化输出重试。

移植自 ``D:\\项目1\\topic_spike`` 中**已实测验证**的解析逻辑（该 spike 用真实
DeepSeek 跑通过），并按 LangGraph 基座的需要做了两点扩展：

1. 支持从噪声中抽取**数组**（不只是对象）——有些模型的顶层输出是 ``[...]``；
2. 增加 :func:`invoke_json`：把「JSON 模式 → 不支持则降级 → 解析失败则纠正重问」
   的完整闭环封装成一个函数，返回 ``(payload, attempts)``，其中 ``attempts``
   可直接被上层记录为 ``agent_runs.json_parse_attempts``。

解析策略（由易到难逐层兜底）：

1. 直接 ``json.loads``；
2. 剥离 ```` ```json ... ``` ```` 代码围栏后再解析；
3. 按括号平衡扫描出**首个**完整 JSON 对象 / 数组后再解析；
4. 每一步失败时都会再试一次「修复字符串字面量内裸控制字符」的版本。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.agent.errors import (
    AgentJSONError,
    AgentNonRetryableError,
    AgentRetryableError,
    classify_exception,
)

logger = logging.getLogger("xhs-agent.agent.json")

_FENCE_PATTERN = re.compile(r"```[a-zA-Z]*\s*|\s*```")

#: 纠正提示：解析失败后追加给模型，要求只输出 JSON
_CORRECTION_PROMPT = (
    "你上一次的输出无法被解析为合法 JSON。请**只**输出一个合法的 JSON，"
    "不要包含 ``` 代码围栏、不要任何解释性文字、不要省略号。"
    "字符串值内不要出现未转义的换行或制表符。"
)


# ---------------------------------------------------------------------------
# 文本层：从脏输出里抢救 JSON
# ---------------------------------------------------------------------------
def strip_code_fence(text: str) -> str:
    """剥离 ```` ```json ... ``` ```` 代码围栏，保留内部内容。"""
    return _FENCE_PATTERN.sub("", text).strip()


def sanitize_control_chars(text: str) -> str:
    """把 JSON **字符串字面量内部**的裸控制字符转成合法转义。

    真实模型经常在字符串值里原样输出换行 / 制表符，标准 ``json`` 解析会报
    ``Invalid control character``。本函数只处理字符串内部，字符串外的换行保持原样，
    因此不会破坏 JSON 结构。
    """
    result: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if not in_string:
            if char == '"':
                in_string = True
            result.append(char)
            continue
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\":
            result.append(char)
            escaped = True
            continue
        if char == '"':
            in_string = False
            result.append(char)
            continue
        code = ord(char)
        if code == 0x0A:
            result.append("\\n")
        elif code == 0x0D:
            result.append("\\r")
        elif code == 0x09:
            result.append("\\t")
        elif code < 0x20:
            continue
        else:
            result.append(char)
    return "".join(result)


def _try_loads(text: str) -> Any | None:
    """尝试解析成 dict / list；失败时先修字符串内裸控制字符再试一次。"""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        try:
            payload = json.loads(sanitize_control_chars(text))
        except (json.JSONDecodeError, TypeError):
            return None
    except TypeError:
        return None
    return payload if isinstance(payload, (dict, list)) else None


def _scan_balanced(text: str, start: int, open_ch: str, close_ch: str) -> str | None:
    """从 ``start`` 处的开括号开始按括号平衡扫描出完整片段。

    扫描过程中会跳过字符串字面量，避免 ``"{{"`` 之类的误判。
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == open_ch:
            depth += 1
        elif char == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_json(raw_text: str) -> Any | None:
    """从可能是脏输出的文本里提取 JSON 对象或数组。

    依次尝试：直接解析 → 去围栏后解析 → 扫描首个平衡 JSON 片段后解析。

    Args:
        raw_text: 模型原始输出，可能含围栏、前后废话、多个对象。

    Returns:
        dict / list；``None`` 表示彻底失败。
    """
    payload, _source = extract_json_with_source(raw_text)
    return payload


def extract_json_with_source(raw_text: str) -> tuple[Any | None, str]:
    """同 :func:`extract_json`，额外返回「解析命中方式」便于日志排障。"""
    if not raw_text or not raw_text.strip():
        return None, "输出为空"

    for candidate, source in ((raw_text, "原文"), (strip_code_fence(raw_text), "去围栏")):
        payload = _try_loads(candidate) if candidate else None
        if payload is not None:
            return payload, source

    # 最后手段：定位首个 '{' 或 '['，按括号平衡截取
    for open_ch, close_ch, label in (("{", "}", "平衡扫描(对象)"), ("[", "]", "平衡扫描(数组)")):
        start = raw_text.find(open_ch)
        while start != -1:
            segment = _scan_balanced(raw_text, start, open_ch, close_ch)
            if segment:
                payload = _try_loads(segment)
                if payload is not None:
                    return payload, label
            start = raw_text.find(open_ch, start + 1)

    return None, "三层兜底均未命中"


# ---------------------------------------------------------------------------
# 调用层：结构化输出 + 自动降级 + 纠正重问
# ---------------------------------------------------------------------------
@dataclass
class JsonInvokeResult:
    """一次「要 JSON」的模型调用结果。"""

    payload: Any
    #: 总尝试次数（1 表示一次命中，理想值恒为 1）
    attempts: int
    raw_text: str
    #: 是否实际使用了 ``response_format=json_object``
    json_mode_used: bool
    warnings: list[str] = field(default_factory=list)
    #: 模型返回的 token 用量（若有）
    usage: dict[str, int | None] | None = None


def _extract_text(response: Any) -> str:
    """从 LangChain 的 AIMessage 里取正文文本。"""
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content
    # 有些模型返回 content blocks（list[dict]），拼出文本部分
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)


def _extract_usage(response: Any) -> dict[str, int | None] | None:
    """从响应里取 token 用量（新版字段为 ``usage_metadata``）。"""
    usage = getattr(response, "usage_metadata", None)
    if not isinstance(usage, dict):
        return None
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def _looks_like_json_mode_rejection(exc: BaseException) -> bool:
    """判断异常是否为「端点不支持 json_object」这类拒绝。

    典型表现：400 BadRequest，消息里含 response_format / json_object / json mode。
    """
    text = str(exc).lower()
    markers = ("response_format", "json_object", "json mode", "json mode is not", "unsupported")
    return any(marker in text for marker in markers)


def invoke_json(
    model: Any,
    messages: list[BaseMessage],
    *,
    max_attempts: int | None = None,
    want_json: bool = True,
) -> JsonInvokeResult:
    """调用模型并确保拿到可解析的 JSON，返回 ``(payload, attempts)``。

    流程：优先带 ``response_format=json_object`` → 若端点拒绝则**自动降级**为
    「提示词约束 + 文本解析」→ 每次拿到输出后尝试解析 → 解析失败则追加纠正提示重问，
    最多 ``max_attempts`` 次。

    Args:
        model: 任何 LangChain ChatModel（含 FakeListChatModel）。
        messages: 基础消息列表（system + user）。
        max_attempts: 最多尝试次数；默认取 ``settings.AGENT_JSON_MAX_ATTEMPTS``。
        want_json: 是否先尝试 JSON 模式。

    Returns:
        JsonInvokeResult: payload 与 attempts（可直接记为 json_parse_attempts）。

    Raises:
        AgentRetryableError: 网络 / 限流 / 5xx 等可重试错误（交给节点的 RetryPolicy）。
        AgentNonRetryableError: 4xx / 鉴权 / 参数错误，立即失败。
        AgentJSONError: 耗尽尝试次数后仍无法解析为 JSON。
    """
    from app.core.config import settings  # 延迟导入，避免循环依赖

    attempts_limit = max_attempts or settings.AGENT_JSON_MAX_ATTEMPTS
    warnings: list[str] = []
    use_json_mode = bool(want_json)

    conversation: list[BaseMessage] = list(messages)
    attempt = 0
    last_text = ""
    last_usage: dict[str, int | None] | None = None

    while True:
        attempt += 1
        try:
            response = model.invoke(conversation)
        except AgentJSONError:
            raise
        except BaseException as exc:  # noqa: BLE001 —— 统一分类后再抛
            if use_json_mode and _looks_like_json_mode_rejection(exc):
                # 端点不支持 json_object：降级为「提示词约束 + 文本解析」，
                # 本次不计入解析尝试（还没拿到输出）。
                warnings.append(
                    "端点不支持 response_format=json_object，已降级为提示词约束 + 文本解析"
                )
                logger.warning(warnings[-1])
                use_json_mode = False
                attempt -= 1
                continue
            # 其他错误按类型抛出：可重试的给 RetryPolicy，不可重试的立即失败
            raise classify_exception(exc) from exc

        last_text = _extract_text(response)
        last_usage = _extract_usage(response) or last_usage
        payload = extract_json(last_text)
        if payload is not None:
            return JsonInvokeResult(
                payload=payload,
                attempts=attempt,
                raw_text=last_text,
                json_mode_used=use_json_mode,
                warnings=warnings,
                usage=last_usage,
            )

        if attempt >= attempts_limit:
            raise AgentJSONError(
                f"连续 {attempt} 次输出均无法解析为 JSON（上限 {attempts_limit}）",
                raw_text=last_text,
            )

        logger.warning("第 %d 次输出不是合法 JSON，追加纠正提示后重试", attempt)
        conversation.append(AIMessage(content=last_text or "(空输出)"))
        conversation.append(HumanMessage(content=_CORRECTION_PROMPT))


def parse_or_raise(raw_text: str) -> Any:
    """纯文本解析入口（不调用模型）。失败抛 :class:`AgentJSONError`。"""
    payload, source = extract_json_with_source(raw_text)
    if payload is None:
        raise AgentJSONError(f"无法从文本中提取 JSON（{source}）", raw_text=raw_text)
    return payload


__all__ = [
    "JsonInvokeResult",
    "extract_json",
    "extract_json_with_source",
    "invoke_json",
    "parse_or_raise",
    "sanitize_control_chars",
    "strip_code_fence",
]
