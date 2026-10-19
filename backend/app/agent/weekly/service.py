"""周报服务层：编排「运营周期 → 一次周度复盘 → 审计回填」（第 17 步）。

职责与边界（与第 12 / 15 / 16 步 service 同范式）：

- **零业务表写入**：周报结论**不落业务表**，整体写入 ``agent_runs.outputs``
  （``run_type="weekly_report"``）——这是本步既定口径；周报绝不修改
  notes / topics / note_metrics / diagnostics，也不触发任何发布动作；
- **异常必须抛出**：翻译成统一错误体（AppError）由 ``app.main`` 渲染——
  422（周期参数非法）/ 400（未配置、JSON 重试超限 / 其它运行失败）；
  **响应不含 Traceback、不含 llm_raw、不含 key**；
- **审计回填**（不改 harness）：``run_agent`` 成功时只写 output 的键名，
  周报本体与 ``llm_raw`` 由本层用返回的 ``run_id`` 在 ``agent_runs`` 上补齐；
  ``llm_raw`` 写入前过 :func:`redact_secrets` 并截断到 20000（同既有口径）；
- **模型注入**：``model=None`` 时用 ``get_chat_model(temperature=0.2,
  want_json=True)``（未配置 → ``AgentConfigError`` → 本层译 400，**零请求、
  零 agent_runs**）；离线自检经 ``model`` 参数注入假模型，全程零真实请求；
- **周期参数先校验**：非法周期在跑图前直接 422（**零 agent_runs 产生**）；
  图内 ``load_context`` 会幂等地再校验一次（防御）。

历史查询（``list_weekly_reports`` / ``get_weekly_report``）从 ``agent_runs``
按 ``run_type='weekly_report'`` 过滤，倒序（``created_at desc, id desc``），
放在本层（路由层零 ORM 表达式）。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.agent.errors import AgentConfigError, AgentRetryableError, classify_exception
from app.agent.llm.factory import get_chat_model
from app.agent.runner import run_agent
from app.agent.tracing.recorder import redact_secrets
from app.agent.weekly.contracts import (
    WeeklyContextError,
    WeeklyGiveUpError,
    resolve_period,
)
from app.agent.weekly.graph import build_weekly_report_graph
from app.core.errors import bad_request, validation_error
from app.db.session import SessionLocal
from app.models.agent_run import AgentRun
from app.models.enums import RunTrigger, RunType

logger = logging.getLogger("xhs-agent.weekly.service")

#: 归档前的模型原始输出长度上限（防止超长落库；与第 12 / 15 / 16 步同口径）
LLM_RAW_MAX_LEN = 20000


# ---------------------------------------------------------------------------
# 审计回填（与第 10 / 12 / 15 / 16 步同实现：harness 不改，业务明细在 service 补齐）
# ---------------------------------------------------------------------------
def _backfill_run(
    db: Session,
    run_id: str,
    *,
    patch_outputs: dict[str, Any],
    llm_raw: str | None,
) -> None:
    """用 ``run_id`` 给 ``agent_runs`` 补周报本体（``outputs`` / ``llm_raw``）。

    ``outputs`` 用 ``COALESCE(outputs,'{}'::jsonb) || patch`` **合并保留** harness
    已写内容；``llm_raw`` 写入前过 :func:`redact_secrets` 兜底并截断到
    :data:`LLM_RAW_MAX_LEN`。
    """
    raw_value: str | None = None
    if llm_raw:
        cleaned = redact_secrets(llm_raw)
        raw_value = str(cleaned)[:LLM_RAW_MAX_LEN]

    db.execute(
        sa_text(
            "UPDATE agent_runs SET "
            "outputs = COALESCE(outputs, '{}'::jsonb) || CAST(:patch AS jsonb), "
            "llm_raw = COALESCE(:raw, llm_raw) "
            "WHERE id = :rid"
        ),
        {
            "patch": json.dumps(patch_outputs, ensure_ascii=False, default=str),
            "raw": raw_value,
            "rid": run_id,
        },
    )
    db.commit()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def generate_weekly_report(
    *,
    period_start: Any = None,
    period_end: Any = None,
    days: Any = None,
    trigger: str = RunTrigger.MANUAL.value,
    model: Any | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """跑一次周报并把结论写入 ``agent_runs.outputs``。

    Args:
        period_start / period_end: 显式周期（ISO 时间或 ``YYYY-MM-DD``；可只给一端）。
        days: 「近 N 天」便捷参数（与显式起止互斥）。
        trigger: ``manual`` / ``scheduled``。
        model: 注入的 ChatModel；``None`` 时用
            :func:`~app.agent.llm.factory.get_chat_model`（离线自检由此注入假模型）。
        now: 注入的「当前时间」（测试用；``None`` 取本地当前时刻）。

    Returns:
        ``{run_id, period_start, period_end, report, parse_attempts, data_gaps,
        warnings}`` —— 绝不含 ``llm_raw`` / key / Traceback（自检会静态断言）。

    Raises:
        AppError(422): 周期参数非法（**零 agent_runs 产生**）。
        AppError(400): LLM 未配置（开关 / key / 模型名占位，**不发请求**）；
            结构校验重试超限（``WeeklyGiveUpError``）；其它运行失败。
    """
    # ---- 周期参数（先校验：非法直接 422，不产生任何 run）----
    try:
        # span_days 仅用于本层校验；图内会以显式起止再算一次（见下方 inputs）
        start, end, _span_days = resolve_period(
            period_start=period_start,
            period_end=period_end,
            days=days,
            now=now,
        )
    except WeeklyContextError as exc:
        raise validation_error(str(exc)) from exc

    # ---- 模型（未配置 → AgentConfigError → 400；零请求、零 agent_runs）----
    try:
        chosen_model = (
            model
            if model is not None
            else get_chat_model(temperature=0.2, want_json=True)
        )
    except AgentConfigError as exc:
        raise bad_request("周报未就绪，无法运行", details={"reason": str(exc)}) from exc

    period_start_iso = start.isoformat()
    period_end_iso = end.isoformat()

    try:
        result = run_agent(
            kind="weekly_report",
            build_fn=build_weekly_report_graph,
            build_kwargs={"model": chosen_model},
            # 周期已在本层解析完毕：图内只接收**显式起止**（不再传 days，
            # 避免与图内 resolve_period 的「days 与显式起止互斥」规则冲突）。
            inputs={
                "period_start": period_start_iso,
                "period_end": period_end_iso,
            },
            run_type=RunType.WEEKLY_REPORT,
            trigger=trigger,
            extra_state={
                "period_start": period_start_iso,
                "period_end": period_end_iso,
            },
        )
    except WeeklyContextError as exc:
        # 图内防御性再校验失败（理论上不可达：本层已先校验）
        raise validation_error(str(exc)) from exc
    except WeeklyGiveUpError as exc:
        raise bad_request(str(exc), details={"reason": exc.reason}) from exc
    except AgentConfigError as exc:
        raise bad_request("周报未就绪，无法运行", details={"reason": str(exc)}) from exc
    except Exception as exc:  # noqa: BLE001 —— 分类后统一译成统一错误体
        classified = classify_exception(exc)
        retryable = isinstance(classified, AgentRetryableError)
        # 兜底脱敏：第三方错误文本里若混入密钥形态内容，也不得外泄
        reason = str(redact_secrets(str(classified)))[:500]
        raise bad_request(
            "周报运行失败（可重试，请稍后再试）" if retryable else "周报运行失败",
            details={"reason": reason, "retryable": retryable},
        ) from exc

    run_id = str(result["run_id"])
    state = result.get("state") or {}
    output = result.get("output")

    if not isinstance(output, dict) or not isinstance(output.get("report"), dict):
        # 理论上不可达（persist_run 必然组装 output）；防御：给出可识别错误
        raise bad_request(
            "周报未产出有效结果，请重试", details={"run_id": run_id}
        )

    report = output["report"]

    # ---- 审计回填（周报本体 + llm_raw 脱敏截断；不写任何业务表）----
    with SessionLocal() as db:
        _backfill_run(
            db,
            run_id,
            patch_outputs={
                "report": report,
                "period_start": output.get("period_start"),
                "period_end": output.get("period_end"),
                "parse_attempts": output.get("parse_attempts"),
                "prompt_version": output.get("prompt_version"),
                "persisted": True,
            },
            llm_raw=state.get("llm_raw"),
        )

    summary = {
        "run_id": run_id,
        "period_start": output.get("period_start"),
        "period_end": output.get("period_end"),
        "report": report,
        "parse_attempts": int(result.get("parse_attempts") or 0),
        "data_gaps": list(output.get("data_gaps") or []),
        "warnings": list(output.get("warnings") or []),
    }
    logger.info(
        "周报完成：run=%s 周期=[%s ~ %s] parse_attempts=%s 缺口=%s 建议=%s",
        run_id,
        summary["period_start"],
        summary["period_end"],
        summary["parse_attempts"],
        len(summary["data_gaps"]),
        len(report.get("next_week_suggestions") or []),
    )
    return summary


# ---------------------------------------------------------------------------
# 历史查询（从 agent_runs 过滤 run_type='weekly_report'）
# ---------------------------------------------------------------------------
def list_weekly_reports(db: Session, *, limit: int = 10) -> list[AgentRun]:
    """最近周报（``created_at`` 倒序；``id`` 兜底保证顺序稳定）。

    放在 service 层而非路由层：路由只做参数解析与响应组装，不写 SQLAlchemy
    表达式（第 8 步确立的分层纪律，``selftest_api.py`` 会静态断言）。
    """
    stmt = (
        select(AgentRun)
        .where(AgentRun.run_type == RunType.WEEKLY_REPORT)
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(max(1, limit))
    )
    return list(db.execute(stmt).scalars().all())


def get_weekly_report(db: Session, run_id: uuid.UUID) -> AgentRun | None:
    """按 ``run_id`` 取单条周报（仅 ``run_type='weekly_report'``；不存在 → None）。"""
    stmt = select(AgentRun).where(
        AgentRun.id == run_id,
        AgentRun.run_type == RunType.WEEKLY_REPORT,
    )
    return db.execute(stmt).scalars().first()


__all__ = [
    "LLM_RAW_MAX_LEN",
    "generate_weekly_report",
    "get_weekly_report",
    "list_weekly_reports",
]
