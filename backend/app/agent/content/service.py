"""创作服务层：编排「单选题 → 跑小图 → 落库 → 审计回填」。

职责与边界（与第 10 步选题 service 同范式）：

- **单选题一张图**：每次调用独立跑一次 ``run_agent(kind="content_creation")``，
  图内部完成「载入上下文 → 生成 → 合规自检 → 落库」；
- **不引入并发锁**：本步**不限制**同一 topic 重复生成（唯一的闸门是
  「topic 必须为 approved」；生成成功即把 topic 置 ``writing``），也不加全局并发锁
  ——与第 10 步现状一致；
- **审计回填**（不改 harness）：``run_agent`` 只写 output 的键名，业务明细
  （note_id / compliance_status / 命中数 / 标题）与 ``llm_raw`` 由本层用返回的
  ``run_id`` 在 ``agent_runs`` 上补齐；重试耗尽仍降级落库的运行补记 ``partial``；
- **失败不抛出**：生成失败（如 JSON 连续失败）时返回 ``status="failed"`` 的汇总
  （与 ``POST /topics/generate`` 的批量语义一致），由调用方按 ``status`` 判断。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import select, text as sa_text
from sqlalchemy.orm import Session

from app.agent.content.graph import build_content_graph
from app.agent.errors import AgentNonRetryableError
from app.agent.llm.factory import get_chat_model
from app.agent.runner import run_agent
from app.agent.tracing.recorder import redact_secrets
from app.core.errors import bad_request, not_found
from app.db.session import SessionLocal
from app.models.agent_run import AgentRun
from app.models.enums import PersonaStatus, RunStatus, RunType, TopicStatus
from app.models.persona import Persona
from app.models.topic import Topic

logger = logging.getLogger("xhs-agent.content.service")

#: 归档前的模型原始输出长度上限（防止超长落库）
LLM_RAW_MAX_LEN = 20000


# ---------------------------------------------------------------------------
# 审计回填（与第 10 步同实现：harness 不改，业务明细在 service 补齐）
# ---------------------------------------------------------------------------
def _backfill_run(
    db: Session,
    run_id: str,
    *,
    patch_outputs: dict[str, Any],
    llm_raw: str | None,
    status: str | None = None,
    note_id: Any = None,
    topic_id: Any = None,
) -> None:
    """用 ``run_id`` 给 ``agent_runs`` 补业务明细（``outputs`` / ``llm_raw`` / 关联 id / 可选 status）。

    ``outputs`` 用 ``COALESCE(outputs,'{}'::jsonb) || patch`` **合并保留** harness 已写内容；
    ``llm_raw`` 写入前过 :func:`redact_secrets` 兜底（**断言不含 sk-**）。

    ``note_id`` / ``topic_id`` 也在这里补：harness 的 ``AgentRunRecorder`` **支持**这两个参数，
    但 ``run_agent`` 只转发 ``account_id`` / ``persona_id``（基座文件本步禁止改动），
    因此本层用同一套「返回后按 run_id 回填」机制把关联 id 补上，
    让审计行能直接回答「这次运行产出了哪篇笔记 / 来自哪个选题」。
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
            "note_id = COALESCE(:note_id, note_id), "
            "topic_id = COALESCE(:topic_id, topic_id) "
            "WHERE id = :rid"
        ),
        {
            "patch": json.dumps(patch_outputs, ensure_ascii=False, default=str),
            "raw": raw_value,
            "note_id": str(note_id) if note_id else None,
            "topic_id": str(topic_id) if topic_id else None,
            "rid": run_id,
        },
    )
    if status is not None:
        db.execute(
            sa_text("UPDATE agent_runs SET status = :st WHERE id = :rid"),
            {"st": status, "rid": run_id},
        )
    db.commit()


def list_recent_runs(db: Session, *, limit: int = 10) -> list[AgentRun]:
    """最近 ``run_type=content_creation`` 的运行记录（供 ``GET /notes/runs`` 使用）。

    放在 service 层而非路由层：路由只做参数解析与响应组装，不写 SQLAlchemy 表达式
    （第 8 步确立的分层纪律，``selftest_api.py`` 会静态断言）。
    """
    stmt = (
        select(AgentRun)
        .where(AgentRun.run_type == RunType.CONTENT_CREATION)
        .order_by(AgentRun.created_at.desc())
        .limit(max(1, limit))
    )
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def _failure_summary(message: str) -> dict[str, Any]:
    """统一的失败汇总（不抛出；调用方按 ``status`` 判断）。"""
    return {
        "status": RunStatus.FAILED.value,
        "run_id": None,
        "note_id": None,
        "title": None,
        "compliance_status": None,
        "forbidden_hits": None,
        "tags_count": None,
        "degraded": False,
        "field_errors": [],
        "error_message": message,
    }


def create_note_for_topic(
    topic_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    trigger: str = "manual",
    model: Any | None = None,
) -> dict[str, Any]:
    """为一个**已通过评审（approved）**的选题生成一篇笔记草稿并落库。

    前置校验（不满足时抛对应业务错误，**不发起任何模型调用**）：

    - 选题不存在 → 404；选题状态不是 ``approved`` → 400；
    - 选题未关联人设 / 人设已归档 → 400；归属账号无法确定 → 400。

    另有 LLM 配置闸门：未配置（``AGENT_LLM_ENABLED`` / ``LLM_API_KEY`` / ``LLM_MODEL``）
    时抛 :class:`AgentConfigError`（路由转 400），也不发起任何对外请求。

    Args:
        topic_id: 选题 id（必填）。
        account_id: 账号 id（可选兜底；**归属账号以选题关联为准**，仅当选题与人设均未关联账号时使用）。
        trigger: ``manual`` / ``scheduled``（本步仅 manual）。
        model: 注入的 LangChain ChatModel；``None`` 时用 :func:`get_chat_model`
            （离线自检由此注入假模型）。

    Returns:
        ``{status, run_id, note_id, title, compliance_status, forbidden_hits,
        tags_count, degraded, field_errors, error_message}``
        —— ``status`` 为 ``success`` / ``partial``（降级落库）/ ``failed``。
    """
    # ---- 前置校验（顺序刻意在前：先给出准确的业务错误，再谈模型）----
    with SessionLocal() as db:
        topic = db.get(Topic, topic_id)
        if topic is None:
            raise not_found("选题不存在", details={"topic_id": str(topic_id)})
        if topic.status != TopicStatus.APPROVED:
            raise bad_request(
                "选题状态不是 approved，不能创作",
                details={"topic_id": str(topic_id), "status": topic.status.value},
            )
        persona = db.get(Persona, topic.persona_id) if topic.persona_id else None
        if persona is None:
            raise bad_request(
                "选题未关联人设，无法创作", details={"topic_id": str(topic_id)}
            )
        if persona.status == PersonaStatus.ARCHIVED:
            raise bad_request(
                "人设已归档，不能用于创作",
                details={"persona_id": str(persona.id), "status": persona.status.value},
            )
        resolved_account = topic.account_id or persona.account_id or account_id
        if resolved_account is None:
            raise bad_request(
                "无法确定归属账号（选题与人设均未关联账号）",
                details={"topic_id": str(topic_id), "persona_id": str(persona.id)},
            )
        resolved_persona_id = persona.id

    # ---- 模型（未配置时在这里抛 AgentConfigError；离线自检经 model 注入口绕过）----
    chosen_model = model if model is not None else get_chat_model(temperature=0.4, want_json=True)

    try:
        result = run_agent(
            kind="content_creation",
            build_fn=build_content_graph,
            build_kwargs={"model": chosen_model},
            inputs={
                "topic_id": str(topic_id),
                "account_id": str(resolved_account),
                "trigger": trigger,
            },
            account_id=resolved_account,
            persona_id=resolved_persona_id,
            trigger=trigger,
            extra_state={"topic_id": str(topic_id)},
        )
    except AgentNonRetryableError as exc:
        # run_agent 已把该次运行落 agent_runs=failed（并已抛出）
        logger.info("创作失败（不可重试）：topic=%s %s", topic_id, exc)
        return _failure_summary(str(exc))
    except Exception as exc:  # noqa: BLE001 —— 统一转成失败汇总（不吞审计）
        logger.warning("创作异常：topic=%s %s", topic_id, exc)
        return _failure_summary(f"{type(exc).__name__}: {exc}")

    run_id = str(result["run_id"])
    state = result.get("state") or {}
    output = result.get("output") or {}
    degraded = bool(output.get("degraded"))

    with SessionLocal() as db:
        _backfill_run(
            db,
            run_id,
            patch_outputs={
                "note_id": output.get("note_id"),
                "generated": 1,
                "compliance_status": output.get("compliance_status"),
                "forbidden_hits": output.get("forbidden_hits"),
                "tags_count": output.get("tags_count"),
                "title": output.get("title"),
                # 降级落库标记：`GET /notes/runs` 的 `degraded` 字段读的就是它
                "degraded": degraded,
            },
            llm_raw=state.get("llm_raw"),
            # 重试耗尽仍落库（有字段硬伤）→ 补记 partial；正常 → 保持 harness 写的 success
            status=RunStatus.PARTIAL.value if degraded else None,
            note_id=output.get("note_id"),
            topic_id=topic_id,
        )

    summary = {
        "status": RunStatus.PARTIAL.value if degraded else RunStatus.SUCCESS.value,
        "run_id": run_id,
        "note_id": output.get("note_id"),
        "title": output.get("title"),
        "compliance_status": output.get("compliance_status"),
        "forbidden_hits": output.get("forbidden_hits"),
        "tags_count": output.get("tags_count"),
        "degraded": degraded,
        "field_errors": list(output.get("field_errors") or []),
        "error_message": None,
    }
    logger.info(
        "创作完成：topic=%s note=%s status=%s compliance=%s 命中=%s",
        topic_id,
        summary["note_id"],
        summary["status"],
        summary["compliance_status"],
        summary["forbidden_hits"],
    )
    return summary


__all__ = ["create_note_for_topic", "list_recent_runs"]
