"""数据回填路由：``/api/v1/metrics``（第 15 步）。

契约：

    POST  /api/v1/metrics             落库一条快照（人工录入 / 识别核对后提交）
    GET   /api/v1/metrics?note_id=    某笔记的数据时间线（captured_at 倒序）
    POST  /api/v1/metrics/recognize   截图识别（multipart；返回候选，**绝不写库**）

要点：

- **POST 是唯一写入口**：``screenshot_path`` 不由客户端决定 —— 服务端只从
  ``evidence.screenshot_file`` 提取**自己生成**的文件名（强格式校验，防路径注入）；
- **识别结果必须人工核对后才落库**：``/recognize`` 只返回候选（六指标 + evidence +
  missing / uncertain），不碰 ``note_metrics`` 表；
- 本步**不提供 PUT / DELETE**：快照不做更新、不做物理删除（随笔记 CASCADE）。
- 截图落盘默认关闭（``SAVE_SCREENSHOT=false`` 时识别链路不写任何文件）；
  仅当配置为 true **且** 用户在识别请求显式确认（``save_screenshot=true``）时，
  写入 ``SCREENSHOT_DIR``（相对 ``backend/``；目录已加入 .gitignore）。

⚠️ 路由层不写任何 SQLAlchemy 表达式（第 8 步分层纪律，``selftest_api`` 静态断言）。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.orm import Session

from app.agent.errors import AgentConfigError
from app.agent.metrics.contracts import SUFFIX_BY_MIME, detect_image_mime
from app.agent.metrics.service import recognize_metric_screenshot
from app.api.deps import get_db
from app.core.config import settings
from app.core.errors import bad_request, not_found
from app.crud.metric import crud_metric
from app.crud.note import crud_note
from app.schemas.common import ErrorOutSpec
from app.schemas.metric import (
    MetricRecognizeResult,
    MetricSnapshotCreate,
    MetricSnapshotListOut,
    MetricSnapshotOut,
)

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])

#: 截图留档文件名的允许格式（**只有服务端生成的名字形态**；带任何路径成分一律拒绝）
_SCREENSHOT_FILE_PATTERN = re.compile(r"^[0-9A-Za-z_\-]{8,80}\.(?:jpg|png|webp)$")


def _ensure_note(db: Session, note_id: uuid.UUID) -> None:
    """所属笔记必须存在，否则 404。"""
    if crud_note.get(db, note_id) is None:
        raise not_found("笔记不存在", details={"note_id": str(note_id)})


def _extract_screenshot_file(evidence: dict[str, Any] | None) -> str | None:
    """从 ``evidence`` 提取服务端生成的截图文件名；形状不合法 → ``None``。

    客户端**不能**直接传 ``screenshot_path``（schema 里没有该字段、传了也忽略）；
    唯一通道是识别响应里回显的 ``evidence.screenshot_file``（服务端生成），
    且必须通过 :data:`_SCREENSHOT_FILE_PATTERN` 的强格式校验（拒绝路径成分）。
    """
    if not isinstance(evidence, dict):
        return None
    candidate = evidence.get("screenshot_file")
    if not isinstance(candidate, str):
        return None
    candidate = candidate.strip()
    if not _SCREENSHOT_FILE_PATTERN.match(candidate):
        return None
    return candidate


def _save_screenshot(image_bytes: bytes, mime: str) -> str:
    """把截图写入 ``SCREENSHOT_DIR``（仅在显式开启 + 用户确认时调用），返回相对文件名。

    文件名形态：``{UTC 时间戳}_{内容 sha256 前 12 位}{后缀}``——同一张图重复保存
    会得到同一名字（覆盖写，天然幂等），也便于人工在目录里按时间浏览。
    """
    directory = settings.screenshot_dir_path
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(image_bytes).hexdigest()[:12]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = SUFFIX_BY_MIME.get(mime) or ".png"
    name = f"{stamp}_{digest}{suffix}"
    (directory / name).write_bytes(image_bytes)
    return name


# ---------------------------------------------------------------------------
# GET ""（数据时间线；note_id 必填）
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=MetricSnapshotListOut,
    summary="某笔记的数据时间线",
    responses={**ErrorOutSpec.BAD_REQUEST, **ErrorOutSpec.NOT_FOUND},
)
def list_metric_snapshots(
    note_id: uuid.UUID | None = Query(None, description="所属笔记 id（必填）"),
    db: Session = Depends(get_db),
) -> MetricSnapshotListOut:
    """按采集时刻倒序返回某笔记的全部快照（``id`` 兜底保证顺序稳定）。"""
    if note_id is None:
        raise bad_request("note_id 为必填查询参数", details={"field": "note_id"})
    _ensure_note(db, note_id)
    items = crud_metric.list_by_note(db, note_id)
    return MetricSnapshotListOut(
        note_id=note_id,
        items=[MetricSnapshotOut.model_validate(item) for item in items],
    )


# ---------------------------------------------------------------------------
# POST ""（落库一条快照；唯一写入口）
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=MetricSnapshotOut,
    status_code=201,
    summary="落库一条数据快照（人工确认后）",
    responses={
        **ErrorOutSpec.VALIDATION,
        **ErrorOutSpec.NOT_FOUND,
        **ErrorOutSpec.CONFLICT,
    },
)
def create_metric_snapshot(
    payload: MetricSnapshotCreate, db: Session = Depends(get_db)
) -> MetricSnapshotOut:
    """落库一条快照；同笔记同 ``captured_at`` 已存在 → 409。

    ``screenshot_path`` 的服务端受控赋值：仅当 ``evidence.screenshot_file``
    是识别接口生成并回显的合法文件名时才写入该列，否则留 NULL。
    """
    _ensure_note(db, payload.note_id)
    data = payload.model_dump()
    data["screenshot_path"] = _extract_screenshot_file(payload.evidence)
    metric = crud_metric.create_snapshot(db, data)
    return MetricSnapshotOut.model_validate(metric)


# ---------------------------------------------------------------------------
# POST "/recognize"（截图识别；multipart；返回候选，绝不写库）
# ---------------------------------------------------------------------------
@router.post(
    "/recognize",
    response_model=MetricRecognizeResult,
    summary="截图识别（返回六指标候选，人工核对后才落库）",
    responses={
        **ErrorOutSpec.BAD_REQUEST,
        **ErrorOutSpec.NOT_FOUND,
        **ErrorOutSpec.VALIDATION,
    },
)
def recognize_metric(
    file: UploadFile = File(..., description="小红书创作中心数据截图（png / jpeg / webp）"),
    note_id: uuid.UUID | None = Form(None, description="可选：关联的笔记 id"),
    save_screenshot: bool = Form(
        False, description="是否留档截图（仅当后端 SAVE_SCREENSHOT=true 时生效）"
    ),
) -> MetricRecognizeResult:
    """识别单张截图，返回**候选值**（不写库）。

    - 图片类型 / 大小 / 文件头魔数非法 → 422；笔记不存在 → 404；
    - 视觉未配置 → 400（附配置指引）；模型不支持图片 → 400（附视觉文档）；
    - JSON 连续解析失败重试超限 → 400（可重试 / 换截图）。
    """
    image_bytes = file.file.read()
    image_mime = (file.content_type or "").strip().lower()

    try:
        result = recognize_metric_screenshot(
            image_bytes,
            image_mime,
            note_id=note_id,
            image_name=file.filename,
        )
    except AgentConfigError as exc:
        raise bad_request(
            "视觉识别未就绪，无法识别截图",
            details={"reason": str(exc)},
        ) from exc

    # --- 截图留档（默认整体关闭；两把闸门都开才落盘）---
    screenshot_file: str | None = None
    if settings.SAVE_SCREENSHOT and save_screenshot:
        detected = detect_image_mime(image_bytes)
        if detected is not None:
            screenshot_file = _save_screenshot(image_bytes, detected)
            evidence = result.get("evidence")
            if isinstance(evidence, dict):
                # 服务端生成、随 evidence 回显；落库时由 POST /metrics 提取
                evidence["screenshot_file"] = screenshot_file

    result["screenshot_file"] = screenshot_file
    return MetricRecognizeResult.model_validate(result)
