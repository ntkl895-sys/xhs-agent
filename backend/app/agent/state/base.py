"""统一状态结构：所有业务图都从 :class:`BaseAgentState` 继承。

LangGraph 的状态是一个 ``TypedDict``——节点返回的部分字典会被**合并**回状态。
为避免「同一个 key 被多个节点覆盖」这类经典坑，这里约定：

- 需要**追加**的字段用 ``Annotated[list, operator.add]``（errors / node_trace）；
- 其余字段为**覆盖**语义（后写覆盖先写）。

注意：状态会被序列化进检查点，因此**只放可 JSON 序列化的数据**，
且**绝不允许放入 API Key / 密钥**。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class BaseAgentState(TypedDict, total=False):
    """Agent 运行的统一最小状态。

    Attributes:
        run_id: 本次运行的 UUID（与 ``agent_runs.id`` 对应）。
        thread_id: LangGraph 线程 id（检查点按它归属，interrupt/resume 依赖它）。
        kind: 运行种类（对应 ``RunType`` 值，如 ``other`` / ``topic_generation``）。
        account_id: 关联账号 id（可空）。
        persona_id: 关联人设 id（可空）。
        input: 运行入参快照（可 JSON 序列化）。
        output: 最终产出（结构化对象 / dict）。
        errors: 累计的错误摘要（**追加**语义）。
        parse_attempts: 结构化输出的解析尝试次数（理想为 1）。
        model: 实际使用的模型 ID。
        usage: token 用量 ``{input_tokens, output_tokens, total_tokens}``。
        node_trace: 节点轨迹（节点名 / 耗时 / 状态 / 错误摘要），**追加**语义。
        human_payload: 人在回路待确认载荷（``interrupt()`` 抛出后由外部读取）。
    """

    run_id: str
    thread_id: str
    kind: str
    account_id: str | None
    persona_id: str | None
    input: dict[str, Any]
    output: dict[str, Any] | None
    errors: Annotated[list[str], operator.add]
    parse_attempts: int
    model: str
    usage: dict[str, int | None]
    node_trace: Annotated[list[dict[str, Any]], operator.add]
    human_payload: dict[str, Any] | None


def initial_state(
    *,
    run_id: str,
    thread_id: str,
    kind: str,
    inputs: dict[str, Any] | None = None,
    account_id: str | None = None,
    persona_id: str | None = None,
    model: str = "",
) -> BaseAgentState:
    """构造一个填好默认值的初始状态。

    业务图通常直接用它作为 ``graph.invoke`` 的输入，再自行追加业务字段。
    """
    return BaseAgentState(
        run_id=run_id,
        thread_id=thread_id,
        kind=kind,
        account_id=account_id,
        persona_id=persona_id,
        input=dict(inputs or {}),
        output=None,
        errors=[],
        parse_attempts=0,
        model=model,
        usage={},
        node_trace=[],
        human_payload=None,
    )


_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens")


def merge_usage(
    current: dict[str, int | None] | None,
    incoming: dict[str, int | None] | None,
) -> dict[str, int | None]:
    """把新一次的 token 用量**累加**到已有用量上。

    ``usage`` 在状态里是覆盖语义，多节点调用 LLM 时必须显式调用本函数合并，
    否则后一个节点会覆盖前一个节点的用量，导致成本统计偏低。
    """
    merged: dict[str, int | None] = dict(current or {})
    for key in _USAGE_KEYS:
        value = (incoming or {}).get(key)
        if isinstance(value, int):
            merged[key] = (merged.get(key) or 0) + value
    return merged


def merge_parse_attempts(current: int | None, incoming: int | None) -> int:
    """多次结构化调用取**较大**的尝试次数（反映最差情况，便于发现不稳定）。"""
    if not isinstance(incoming, int) or incoming <= 0:
        return int(current or 0)
    return max(int(current or 0), incoming)


__all__ = ["BaseAgentState", "initial_state", "merge_usage", "merge_parse_attempts"]
