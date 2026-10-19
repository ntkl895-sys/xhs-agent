"""选题服务层：编排「批量热点 → 逐热点跑小图 → 落库 → 审计回填」。

职责与边界：

- **单热点一张图**：每个热点独立跑一次 ``run_agent(kind="topic_generation")``，
  图内部完成「生成 → 校验判重 → 落库」；
- **service 串行批量**：``TOPIC_CONCURRENCY`` 默认 1，逐热点串行，**不引入多线程 / asyncio**
  （与同步栈一致）；
- **单点失败容错**：``run_agent`` 已把失败落 ``agent_runs=failed`` 并抛出，本层捕获后
  记该热点 failed **继续下一热点**，整体不中断；
- **审计回填**（不改 harness）：``run_agent`` 只写 output 的键名，业务明细（计数、topic_ids、
  模型原始输出）由本层用返回的 ``run_id`` 在同一事务里 ``UPDATE agent_runs`` 补齐。

``force=False`` 时会跳过「该人设已有未 discarded 选题覆盖过该热点」的热点，不调用模型、不耗 token。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import select, text as sa_text
from sqlalchemy.orm import Session

from app.agent.errors import AgentNonRetryableError
from app.agent.llm.factory import get_chat_model
from app.agent.runner import run_agent
from app.agent.topics.graph import build_topic_graph
from app.agent.tracing.recorder import redact_secrets
from app.core.config import settings
from app.core.errors import bad_request, not_found
from app.core.timeutil import today_local
from app.crud.topic import crud_topic
from app.db.session import SessionLocal
from app.models.agent_run import AgentRun
from app.models.enums import HotspotStatus, PersonaStatus, RunStatus, RunType
from app.models.hotspot import Hotspot
from app.models.persona import Persona

logger = logging.getLogger("xhs-agent.topics.service")

#: 归档前的模型原始输出长度上限（防止超长落库）
LLM_RAW_MAX_LEN = 20000


# ---------------------------------------------------------------------------
# 读取辅助
# ---------------------------------------------------------------------------
def _clamp_per_hotspot(value: int | None) -> int:
    """把 ``per_hotspot`` 收敛到合法区间（1~MAX_OUTLINE 之类无意义，这里只保证 ≥1）。"""
    try:
        number = int(value) if value is not None else settings.TOPIC_DEFAULT_PER_HOTSPOT
    except (TypeError, ValueError):
        number = settings.TOPIC_DEFAULT_PER_HOTSPOT
    return max(1, number)


def _clamp_limit(value: int | None) -> int:
    """把 ``limit_hotspots`` 收敛到 1~``TOPIC_MAX_HOTSPOTS_PER_REQUEST``。"""
    try:
        number = int(value) if value is not None else settings.TOPIC_DEFAULT_HOTSPOT_LIMIT
    except (TypeError, ValueError):
        number = settings.TOPIC_DEFAULT_HOTSPOT_LIMIT
    return max(1, min(number, settings.TOPIC_MAX_HOTSPOTS_PER_REQUEST))


def _hotspot_payload(hotspot: Hotspot) -> dict[str, Any]:
    """把 ORM 热点转成纯 dict（读属性后再离开会话也安全）。"""
    return {
        "id": str(hotspot.id),
        "source": hotspot.source,
        "title": hotspot.title,
        "url": hotspot.url,
        "mobile_url": hotspot.mobile_url,
        "hot_score": hotspot.hot_score,
        "rank_position": hotspot.rank_position,
        "hot_date": hotspot.hot_date.isoformat() if hotspot.hot_date else None,
    }


def _resolve_hotspot_payloads(
    db: Session,
    hotspot_ids: list[uuid.UUID] | None,
    limit: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """解析要处理的热点。

    - 显式 ``hotspot_ids``：按给定顺序取；**不存在的 id** 记进 ``missing``；
      **重复 id 按首次出现去重**（第 20 步收口 —— 原实现会把同一热点重复
      送进提示词并浪费处理配额；去重先于 MAX 截断，重复项不再挤占名额）；
    - 缺省：``status=NEW``、**当天优先**（不足回退最近），``hot_score DESC`` 前 ``limit`` 个。

    Returns:
        (热点纯 dict 列表, 缺失的 id 字符串列表)
    """
    if hotspot_ids:
        unique_ids: list[uuid.UUID] = []
        seen: set[uuid.UUID] = set()
        for hid in hotspot_ids:
            if hid in seen:
                continue
            seen.add(hid)
            unique_ids.append(hid)

        payloads: list[dict[str, Any]] = []
        missing: list[str] = []
        for hid in unique_ids[: settings.TOPIC_MAX_HOTSPOTS_PER_REQUEST]:
            row = db.get(Hotspot, hid)
            if row is None:
                missing.append(str(hid))
            else:
                payloads.append(_hotspot_payload(row))
        return payloads, missing

    today = today_local()
    picked: list[Hotspot] = []
    stmt_today = (
        select(Hotspot)
        .where(Hotspot.status == HotspotStatus.NEW, Hotspot.hot_date == today)
        .order_by(Hotspot.hot_score.desc().nulls_last())
        .limit(limit)
    )
    picked.extend(db.execute(stmt_today).scalars().all())

    if len(picked) < limit:
        exclude = [row.id for row in picked]
        stmt_recent = select(Hotspot).where(Hotspot.status == HotspotStatus.NEW)
        if exclude:
            stmt_recent = stmt_recent.where(Hotspot.id.notin_(exclude))
        stmt_recent = (
            stmt_recent.order_by(Hotspot.hot_date.desc().nulls_last(), Hotspot.hot_score.desc().nulls_last())
            .limit(limit - len(picked))
        )
        picked.extend(db.execute(stmt_recent).scalars().all())

    return [_hotspot_payload(row) for row in picked], []


# ---------------------------------------------------------------------------
# 审计回填
# ---------------------------------------------------------------------------
def _backfill_run(
    db: Session,
    run_id: str,
    *,
    patch_outputs: dict[str, Any],
    llm_raw: str | None,
    status: str | None = None,
) -> None:
    """用 ``run_id`` 给 ``agent_runs`` 补业务明细（``outputs`` / ``llm_raw`` / 可选 status）。

    ``outputs`` 用 ``COALESCE(outputs,'{}'::jsonb) || patch`` **合并保留** harness 已写内容；
    ``llm_raw`` 写入前过 :func:`redact_secrets` 兜底（**断言不含 sk-**）。
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
    if status is not None:
        db.execute(
            sa_text("UPDATE agent_runs SET status = :st WHERE id = :rid"),
            {"st": status, "rid": run_id},
        )
    db.commit()


def list_recent_runs(db: Session, *, limit: int = 10) -> list[AgentRun]:
    """最近 ``run_type=topic_generation`` 的运行记录（供 ``GET /runs`` 使用）。

    放在 service 层而非路由层：路由只做参数解析与响应组装，不写 SQLAlchemy 表达式
    （第 8 步确立的分层纪律，``selftest_api.py`` 会静态断言）。
    """
    stmt = (
        select(AgentRun)
        .where(AgentRun.run_type == RunType.TOPIC_GENERATION)
        .order_by(AgentRun.created_at.desc())
        .limit(max(1, limit))
    )
    return list(db.execute(stmt).scalars().all())


def _count_rejected(rejected: list[dict[str, Any]]) -> tuple[int, int]:
    """把拒绝项拆成（不合格数, 重复跳过数）。"""
    invalid = sum(1 for item in rejected if item.get("kind") != "duplicate")
    duplicate = sum(1 for item in rejected if item.get("kind") == "duplicate")
    return invalid, duplicate


def _summarize_status(succeeded: int, failed: int) -> str:
    """整批状态：全部成功=success；部分成功=partial；全部失败=failed。"""
    if failed == 0:
        return RunStatus.SUCCESS.value
    if succeeded == 0:
        return RunStatus.FAILED.value
    return RunStatus.PARTIAL.value


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def generate_for_hotspots(
    persona_id: uuid.UUID,
    hotspot_ids: list[uuid.UUID] | None = None,
    per_hotspot: int | None = None,
    limit_hotspots: int | None = None,
    force: bool = False,
    trigger: str = "manual",
    model: Any | None = None,
    tool_model: Any | None = None,
) -> dict[str, Any]:
    """为一个（或多个）热点批量生成选题并落库。

    Args:
        persona_id: 人设 id（必填；不存在 404、已归档 400）。
        hotspot_ids: 指定热点；为空则按「当天优先 + 热度」自动取。
        per_hotspot: 每个热点生成的选题条数。
        limit_hotspots: 自动取热点时的个数上限。
        force: False 时跳过已被该人设覆盖过的热点。
        trigger: ``manual`` / ``scheduled``（本步仅 manual）。
        model: 注入的 LangChain ChatModel；``None`` 时用 :func:`get_chat_model`
            （离线自检由此注入假模型）。
        tool_model: 工具模式用的纯文本模型（不带 ``response_format``）；
            ``None`` 且开关打开时由本层构造。开关关闭时不使用。

    Returns:
        ``{requested, succeeded, failed, skipped, persisted_count, topics, runs}``
    """
    effective_per = _clamp_per_hotspot(per_hotspot)
    effective_limit = _clamp_limit(limit_hotspots)
    chosen_model = model if model is not None else get_chat_model(temperature=0.2, want_json=True)
    # 工具模式：另起一个不带 response_format 的实例（json_object + tools 在当前
    # langchain-openai 版本会要求 strict 工具，探针已实测冲突）。不产生任何请求。
    chosen_tool_model = None
    if settings.TOPIC_TOOLCALLING_ENABLED:
        chosen_tool_model = (
            tool_model if tool_model is not None else get_chat_model(temperature=0.2)
        )

    with SessionLocal() as db:
        persona = db.get(Persona, persona_id)
        if persona is None:
            raise not_found("人设不存在", details={"persona_id": str(persona_id)})
        if persona.status == PersonaStatus.ARCHIVED:
            raise bad_request(
                "人设已归档，不能用于选题",
                details={"persona_id": str(persona_id), "status": persona.status.value},
            )
        account_id = persona.account_id
        covered = crud_topic.covered_hotspot_ids(db, persona_id)
        payloads, missing = _resolve_hotspot_payloads(db, hotspot_ids, effective_limit)

    requested = len(payloads) + len(missing)
    succeeded = 0
    failed = len(missing)
    skipped = 0
    persisted_count = 0
    topics: list[dict[str, Any]] = []
    run_ids: list[str] = []

    if missing:
        logger.warning("指定的热点中有 %d 个不存在，已跳过：%s", len(missing), missing)

    for payload in payloads:
        hotspot_id = payload["id"]
        if not force and hotspot_id in covered:
            skipped += 1
            logger.info("热点 %s 已被人设 %s 覆盖，跳过（force=False）", hotspot_id, persona_id)
            continue

        try:
            result = run_agent(
                kind="topic_generation",
                build_fn=build_topic_graph,
                build_kwargs={"model": chosen_model, "tool_model": chosen_tool_model},
                inputs={
                    "persona_id": str(persona_id),
                    "hotspot_id": hotspot_id,
                    "per_hotspot": effective_per,
                    "force": bool(force),
                },
                account_id=account_id,
                persona_id=persona_id,
                trigger=trigger,
                extra_state={
                    "hotspot_id": hotspot_id,
                    "per_hotspot": effective_per,
                },
            )
        except AgentNonRetryableError as exc:
            # run_agent 已把该次运行落 agent_runs=failed（并已抛错）
            failed += 1
            logger.info("热点 %s 生成失败（不可重试）：%s", hotspot_id, exc)
            continue
        except Exception as exc:  # noqa: BLE001 —— 单点失败不拖垮整批
            failed += 1
            logger.warning("热点 %s 生成异常：%s", hotspot_id, exc)
            continue

        run_id = str(result["run_id"])
        run_ids.append(run_id)
        state = result.get("state") or {}
        output = result.get("output") or {}
        validated = list(state.get("validated") or [])
        rejected = list(state.get("rejected") or [])
        persisted = int(output.get("persisted") or 0)
        topic_ids = list(output.get("topic_ids") or [])
        invalid_count, duplicate_count = _count_rejected(rejected)

        run_status_value: str | None = None
        if persisted and persisted < effective_per:
            run_status_value = RunStatus.PARTIAL.value

        # 工具模式的可观测明细（关闭时完全不写这个键，输出与接入前一致）
        tool_events = list(state.get("tool_events") or [])
        patch_outputs: dict[str, Any] = {
            "topic_ids": topic_ids,
            "generated": len(validated),
            "persisted": persisted,
            "rejected": invalid_count,
            "skipped": duplicate_count,
        }
        if settings.TOPIC_TOOLCALLING_ENABLED:
            patch_outputs["tool_calling"] = {
                "enabled": True,
                "rounds": int(state.get("tool_rounds") or 0),
                "hallucinated": list(state.get("hallucinated_tools") or []),
                "calls": [
                    {
                        "tool": item.get("tool"),
                        "args": item.get("args"),
                        "ok": item.get("ok"),
                        "duration_ms": item.get("duration_ms"),
                        "result_summary": item.get("result_summary"),
                    }
                    for item in tool_events
                ],
            }

        with SessionLocal() as db:
            _backfill_run(
                db,
                run_id,
                patch_outputs=patch_outputs,
                llm_raw=state.get("llm_raw"),
                status=run_status_value,
            )

        if persisted <= 0:
            failed += 1
            continue

        succeeded += 1
        persisted_count += persisted
        for index, topic_id in enumerate(topic_ids):
            item = validated[index] if index < len(validated) else {}
            topics.append(
                {
                    "id": topic_id,
                    "title": item.get("title"),
                    "score": item.get("confidence"),
                    "difficulty": item.get("difficulty"),
                    "pillar": item.get("pillar"),
                }
            )

    summary = {
        "status": _summarize_status(succeeded, failed),
        "requested": requested,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "persisted_count": persisted_count,
        "topics": topics,
        "runs": run_ids,
    }
    logger.info(
        "选题生成完成：请求 %d，成功 %d，失败 %d，跳过 %d，落库 %d",
        requested,
        succeeded,
        failed,
        skipped,
        persisted_count,
    )
    return summary


__all__ = ["generate_for_hotspots", "list_recent_runs"]
