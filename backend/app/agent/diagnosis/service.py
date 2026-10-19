"""单篇诊断服务层：编排「单篇笔记 → 一次诊断结论 → 审计回填」（第 16 步）。

职责与边界（与第 12 步创作 / 第 15 步识别 service 同范式）：

- **落库但只落诊断**：跑图成功后 ``diagnostics`` 恰新增一条（由图内 ``persist``
  节点在**单事务**内完成）；诊断**绝不修改** notes / topics / note_metrics，
  也不触发任何发布动作——只读 + 建议是硬红线；
- **异常必须抛出**（同识别 service）：翻译成统一错误体（AppError）由
  ``app.main`` 渲染——404（笔记不存在）/ 422（入参不合法）/ 400（未配置、
  JSON 重试超限 / 其它运行失败）；**响应不含 Traceback、不含 llm_raw**；
- **审计回填**（不改 harness）：``run_agent`` 成功时只写 output 的键名，
  诊断摘要与 ``llm_raw`` 由本层用返回的 ``run_id`` 在 ``agent_runs`` 上补齐；
  ``llm_raw`` 写入前过 :func:`redact_secrets` 并截断；``note_id`` 同样按
  run_id 回填（``run_agent`` 不转发 note_id，基座文件本步禁止改动）；
- **模型注入**：``model=None`` 时用 ``get_chat_model(temperature=0.2,
  want_json=True)``（未配置 → ``AgentConfigError`` → 本层译 400，**零请求、
  零 agent_runs**）；离线自检经 ``model`` 参数注入假模型，全程零真实请求。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.agent.diagnosis.contracts import DiagnosisContextError, DiagnosisGiveUpError
from app.agent.diagnosis.graph import build_diagnosis_graph
from app.agent.errors import AgentConfigError, AgentRetryableError, classify_exception
from app.agent.llm.factory import get_chat_model
from app.agent.runner import run_agent
from app.agent.tracing.recorder import redact_secrets
from app.core.errors import bad_request, not_found, validation_error
from app.db.session import SessionLocal
from app.models.enums import DiagnosisType, RunTrigger, RunType
from app.models.note import Note

logger = logging.getLogger("xhs-agent.diagnosis.service")

#: 归档前的模型原始输出长度上限（防止超长落库；与第 12 / 15 步同口径）
LLM_RAW_MAX_LEN = 20000

#: 合法的诊断类型取值（与 ``DiagnosisType`` 枚举一致，勿手写）
_DIAGNOSIS_TYPE_VALUES = frozenset(member.value for member in DiagnosisType)


# ---------------------------------------------------------------------------
# 审计回填（与第 10 / 12 / 15 步同实现：harness 不改，业务明细在 service 补齐）
# ---------------------------------------------------------------------------
def _backfill_run(
    db: Session,
    run_id: str,
    *,
    patch_outputs: dict[str, Any],
    llm_raw: str | None,
    note_id: Any = None,
) -> None:
    """用 ``run_id`` 给 ``agent_runs`` 补诊断摘要（``outputs`` / ``llm_raw`` / ``note_id``）。

    ``outputs`` 用 ``COALESCE(outputs,'{}'::jsonb) || patch`` **合并保留** harness
    已写内容；``llm_raw`` 写入前过 :func:`redact_secrets` 兜底（**断言不含密钥前缀**）
    并截断到 :data:`LLM_RAW_MAX_LEN`。
    """
    raw_value: str | None = None
    if llm_raw:
        cleaned = redact_secrets(llm_raw)
        raw_value = str(cleaned)[:LLM_RAW_MAX_LEN]

    db.execute(
        sa_text(
            "UPDATE agent_runs SET "
            "outputs = COALESCE(outputs, '{}'::jsonb) || CAST(:patch AS jsonb), "
            "llm_raw = COALESCE(:raw, llm_raw), "
            "note_id = COALESCE(:note_id, note_id) "
            "WHERE id = :rid"
        ),
        {
            "patch": json.dumps(patch_outputs, ensure_ascii=False, default=str),
            "raw": raw_value,
            "note_id": str(note_id) if note_id else None,
            "rid": run_id,
        },
    )
    db.commit()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def diagnose_note(
    *,
    note_id: uuid.UUID | str,
    diagnosis_type: str | None = None,
    trigger: str = RunTrigger.MANUAL.value,
    model: Any | None = None,
) -> dict[str, Any]:
    """对单篇笔记跑一次诊断并把结论落库（``diagnostics``）。

    Args:
        note_id: 目标笔记 id（必填；不存在 → 404，在跑图前给出，不产生运行记录）。
        diagnosis_type: 可选的手动诊断类型（``cold_start`` / ``review`` /
            ``anomaly``）；``None`` 时由本地纯函数自动判定。
        trigger: ``manual`` / ``scheduled``（本步仅 manual）。
        model: 注入的 ChatModel；``None`` 时用
            :func:`~app.agent.llm.factory.get_chat_model`（离线自检由此注入假模型）。

    Returns:
        ``{diagnostic_id, note_id, diagnosis_type, diagnosis_type_source, score,
        parse_attempts, data_gaps, warnings, run_id}``
        —— 绝不含 ``llm_raw`` / key / Traceback（自检会静态断言）。

    Raises:
        AppError(422): ``note_id`` 不是合法 UUID / ``diagnosis_type`` 不合法。
        AppError(404): 笔记不存在。
        AppError(400): LLM 未配置（开关 / key / 模型名占位，**不发请求**）；
            结构校验重试超限（``DiagnosisGiveUpError``）；其它运行失败。
    """
    # ---- 入参（防御；路由层 schema 已先行校验，正常不触发）----
    try:
        note_uuid = uuid.UUID(str(note_id))
    except (TypeError, ValueError) as exc:
        raise validation_error(
            "note_id 不是合法 UUID", details={"note_id": str(note_id)}
        ) from exc

    manual_type: str | None = None
    if diagnosis_type:
        candidate = str(diagnosis_type).strip()
        if candidate:
            if candidate not in _DIAGNOSIS_TYPE_VALUES:
                raise validation_error(
                    "diagnosis_type 不合法（仅支持 cold_start / review / anomaly）",
                    details={"diagnosis_type": candidate},
                )
            manual_type = candidate

    # ---- 笔记存在性 + 归属（404 前置：不产生 agent_runs 行）----
    with SessionLocal() as db:
        note = db.get(Note, note_uuid)
        if note is None:
            raise not_found("笔记不存在，无法诊断", details={"note_id": str(note_uuid)})
        account_id = note.account_id
        persona_id = note.persona_id

    # ---- 模型（未配置 → AgentConfigError → 400；零请求、零 agent_runs）----
    try:
        chosen_model = (
            model
            if model is not None
            else get_chat_model(temperature=0.2, want_json=True)
        )
    except AgentConfigError as exc:
        raise bad_request(
            "单篇诊断未就绪，无法运行", details={"reason": str(exc)}
        ) from exc

    extra_state: dict[str, Any] = {"note_id": str(note_uuid)}
    if manual_type:
        extra_state["diagnosis_type"] = manual_type

    try:
        result = run_agent(
            kind="diagnosis",
            build_fn=build_diagnosis_graph,
            build_kwargs={"model": chosen_model},
            inputs={"note_id": str(note_uuid), "diagnosis_type": manual_type},
            account_id=account_id,
            persona_id=persona_id,
            run_type=RunType.DIAGNOSIS,
            trigger=trigger,
            extra_state=extra_state,
        )
    except DiagnosisContextError as exc:
        raise not_found(str(exc), details={"note_id": str(note_uuid)}) from exc
    except DiagnosisGiveUpError as exc:
        raise bad_request(str(exc), details={"reason": exc.reason}) from exc
    except AgentConfigError as exc:
        raise bad_request(
            "单篇诊断未就绪，无法运行", details={"reason": str(exc)}
        ) from exc
    except Exception as exc:  # noqa: BLE001 —— 分类后统一译成统一错误体
        classified = classify_exception(exc)
        retryable = isinstance(classified, AgentRetryableError)
        # 兜底脱敏：第三方错误文本里若混入密钥形态内容，也不得外泄
        reason = str(redact_secrets(str(classified)))[:500]
        raise bad_request(
            "诊断运行失败（可重试，请稍后再试）" if retryable else "诊断运行失败",
            details={"reason": reason, "retryable": retryable, "note_id": str(note_uuid)},
        ) from exc

    run_id = str(result["run_id"])
    state = result.get("state") or {}
    output = result.get("output")

    if not isinstance(output, dict) or not output.get("diagnostic_id"):
        # 理论上不可达（persist 节点必然组装 output）；防御：给出可识别错误
        raise bad_request(
            "诊断未产出有效结果，请重试",
            details={"run_id": run_id, "note_id": str(note_uuid)},
        )

    # ---- 审计回填（llm_raw 脱敏 + 截断；note_id 按 run_id 补）----
    with SessionLocal() as db:
        _backfill_run(
            db,
            run_id,
            patch_outputs={
                "diagnostic_id": output.get("diagnostic_id"),
                "diagnosis_type": output.get("diagnosis_type"),
                "score": output.get("score"),
                "persisted": True,
            },
            llm_raw=state.get("llm_raw"),
            note_id=str(note_uuid),
        )

    summary = {
        "diagnostic_id": output.get("diagnostic_id"),
        "note_id": str(note_uuid),
        "diagnosis_type": output.get("diagnosis_type"),
        "diagnosis_type_source": output.get("diagnosis_type_source"),
        "score": output.get("score"),
        "parse_attempts": int(result.get("parse_attempts") or 0),
        "data_gaps": list(output.get("data_gaps") or []),
        "warnings": list(output.get("warnings") or []),
        "run_id": run_id,
    }
    logger.info(
        "单篇诊断完成：run=%s note=%s 类型=%s(%s) 评分=%s 缺口=%s parse_attempts=%s",
        run_id,
        note_uuid,
        summary["diagnosis_type"],
        summary["diagnosis_type_source"],
        summary["score"],
        len(summary["data_gaps"]),
        summary["parse_attempts"],
    )
    return summary


__all__ = ["diagnose_note", "LLM_RAW_MAX_LEN"]
