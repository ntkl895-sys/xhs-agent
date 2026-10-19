"""多版本创作服务层：编排「单选题 → 跑小图 → 落库 → 审计回填」+ 批次查询 + 互斥采用。

职责与边界（与第 10 / 12 步 service 同范式）：

- **单选题一张图**：每次调用独立跑一次 ``run_agent(kind="content_variants")``，
  图内部完成「载入上下文 → 生成 N 版 → 逐版判定 → 单事务落库」；
- **不引入并发锁**：闸门与第 12 步一致（topic 必须为 ``approved``；生成成功即把
  topic 置 ``writing``）——沿用既有限制，不扩权；
- **审计回填**（不改 harness）：``run_agent`` 只写通用键，业务明细
  （variant_group_id / generated / persisted / blocked / rejected / note_ids 等）与
  ``llm_raw`` 由本层用返回的 ``run_id`` 回填到 ``agent_runs``
  （复用第 12 步创作的同一套回填实现）；run 状态按「全成功 / 部分合格 / 全失败」
  正确落库（partial / failed 由本层覆盖补记）；
- **失败不抛出**：生成失败（如 JSON 连续失败、全部版本不合格）时返回
  ``status="failed"`` 的汇总（与 ``POST /topics/generate`` 的批量语义一致），
  由调用方按 ``status`` 判断。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.agent.content.service import _backfill_run  # 复用第 12 步的审计回填（同一套 run_id 裸 SQL）
from app.agent.content_variants.contracts import DEFAULT_VARIANTS, OUTCOME_QUALIFIED
from app.agent.content_variants.graph import build_content_variants_graph
from app.agent.errors import AgentNonRetryableError
from app.agent.llm.factory import get_chat_model
from app.agent.runner import run_agent
from app.core.errors import bad_request, not_found
from app.crud.note import crud_note
from app.db.session import SessionLocal
from app.models.enums import PersonaStatus, RunStatus, TopicStatus
from app.models.persona import Persona
from app.models.topic import Topic

logger = logging.getLogger("xhs-agent.content_variants.service")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _variant_meta_of(note: Any) -> dict[str, Any]:
    """从笔记的 ``llm_meta`` 里安全取出 ``variant`` 子块（无则空 dict）。"""
    meta = note.llm_meta if isinstance(note.llm_meta, dict) else {}
    variant = meta.get("variant") if isinstance(meta.get("variant"), dict) else None
    return variant or {}


def _variant_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    """批次内排序键：``variant.index`` 优先，其次 note_id（确定性）。"""
    index = item.get("index")
    return (
        int(index) if isinstance(index, int) else 0,
        str(item.get("note_id") or ""),
    )


def _variant_note_payload(note: Any, variant: dict[str, Any]) -> dict[str, Any]:
    """一条版本笔记的出参（不含 llm_raw / 向量；供批次与采用响应共用）。"""
    compliance = note.compliance_status
    return {
        "note_id": note.id,
        "angle": str(variant.get("angle") or ""),
        "index": variant.get("index"),
        "title": note.title,
        "body": note.body or "",
        "tags": list(note.tags or []),
        "compliance_status": compliance.value if hasattr(compliance, "value") else str(compliance),
        "adopted": bool(variant.get("adopted")),
        "created_at": note.created_at,
    }


# ---------------------------------------------------------------------------
# 主入口：生成多版本
# ---------------------------------------------------------------------------
def _failure_summary(message: str, *, requested: int, topic_id: str) -> dict[str, Any]:
    """统一的失败汇总（不抛出；调用方按 ``status`` 判断）。"""
    return {
        "status": RunStatus.FAILED.value,
        "run_id": None,
        "topic_id": topic_id,
        "variant_group_id": None,
        "requested": requested,
        "generated": None,
        "persisted": None,
        "blocked": None,
        "rejected": None,
        "items": [],
        "note_ids": [],
        "error_message": message,
    }


def generate_variants(
    topic_id: uuid.UUID,
    *,
    n: int = DEFAULT_VARIANTS,
    angles: list[str] | None = None,
    account_id: uuid.UUID | None = None,
    trigger: str = "manual",
    model: Any | None = None,
) -> dict[str, Any]:
    """为一个**已通过评审（approved）**的选题生成 N 个可对比版本并落库。

    前置校验（不满足时抛对应业务错误，**不发起任何模型调用**）：

    - 选题不存在 → 404；选题状态不是 ``approved`` → 400（与第 12 步同一闸门）；
    - 选题未关联人设 / 人设已归档 → 400；归属账号无法确定 → 400；
    - ``angles`` 提供时数量必须与 ``n`` 一致（防御性复核；路由层 schema 已前置校验）。

    另有 LLM 配置闸门：未配置（``AGENT_LLM_ENABLED`` / ``LLM_API_KEY`` / ``LLM_MODEL``）
    时抛 :class:`AgentConfigError`（路由转 400），也不发起任何对外请求。

    Args:
        topic_id: 选题 id（必填）。
        n: 版本数（1~5）。
        angles: 显式角度列表（可选；None = 模型从内置角度池自选）。
        account_id: 账号 id（可选兜底；**归属账号以选题关联为准**，仅当选题与人设均未关联账号时使用）。
        trigger: ``manual`` / ``scheduled``（本步仅 manual）。
        model: 注入的 LangChain ChatModel；``None`` 时用 :func:`get_chat_model`
            （离线自检由此注入假模型）。

    Returns:
        ``{status, run_id, topic_id, variant_group_id, requested, generated,
        persisted, blocked, rejected, items, note_ids, error_message}``
        —— ``status`` 为 ``success``（全部合格）/ ``partial``（部分合格）/
        ``failed``（全失败，且零落库）。
    """
    # ---- 角度防御性复核（路由层 schema 已校验；service 直调时兜底）----
    if angles:
        angles = [str(item).strip() for item in angles]
        if len(angles) != n:
            raise bad_request(
                "angles 的数量需与 n 一致（或留空自动选取）",
                details={"n": n, "angles_count": len(angles)},
            )

    # ---- 前置校验（顺序刻意在前：先给出准确的业务错误，再谈模型）----
    with SessionLocal() as db:
        topic = db.get(Topic, topic_id)
        if topic is None:
            raise not_found("选题不存在", details={"topic_id": str(topic_id)})
        if topic.status != TopicStatus.APPROVED:
            raise bad_request(
                "选题状态不是 approved，不能生成多版本",
                details={"topic_id": str(topic_id), "status": topic.status.value},
            )
        persona = db.get(Persona, topic.persona_id) if topic.persona_id else None
        if persona is None:
            raise bad_request(
                "选题未关联人设，无法生成多版本", details={"topic_id": str(topic_id)}
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
    # 采样温度刻意略高于第 12 步（0.4 → 0.7）：多版本任务的要点是「角度明显不同」，
    # 需要更强的多样性；由提示词与逐版判定共同兜底质量。
    chosen_model = model if model is not None else get_chat_model(temperature=0.7, want_json=True)

    try:
        result = run_agent(
            kind="content_variants",
            build_fn=build_content_variants_graph,
            build_kwargs={"model": chosen_model},
            inputs={
                "topic_id": str(topic_id),
                "n": n,
                "angles": angles,
                "trigger": trigger,
            },
            account_id=resolved_account,
            persona_id=resolved_persona_id,
            trigger=trigger,
            extra_state={
                "topic_id": str(topic_id),
                "variant_n": n,
                "variant_angles": angles,
            },
        )
    except AgentNonRetryableError as exc:
        # run_agent 已把该次运行落 agent_runs=failed（并已抛出）
        logger.info("多版本生成失败（不可重试）：topic=%s %s", topic_id, exc)
        return _failure_summary(str(exc), requested=n, topic_id=str(topic_id))
    except Exception as exc:  # noqa: BLE001 —— 统一转成失败汇总（不吞审计）
        logger.warning("多版本生成异常：topic=%s %s", topic_id, exc)
        return _failure_summary(f"{type(exc).__name__}: {exc}", requested=n, topic_id=str(topic_id))

    run_id = str(result["run_id"])
    state = result.get("state") or {}
    output = result.get("output") or {}
    results = list(state.get("variant_results") or [])
    requested = int(output.get("requested") or n)
    generated = int(output.get("generated") or 0)
    persisted = int(output.get("persisted") or 0)
    blocked = int(output.get("blocked") or 0)
    rejected = int(output.get("rejected") or 0)

    # ---- run 状态：全成功 / 部分合格 / 全失败（正确落库）----
    if persisted > 0 and persisted >= requested:
        summary_status = RunStatus.SUCCESS.value
        run_status: str | None = None  # harness 已写 success，无需覆盖
    elif persisted > 0:
        summary_status = RunStatus.PARTIAL.value
        run_status = RunStatus.PARTIAL.value
    else:
        summary_status = RunStatus.FAILED.value
        run_status = RunStatus.FAILED.value

    note_ids = [str(item) for item in (output.get("note_ids") or [])]
    with SessionLocal() as db:
        _backfill_run(
            db,
            run_id,
            patch_outputs={
                "variant_group_id": output.get("variant_group_id"),
                "requested": requested,
                "generated": generated,
                "persisted": persisted,
                "blocked": blocked,
                "rejected": rejected,
                "extra_ignored": output.get("extra_ignored"),
                "note_ids": note_ids,
                "angles": output.get("angles"),
                "blocked_details": output.get("blocked_details"),
                "rejected_details": output.get("rejected_details"),
            },
            llm_raw=state.get("llm_raw"),
            status=run_status,
            # 关联首条落库笔记（全量见 outputs.note_ids）；零落库时为 None
            note_id=note_ids[0] if note_ids else None,
            topic_id=topic_id,
        )

    # ---- 逐版明细（含 blocked / rejected；qualified 版本回填 note_id）----
    items: list[dict[str, Any]] = []
    cursor = 0
    for item in results:
        outcome = item.get("outcome")
        item_note_id: str | None = None
        if outcome == OUTCOME_QUALIFIED and cursor < len(note_ids):
            item_note_id = note_ids[cursor]
            cursor += 1
        items.append(
            {
                "angle": str(item.get("angle") or ""),
                "title": str(item.get("title") or ""),
                "body": str(item.get("body") or ""),
                "tags": [str(tag) for tag in (item.get("tags") or [])],
                "status": outcome,
                "reason": item.get("reason"),
                "note_id": item_note_id,
                "hits": item.get("hits"),
            }
        )

    summary = {
        "status": summary_status,
        "run_id": run_id,
        "topic_id": str(topic_id),
        "variant_group_id": output.get("variant_group_id"),
        "requested": requested,
        "generated": generated,
        "persisted": persisted,
        "blocked": blocked,
        "rejected": rejected,
        "items": items,
        "note_ids": note_ids,
        "error_message": None,
    }
    logger.info(
        "多版本生成完成：topic=%s run=%s status=%s 请求=%s 落库=%s 拦截=%s 不合格=%s",
        topic_id,
        run_id,
        summary_status,
        requested,
        persisted,
        blocked,
        rejected,
    )
    return summary


# ---------------------------------------------------------------------------
# 批次查询 / 采用
# ---------------------------------------------------------------------------
def list_topic_batches(topic_id: uuid.UUID) -> dict[str, Any]:
    """某选题的多版本批次列表（批次倒序；批次内按 ``variant.index`` 升序）。

    - 只含**已落库**的版本（blocked / rejected 版本不落库，故不在列表内）；
    - 出参不含 ``llm_raw`` 与向量；选题不存在 → 404。
    """
    with SessionLocal() as db:
        topic = db.get(Topic, topic_id)
        if topic is None:
            raise not_found("选题不存在", details={"topic_id": str(topic_id)})
        rows = crud_note.list_variant_notes(db, topic_id)

        batches: dict[str, list[dict[str, Any]]] = {}
        created_map: dict[str, Any] = {}
        for note in rows:
            variant = _variant_meta_of(note)
            group_id = str(variant.get("group_id") or "")
            if not group_id:
                continue
            batches.setdefault(group_id, []).append(_variant_note_payload(note, variant))
            stamp = note.created_at
            if group_id not in created_map or stamp < created_map[group_id]:
                created_map[group_id] = stamp

        result: list[dict[str, Any]] = []
        for group_id, variants in batches.items():
            variants.sort(key=_variant_sort_key)
            result.append(
                {
                    "group_id": group_id,
                    "created_at": created_map[group_id],
                    "variants": variants,
                }
            )
        result.sort(key=lambda batch: batch["created_at"], reverse=True)

    return {"topic_id": str(topic_id), "batches": result}


def adopt_variant(note_id: uuid.UUID) -> dict[str, Any]:
    """采用某版本（**单事务互斥**：该版置采用，同批次其余置未采用）。

    - 笔记不存在 → 404；不是多版本批次成员 → 400；
    - 可反复切换采用目标；**不**改笔记状态（仍 draft）与选题状态；
    - 返回采用后的**整批最新状态**（前端直接刷新对比视图）。
    """
    with SessionLocal() as db:
        try:
            outcome = crud_note.adopt_variant(db, note_id)
        except ValueError as exc:
            raise bad_request(str(exc), details={"note_id": str(note_id)}) from exc
        if outcome is None:
            raise not_found("笔记不存在", details={"note_id": str(note_id)})

        target, rows = outcome
        target_variant = _variant_meta_of(target)
        group_id = str(target_variant.get("group_id") or "")
        variants = [_variant_note_payload(row, _variant_meta_of(row)) for row in rows]
        variants.sort(key=_variant_sort_key)

    return {
        "group_id": group_id,
        "adopted_note_id": str(target.id),
        "variants": variants,
    }


__all__ = ["adopt_variant", "generate_variants", "list_topic_batches"]
