"""运行审计：把每次 Agent 运行写入 ``agent_runs``。

用途（这也是 ``agent_runs`` 表存在的理由）：

- **成本**：只记 token 数，价格由外部配置决定（本仓库**不内置价格表**）；
- **JSON 稳定性**：记 ``json_parse_attempts``（理想恒为 1）；
- **排障**：记节点轨迹 ``node_trace``、耗时、错误摘要。

**安全红线**：任何字段都不得包含 API Key。除了「上游本来就不该传 key 进来」，
本模块还额外做了一层**递归脱敏**（:func:`redact_secrets`）作为兜底，
自检脚本会断言写出的行里不含 ``sk-`` 形态的密钥。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from app.agent.llm.factory import PLACEHOLDER_MODEL
from app.core.config import settings
from app.models.agent_run import AgentRun
from app.models.enums import RunStatus, RunTrigger, RunType

logger = logging.getLogger("xhs-agent.agent.recorder")

#: 常见密钥形态（OpenAI / DeepSeek 均以 sk- 开头）
_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")

#: 命中这些「键名」时，值一律替换为占位符
_SECRET_KEY_HINTS = ("api_key", "apikey", "api-key", "secret", "password", "token", "authorization")

REDACTED = "[REDACTED]"


def redact_secrets(value: Any) -> Any:
    """递归脱敏：把疑似密钥替换成 ``[REDACTED]``。

    双重保险：① 键名命中常见密钥名 → 整值替换；② 值里出现 ``sk-xxx`` 形态 → 正则替换。
    """
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and any(hint in key.lower() for hint in _SECRET_KEY_HINTS):
                cleaned[key] = REDACTED
            else:
                cleaned[key] = redact_secrets(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _SECRET_PATTERN.sub(REDACTED, value)
    return value


def map_run_type(kind: str) -> RunType:
    """把业务 ``kind`` 映射到 ``RunType``；未知的一律落到 ``other``。"""
    normalized = (kind or "").strip().lower()
    for member in RunType:
        if member.value == normalized:
            return member
    return RunType.OTHER


def map_trigger(trigger: str | None) -> RunTrigger:
    """把触发方式映射到 ``RunTrigger``（默认 manual）。"""
    normalized = (trigger or "").strip().lower()
    for member in RunTrigger:
        if member.value == normalized:
            return member
    return RunTrigger.MANUAL


class AgentRunRecorder:
    """一次 Agent 运行的审计记录器（上下文管理器）。

    用法：

    .. code-block:: python

        with AgentRunRecorder(run_type="topic_generation", kind="topic_generation",
                              inputs={"n": 5}) as rec:
            output = graph.invoke(state, config)
            rec.record_usage(output.get("usage"))
            rec.record_parse_attempts(output.get("parse_attempts"))
            for node in output.get("node_trace", []):
                rec.add_node_trace(**node)

    进入时插入一行（status 暂空），退出时回写 status / token / latency / 错误。
    即便业务抛异常，也会被记成 ``failed`` 并**继续向上抛**（不吞异常）。
    """

    def __init__(
        self,
        *,
        run_type: str | RunType = RunType.OTHER,
        kind: str = "",
        trigger: str | RunTrigger | None = None,
        inputs: dict[str, Any] | None = None,
        account_id: Any = None,
        persona_id: Any = None,
        note_id: Any = None,
        topic_id: Any = None,
        llm_model: str | None = None,
        llm_base_url: str | None = None,
        session: Any = None,
    ) -> None:
        self.run_type = run_type if isinstance(run_type, RunType) else map_run_type(run_type)
        self.kind = kind or self.run_type.value
        self.trigger = trigger if isinstance(trigger, RunTrigger) else map_trigger(trigger)
        self.inputs = redact_secrets(dict(inputs or {}))
        self.account_id = account_id
        self.persona_id = persona_id
        self.note_id = note_id
        self.topic_id = topic_id
        self.llm_model = llm_model if llm_model is not None else settings.LLM_MODEL
        self.llm_base_url = llm_base_url if llm_base_url is not None else settings.LLM_BASE_URL

        self.run_id: Any = None
        self._session = session
        self._owns_session = session is None
        self._started = time.perf_counter()
        self._usage: dict[str, int | None] = {}
        self._parse_attempts: int | None = None
        self._node_trace: list[dict[str, Any]] = []
        self._outputs: dict[str, Any] | None = None
        self._finished = False

    # ------------------------------------------------------------ 生命周期
    def __enter__(self) -> AgentRunRecorder:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None:
            self.finish(status=RunStatus.SUCCESS)
        else:
            self.finish(
                status=RunStatus.FAILED,
                error_message=f"{exc_type.__name__}: {exc}" if exc_type else None,
            )
        # 返回 False：异常继续向上抛，绝不静默吞掉
        return False

    def _get_session(self) -> Any:
        if self._session is None:
            from app.db.session import SessionLocal

            self._session = SessionLocal()
        return self._session

    def _close_session(self) -> None:
        if self._owns_session and self._session is not None:
            self._session.close()
            self._session = None

    def start(self) -> Any:
        """插入审计行的初始状态（只有到这一步失败才算致命）。"""
        session = self._get_session()
        row = AgentRun(
            run_type=self.run_type,
            trigger=self.trigger,
            account_id=self.account_id,
            persona_id=self.persona_id,
            note_id=self.note_id,
            topic_id=self.topic_id,
            inputs=self.inputs,
            llm_model=self.llm_model,
            llm_base_url=self.llm_base_url,
            started_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        self.run_id = row.id
        return self.run_id

    # ------------------------------------------------------------ 记录器
    def record_usage(self, usage: dict[str, int | None] | None) -> None:
        """累计 token 用量（多次调用会相加）。"""
        if not usage:
            return
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                self._usage[key] = (self._usage.get(key) or 0) + value

    def record_parse_attempts(self, attempts: int | None) -> None:
        """记录结构化输出的解析尝试次数（多次取较大值，反映最差情况）。"""
        if not isinstance(attempts, int):
            return
        self._parse_attempts = (
            attempts if self._parse_attempts is None else max(self._parse_attempts, attempts)
        )

    def add_node_trace(
        self,
        node: str,
        *,
        duration_ms: float | int | None = None,
        status: str = "ok",
        error: str | None = None,
        **extra: Any,
    ) -> None:
        """追加一条节点轨迹（节点名 / 耗时 / 状态 / 错误摘要）。"""
        entry: dict[str, Any] = {
            "node": node,
            "status": status,
            "duration_ms": round(float(duration_ms), 3) if duration_ms is not None else None,
        }
        if error:
            entry["error"] = _SECRET_PATTERN.sub(REDACTED, str(error))[:500]
        if extra:
            entry.update(redact_secrets(extra))
        self._node_trace.append(entry)

    def set_outputs(self, outputs: dict[str, Any] | None) -> None:
        """记录产出摘要（id 列表 / 统计），自动脱敏。"""
        self._outputs = redact_secrets(outputs) if outputs else None

    # ------------------------------------------------------------ 收尾
    def finish(
        self,
        *,
        status: RunStatus | str = RunStatus.SUCCESS,
        error_message: str | None = None,
        outputs: dict[str, Any] | None = None,
    ) -> None:
        """回写运行结果（幂等：重复调用只生效一次）。"""
        if self._finished:
            return
        self._finished = True

        if not isinstance(status, RunStatus):
            status = RunStatus(str(status)) if str(status) in {m.value for m in RunStatus} else RunStatus.FAILED
        if outputs is not None:
            self.set_outputs(outputs)

        latency_ms = int((time.perf_counter() - self._started) * 1000)
        session = self._get_session()
        try:
            row = session.get(AgentRun, self.run_id)
            if row is None:  # 理论不会发生
                logger.warning("agent_runs 行 %s 不存在，跳过回写", self.run_id)
                return
            row.status = status
            row.outputs = {
                **(self._outputs or {}),
                "kind": self.kind,
                "node_trace": self._node_trace,
            }
            row.prompt_tokens = self._usage.get("input_tokens")
            row.completion_tokens = self._usage.get("output_tokens")
            row.total_tokens = self._usage.get("total_tokens")
            row.json_parse_attempts = self._parse_attempts
            row.latency_ms = latency_ms
            row.finished_at = datetime.now(timezone.utc)
            if error_message:
                # 兜底脱敏：错误信息里若混入密钥形态也一并抹掉
                row.error_message = _SECRET_PATTERN.sub(REDACTED, str(error_message))[:2000]
            session.commit()
        finally:
            self._close_session()


__all__ = [
    "AgentRunRecorder",
    "redact_secrets",
    "map_run_type",
    "map_trigger",
    "PLACEHOLDER_MODEL",
    "REDACTED",
]
