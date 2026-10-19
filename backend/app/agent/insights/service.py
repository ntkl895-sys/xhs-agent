"""规律提炼服务层（第 18 步）：编排「跑第六个业务图 → 审计回填」。

职责与边界（与第 12 / 16 / 17 步 service 同范式）：

- **前置闸门（先于一切）**：embedding 未就绪（本地依赖未装 / openai 未配置）
  → 400（**零请求零 run**，不浪费 LLM token）；LLM 未配置 → 400（同样零请求零 run）；
- **样本不足不是错误**：图内 ``insufficient_sample`` 收尾（零模型调用、零写入），
  本层把 run 补记 ``partial``，响应透传 ``data_gaps``（含「至少发布 N 篇并回填
  数据后再提炼」）；
- **部分合格**（模型有产出但有条目被剔除）→ run 补记 ``partial``；
- **审计回填**（不改 harness）：``run_agent`` 只写 output 的键名，业务明细
  （persisted / generated / reused / skipped / snippet_ids / sample_size /
  prompt_version 等）与 ``llm_raw``（redact + 截断 20000）由本层用 ``run_id``
  补齐；
- **异常必须翻译**：EmbeddingError（依赖 / 模型加载失败）→ 400 带安装指引；
  InsightsGiveUpError → 400；其它经 classify_exception + redact_secrets → 400；
  **响应不含 Traceback、不含 llm_raw、不含 key**；
- **模型与 embedder 注入**：``model=None`` 时用 ``get_chat_model(temperature=0.2,
  want_json=True)``；``embedder=None`` 时图内取 embedding 服务单例。离线自检经
  参数注入假实现，全程零真实请求。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.agent.embedding.base import EmbeddingError, Embedder
from app.agent.embedding.local import INSTALL_HINT, local_deps_installed
from app.agent.errors import AgentConfigError, AgentRetryableError, classify_exception
from app.agent.insights.contracts import InsightsGiveUpError
from app.agent.insights.graph import build_insights_graph
from app.agent.llm.factory import get_chat_model
from app.agent.runner import run_agent
from app.agent.tracing.recorder import redact_secrets
from app.core.config import settings
from app.core.errors import bad_request
from app.db.session import SessionLocal
from app.models.enums import RunStatus, RunType

logger = logging.getLogger("xhs-agent.insights.service")

#: 归档前的模型原始输出长度上限（防止超长落库；与第 12 / 15 / 16 / 17 步同口径）
LLM_RAW_MAX_LEN = 20000


# ---------------------------------------------------------------------------
# 前置闸门（不加载模型、不发任何请求）
# ---------------------------------------------------------------------------
def _ensure_embedding_ready() -> None:
    """跑图前的 embedding 就绪检查（**绝不加载模型 / 不下载权重**）。

    - 本地 provider：只探测依赖是否可 import（未装 → 400 + 安装指引）；
    - openai provider：只检查配置是否齐全；
    - 未知 provider → 400。

    这一步保证「没装依赖就提炼」在**消耗任何 token 之前**失败。
    """
    provider = (settings.EMBEDDING_PROVIDER or "local").strip().lower()
    if provider == "local":
        if not local_deps_installed():
            raise bad_request(
                "规律提炼未就绪：本地向量化依赖未安装。",
                details={"provider": "local", "hint": INSTALL_HINT},
            )
        return
    if provider == "openai":
        missing: list[str] = []
        if not settings.EMBEDDING_OPENAI_MODEL.strip():
            missing.append("EMBEDDING_OPENAI_MODEL")
        if not settings.EMBEDDING_API_KEY.strip():
            missing.append("EMBEDDING_API_KEY")
        if missing:
            raise bad_request(
                "规律提炼未就绪：openai 兼容 embedding 未配置。",
                details={"provider": "openai", "missing": missing},
            )
        return
    raise bad_request(
        f"未知的 EMBEDDING_PROVIDER：{settings.EMBEDDING_PROVIDER!r}"
        "（仅支持 local / openai），请检查 backend/.env。",
        details={"provider": settings.EMBEDDING_PROVIDER},
    )


# ---------------------------------------------------------------------------
# 审计回填（与第 10 / 12 / 15 / 16 / 17 步同实现：harness 不改，业务明细在 service 补齐）
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
    if status is not None:
        db.execute(
            sa_text("UPDATE agent_runs SET status = :st WHERE id = :rid"),
            {"st": status, "rid": run_id},
        )
    db.commit()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def generate_insights(
    *,
    trigger: str = "manual",
    model: Any | None = None,
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    """跑一次规律提炼（第六个业务图）并把业务明细回填 ``agent_runs``。

    Args:
        trigger: ``manual`` / ``scheduled``。
        model: 注入的 ChatModel；``None`` 时用 :func:`get_chat_model`
            （离线自检由此注入假模型）。
        embedder: 注入的 embedder（离线自检由此注入假实现）；``None`` 时图内取
            embedding 服务单例（懒加载）。

    Returns:
        ``{run_id, status, insufficient_sample, sample_size, min_required,
        generated, persisted, reused, skipped, snippet_ids, rejected, data_gaps,
        warnings, parse_attempts, prompt_version}`` —— 绝不含 ``llm_raw`` / key。

    Raises:
        AppError(400): embedding 未就绪 / LLM 未配置（均零请求零 run）；
            结构重试超限（``InsightsGiveUpError``）；向量化失败（``EmbeddingError``）；
            其它运行失败（经分类与脱敏）。
    """
    # ---- 前置 1：embedding 就绪（未装依赖 / 未配置 → 400 零 run 零 token）----
    if embedder is None:
        _ensure_embedding_ready()

    # ---- 前置 2：LLM 闸门（未配置 → 400 零请求零 run）----
    try:
        chosen_model = (
            model
            if model is not None
            else get_chat_model(temperature=0.2, want_json=True)
        )
    except AgentConfigError as exc:
        raise bad_request("规律提炼未就绪，无法运行", details={"reason": str(exc)}) from exc

    # ---- 跑图（第六个业务图；异常统一翻译）----
    try:
        result = run_agent(
            kind="insights",
            build_fn=build_insights_graph,
            build_kwargs={"model": chosen_model, "embedder": embedder},
            inputs={},
            run_type=RunType.INSIGHT,
            trigger=trigger,
        )
    except InsightsGiveUpError as exc:
        raise bad_request(str(exc), details={"reason": exc.reason}) from exc
    except AgentConfigError as exc:
        raise bad_request("规律提炼未就绪，无法运行", details={"reason": str(exc)}) from exc
    except EmbeddingError as exc:
        raise bad_request(
            "规律向量化失败，未写入任何知识块。",
            details={
                "reason": str(exc),
                "hint": "请确认已安装向量化依赖并完成模型下载"
                "（可先运行 scripts/check_embedding_local.py 预检）。",
            },
        ) from exc
    except Exception as exc:  # noqa: BLE001 —— 分类后统一译成统一错误体
        classified = classify_exception(exc)
        retryable = isinstance(classified, AgentRetryableError)
        reason = str(redact_secrets(str(classified)))[:500]
        raise bad_request(
            "规律提炼失败（可重试，请稍后再试）" if retryable else "规律提炼失败",
            details={"reason": reason, "retryable": retryable},
        ) from exc

    run_id = str(result["run_id"])
    state = result.get("state") or {}
    output = result.get("output")

    if not isinstance(output, dict):
        # 理论上不可达（insufficient / persist 节点必然组装 output）；防御
        raise bad_request("规律提炼未产出有效结果，请重试", details={"run_id": run_id})

    insufficient = bool(output.get("insufficient_sample"))
    rejected = int(output.get("rejected") or 0)
    persisted = int(output.get("persisted") or 0)

    # ---- status 补正：样本不足（零写入）/ 部分合格（有条目被剔除）→ partial ----
    patch_status: str | None = (
        RunStatus.PARTIAL.value if (insufficient or rejected > 0) else None
    )

    # ---- 审计回填（业务明细 + llm_raw 脱敏截断）----
    patch_outputs: dict[str, Any] = {
        "persisted": persisted,
        "generated": int(output.get("generated") or 0),
        "reused": int(output.get("reused") or 0),
        "skipped": int(output.get("skipped") or 0),
        "snippet_ids": list(output.get("snippet_ids") or []),
        "sample_size": output.get("sample_size"),
        "min_required": output.get("min_required"),
        "prompt_version": output.get("prompt_version"),
        "insufficient_sample": insufficient,
        "rejected": rejected,
        "data_gaps": list(output.get("data_gaps") or []),
    }
    with SessionLocal() as db:
        _backfill_run(
            db,
            run_id,
            patch_outputs=patch_outputs,
            llm_raw=state.get("llm_raw"),
            status=patch_status,
        )

    summary: dict[str, Any] = {
        "run_id": run_id,
        "status": patch_status or RunStatus.SUCCESS.value,
        "insufficient_sample": insufficient,
        "sample_size": output.get("sample_size"),
        "min_required": output.get("min_required"),
        "generated": patch_outputs["generated"],
        "persisted": persisted,
        "reused": patch_outputs["reused"],
        "skipped": patch_outputs["skipped"],
        "snippet_ids": patch_outputs["snippet_ids"],
        "rejected": rejected,
        "data_gaps": patch_outputs["data_gaps"],
        "warnings": list(output.get("warnings") or []),
        "parse_attempts": int(result.get("parse_attempts") or 0),
        "prompt_version": output.get("prompt_version"),
    }
    logger.info(
        "规律提炼完成：run=%s status=%s sample=%s/%s generated=%s persisted=%s "
        "reused=%s skipped=%s",
        run_id,
        summary["status"],
        summary["sample_size"],
        summary["min_required"],
        summary["generated"],
        summary["persisted"],
        summary["reused"],
        summary["skipped"],
    )
    return summary


__all__ = ["LLM_RAW_MAX_LEN", "generate_insights"]
