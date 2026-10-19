"""统一【同步】运行入口。

后续所有业务图都通过 :func:`run_agent` 运行，好处是：

- 审计（``agent_runs``）自动就位，不会漏记；
- 异常分类统一：可重试错误由节点的 ``RetryPolicy`` 处理，耗尽后在此**落 failed**；
- interrupt/resume 的恢复语义统一（传 ``resume`` 即走 ``Command(resume=...)``）。

刻意**不引入 asyncio**：与现有 SQLAlchemy 同步会话、同步 service/路由保持一致，
避免同一项目里出现两套并发模型。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from langgraph.types import Command

from app.agent.graph.base import make_config, new_thread_id
from app.agent.tracing.recorder import AgentRunRecorder
from app.core.config import settings
from app.models.enums import RunStatus

logger = logging.getLogger("xhs-agent.agent.runner")


def _extract_interrupt(result: dict[str, Any]) -> dict[str, Any] | None:
    """从运行结果里取出 interrupt 载荷（有则说明图停在人在回路处）。"""
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    first = interrupts[0]
    value = getattr(first, "value", first)
    if isinstance(value, dict):
        return value
    return {"value": value}


def describe_retry_semantics() -> dict[str, Any]:
    """把「重试语义」讲清楚，供文档与自检引用。"""
    retries = max(1, settings.AGENT_NODE_MAX_RETRIES)
    return {
        "node_max_retries": retries,
        "node_max_attempts": retries + 1,  # LangGraph 的 max_attempts = 1 + 重试次数
        "retry_on": "AgentRetryableError(429/5xx/网络异常/JSON解析失败)",
        "no_retry_on": "AgentNonRetryableError(4xx/鉴权/参数错误)",
        "json_max_attempts": settings.AGENT_JSON_MAX_ATTEMPTS,
        "worst_case_model_calls": settings.AGENT_JSON_MAX_ATTEMPTS * (retries + 1),
        "recursion_limit": settings.AGENT_RECURSION_LIMIT,
        "node_timeout_seconds": settings.AGENT_NODE_TIMEOUT_SECONDS,
    }


def run_agent(
    kind: str,
    build_fn: Callable[..., Any],
    inputs: dict[str, Any] | None = None,
    account_id: Any = None,
    persona_id: Any = None,
    resume: dict[str, Any] | Any | None = None,
    *,
    thread_id: str | None = None,
    build_args: tuple[Any, ...] = (),
    build_kwargs: dict[str, Any] | None = None,
    run_type: str | None = None,
    trigger: str | None = None,
    extra_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """同步运行一张图，并把全过程记进 ``agent_runs``。

    Args:
        kind: 运行种类（映射到 ``RunType``，同时写进 ``outputs.kind``）。
        build_fn: **无参**工厂，返回已编译的图（或 ``(graph, state)`` 二元组）。
            传工厂而不是图实例，是为了让每次运行都能拿到最新的检查点单例。
        inputs: 运行入参（会写进 ``agent_runs.inputs``）。
        account_id / persona_id: 关联实体 id（可空）。
        resume: 非空时走 ``Command(resume=...)`` —— 用于 interrupt 恢复。
        thread_id: 指定线程 id；不传则新生成。**恢复时必须传同一个**。
        build_args / build_kwargs: 透传给 ``build_fn``。
        run_type: 覆盖 ``RunType``（默认由 ``kind`` 推导）。
        trigger: ``manual`` / ``scheduled``。
        extra_state: 追加到初始状态的字段（业务图自有字段）。

    Returns:
        dict: ``{run_id, thread_id, output, usage, parse_attempts, status,
        interrupted, interrupt_payload}``
    """
    from app.agent.state.base import initial_state

    tid = thread_id or new_thread_id()
    built = build_fn(*build_args, **(build_kwargs or {}))

    # 允许 build_fn 返回 (graph, state_extra) 以携带业务状态
    graph: Any
    state_extra: dict[str, Any] = {}
    if isinstance(built, tuple) and len(built) == 2:
        graph, state_extra = built
    else:
        graph = built

    state = initial_state(
        run_id="",  # 下面用真实 run_id 覆盖
        thread_id=tid,
        kind=kind,
        inputs=inputs,
        account_id=str(account_id) if account_id else None,
        persona_id=str(persona_id) if persona_id else None,
        model=settings.LLM_MODEL,
    )
    state.update(state_extra or {})
    state.update(extra_state or {})

    rec = AgentRunRecorder(
        run_type=run_type or kind,
        kind=kind,
        trigger=trigger,
        inputs={"kind": kind, **(inputs or {})},
        account_id=account_id,
        persona_id=persona_id,
    )
    config = make_config(tid)

    with rec:
        state["run_id"] = str(rec.run_id)
        payload: Any = Command(resume=resume) if resume is not None else state

        try:
            result = graph.invoke(payload, config)
        except BaseException as exc:  # noqa: BLE001 —— 分类后记 failed 再抛
            logger.warning("图运行失败（kind=%s thread=%s）：%s", kind, tid, exc)
            rec.finish(status=RunStatus.FAILED, error_message=f"{type(exc).__name__}: {exc}")
            raise

        # 汇总审计信息
        rec.record_usage(result.get("usage"))
        rec.record_parse_attempts(result.get("parse_attempts"))
        for node in result.get("node_trace") or []:
            if isinstance(node, dict):
                rec.add_node_trace(
                    node.get("node", "unknown"),
                    duration_ms=node.get("duration_ms"),
                    status=node.get("status", "ok"),
                    error=node.get("error"),
                )
        interrupt_payload = _extract_interrupt(result)
        rec.set_outputs(
            {
                "kind": kind,
                "interrupt": interrupt_payload,
                "output_keys": sorted((result.get("output") or {}).keys())
                if isinstance(result.get("output"), dict)
                else [],
            }
        )
        status = RunStatus.PARTIAL if interrupt_payload else RunStatus.SUCCESS
        rec.finish(status=status)

    return {
        "run_id": str(rec.run_id),
        "thread_id": tid,
        "output": result.get("output"),
        "usage": result.get("usage") or {},
        "parse_attempts": result.get("parse_attempts"),
        "status": status.value,
        "interrupted": interrupt_payload is not None,
        "interrupt_payload": interrupt_payload,
        "state": result,
    }


__all__ = ["run_agent", "describe_retry_semantics", "_extract_interrupt"]
