"""数据回填服务层：编排「单张截图 → 视觉识别 → 审计回填」（第 15 步）。

职责与边界（与第 12 步创作 service 同范式，但**失败语义相反**）：

- **本步不做落库**：识别结果只作为候选返回给前端，必须经人工核对后才由
  ``POST /api/v1/metrics`` 落库（人工确认是硬规则，识别链路零写入）；
- **异常必须抛出**（不同于创作 service 的「失败返回汇总」）：识别面板需要
  拿到明确状态码给出可操作提示——422（文件不合法）/ 404（笔记不存在）/
  400（模型不支持图片、JSON 重试超限），翻译后抛统一错误体（AppError），
  由 ``app.main`` 的异常处理器渲染；
- **审计回填**（不改 harness）：``run_agent`` 成功时只写 output 的键名，
  识别摘要与 ``llm_raw`` 由本层用返回的 ``run_id`` 在 ``agent_runs`` 上补齐；
  ``llm_raw`` 写入前过 :func:`redact_secrets` 并截断，**绝不写图片数据
  （无 base64 / 无绝对路径）**；失败运行由 runner 自行落 ``failed``（含错误信息）；
- **模型注入**：``model=None`` 时用 ``get_chat_model(want_vision=True)``
  （视觉未配置 → ``AgentConfigError``，由**路由层**转 400——与第 10 / 12 步的
  路由处理保持同一结构）；离线自检经 ``model`` 参数注入假模型，全程零真实请求。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.agent.llm.factory import VISION_DOC_URL, get_chat_model
from app.agent.metrics.contracts import (
    ImageValidationError,
    NoteMissingError,
    RecognitionGiveUpError,
    VisionUnsupportedError,
)
from app.agent.metrics.graph import build_metric_graph
from app.agent.runner import run_agent
from app.agent.tracing.recorder import redact_secrets
from app.core.errors import bad_request, not_found, validation_error
from app.db.session import SessionLocal
from app.models.enums import RunTrigger, RunType

logger = logging.getLogger("xhs-agent.metrics.service")

#: 归档前的模型原始输出长度上限（防止超长落库；与第 12 步同口径）
LLM_RAW_MAX_LEN = 20000


# ---------------------------------------------------------------------------
# 审计回填（与第 10 / 12 步同实现：harness 不改，业务明细在 service 补齐）
# ---------------------------------------------------------------------------
def _backfill_run(
    db: Session,
    run_id: str,
    *,
    patch_outputs: dict[str, Any],
    llm_raw: str | None,
    note_id: Any = None,
) -> None:
    """用 ``run_id`` 给 ``agent_runs`` 补识别摘要（``outputs`` / ``llm_raw`` / ``note_id``）。

    ``outputs`` 用 ``COALESCE(outputs,'{}'::jsonb) || patch`` **合并保留** harness
    已写内容；``llm_raw`` 写入前过 :func:`redact_secrets` 兜底（**断言不含密钥前缀**）
    并截断到 :data:`LLM_RAW_MAX_LEN`。

    ``note_id`` 也在这里补：harness 的 ``AgentRunRecorder`` **支持**该参数，
    但 ``run_agent`` 只转发 ``account_id`` / ``persona_id``（基座文件本步禁止改动），
    因此沿用同一套「返回后按 run_id 回填」机制。
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
def recognize_metric_screenshot(
    image_bytes: bytes,
    image_mime: str,
    *,
    note_id: uuid.UUID | str | None = None,
    image_name: str | None = None,
    trigger: str = RunTrigger.MANUAL.value,
    model: Any | None = None,
) -> dict[str, Any]:
    """对单张截图做视觉识别，返回**六个指标的候选值**（不写库）。

    Args:
        image_bytes / image_mime / image_name: 上传文件的字节、声明的 MIME
            与文件名；类型 / 大小 / 文件头魔数校验在图的 ``load_image`` 节点完成
            （不通过 → 422）。
        note_id: 可选。传了会校验笔记存在（不存在 → 404）并载入标题供人工核对。
        trigger: ``manual`` / ``scheduled``（本步仅 manual）。
        model: 注入的**视觉** ChatModel；``None`` 时用
            :func:`~app.agent.llm.factory.get_chat_model`（``want_vision=True``；
            离线自检由此注入假模型）。

    Returns:
        ``{data_source, metrics:{views,likes,collects,comments,shares,new_followers},
        evidence, captured_at, missing_fields, uncertain_fields, parse_attempts,
        warnings, note_id, note_title, run_id}``

        —— 绝不含 base64 / 绝对路径 / ``llm_raw`` / key（自检会静态断言）。

    Raises:
        AgentConfigError: 视觉未配置（开关 / key / 视觉模型名；路由转 400，不发请求）。
        AppError(422): 图片类型 / 大小 / 内容不合法。
        AppError(404): ``note_id`` 指向的笔记不存在。
        AppError(400): 模型不支持图片输入；或 JSON 连续解析失败重试超限。
    """
    # ---- 模型（视觉；未配置时在这里抛 AgentConfigError，由路由转 400，零请求）----
    chosen_model = (
        model
        if model is not None
        else get_chat_model(temperature=0.0, want_json=True, want_vision=True)
    )

    normalized_note_id = str(note_id) if note_id else None
    extra_state: dict[str, Any] = {}
    if normalized_note_id:
        extra_state["note_id"] = normalized_note_id

    try:
        result = run_agent(
            kind="metric_recognize",
            build_fn=build_metric_graph,
            build_kwargs={
                "model": chosen_model,
                "image_bytes": image_bytes,
                "image_mime": image_mime,
                "image_name": image_name,
            },
            inputs={"note_id": normalized_note_id} if normalized_note_id else {},
            run_type=RunType.METRIC_RECOGNIZE,
            trigger=trigger,
            extra_state=extra_state,
        )
    except ImageValidationError as exc:
        # 上传文件不合法：422（消息本身已是完整中文指引）
        raise validation_error(str(exc), details={"reason": str(exc)}) from exc
    except NoteMissingError as exc:
        raise not_found(
            "笔记不存在，无法把识别结果关联到这篇笔记",
            details={"note_id": normalized_note_id},
        ) from exc
    except VisionUnsupportedError as exc:
        raise bad_request(
            str(exc),
            details={"reason": exc.reason, "doc_url": VISION_DOC_URL},
        ) from exc
    except RecognitionGiveUpError as exc:
        raise bad_request(str(exc), details={"reason": exc.reason}) from exc

    run_id = str(result["run_id"])
    state = result.get("state") or {}
    output = result.get("output")

    if not isinstance(output, dict) or not isinstance(output.get("metrics"), dict):
        # 理论上不可达（normalize 节点必然组装 output）；防御：给出可识别错误
        raise bad_request("识别未产出有效结果，请重试或更换截图")

    # ---- 审计回填（不写图片数据；llm_raw 脱敏 + 截断）----
    with SessionLocal() as db:
        _backfill_run(
            db,
            run_id,
            patch_outputs={
                "data_source": output.get("data_source"),
                "metrics": output.get("metrics"),
                "missing_fields": output.get("missing_fields"),
                "uncertain_fields": output.get("uncertain_fields"),
                "parse_attempts": output.get("parse_attempts"),
                "note_title": output.get("note_title"),
            },
            llm_raw=state.get("llm_raw"),
            note_id=normalized_note_id,
        )

    summary = {**output, "run_id": run_id}
    logger.info(
        "截图识别完成：run=%s note=%s 识别=%s 缺失=%s 不确定=%s parse_attempts=%s",
        run_id,
        normalized_note_id,
        sum(1 for value in (output.get("metrics") or {}).values() if value is not None),
        len(output.get("missing_fields") or []),
        len(output.get("uncertain_fields") or []),
        output.get("parse_attempts"),
    )
    return summary


__all__ = ["recognize_metric_screenshot", "LLM_RAW_MAX_LEN"]
