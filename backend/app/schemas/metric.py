"""note_metrics 的 Pydantic schema（第 15 步 · 数据回填）。

四个模型：

- ``MetricValues``：六指标值容器（全可空、非负整数）——识别响应的 ``metrics``；
- ``MetricSnapshotCreate``：**唯一写入口**的请求体（手工录入 / 截图核对后落库）；
- ``MetricSnapshotOut``：单条快照（逐字段对齐 ``note_metrics`` 表；
  ``screenshot_path`` 只回传文件名，不含目录）；
- ``MetricSnapshotListOut``：某笔记的数据时间线（``GET /api/v1/metrics``）。

红线（自检会断言）：任何响应都不含 ``llm_raw`` / base64 / 绝对路径 / API key；
``MetricSnapshotCreate`` **不接受** ``screenshot_path``（``extra="ignore"``）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.core.timeutil import LOCAL_TIMEZONE
from app.models.enums import DataSource


class MetricValues(BaseModel):
    """六个指标的值容器（缺失即 ``None``，绝不臆造）。"""

    views: int | None = Field(default=None, ge=0, description="浏览量（小眼睛）")
    likes: int | None = Field(default=None, ge=0, description="点赞")
    collects: int | None = Field(default=None, ge=0, description="收藏")
    comments: int | None = Field(default=None, ge=0, description="评论")
    shares: int | None = Field(default=None, ge=0, description="分享")
    new_followers: int | None = Field(default=None, ge=0, description="新增粉丝")


class MetricSnapshotCreate(BaseModel):
    """快照落库请求体（人工录入 / 识别候选核对后提交，**服务端唯一写入口**）。

    - ``note_id`` / ``captured_at`` 必填；``captured_at`` 无时区时按项目本地
      时区（UTC+8）解释（前端 ``datetime-local`` 控件给的就是本地时间）；
    - 六指标全部可空、必须非负整数（负数 / 明显非整数 → 422）；
    - ``data_source`` 默认 ``manual``（识别链路显式传 ``screenshot``）；
    - ``evidence`` 可选：识别链路的证据 JSON 原样回传。服务端只从中提取
      **服务端生成**的 ``screenshot_file``（相对文件名）写入 ``screenshot_path`` 列；
    - ``extra="ignore"``：**不接受** ``screenshot_path`` 等未知字段（客户端传了
      也静默丢弃，绝不落进库）。
    """

    model_config = ConfigDict(extra="ignore")

    note_id: uuid.UUID = Field(description="所属笔记 id（必填）")
    captured_at: datetime = Field(description="数据采集时刻（必填，ISO 8601）")
    data_source: DataSource = Field(
        default=DataSource.MANUAL, description="来源：manual（手工）/ screenshot（截图识别）"
    )
    views: int | None = Field(default=None, ge=0)
    likes: int | None = Field(default=None, ge=0)
    collects: int | None = Field(default=None, ge=0)
    comments: int | None = Field(default=None, ge=0)
    shares: int | None = Field(default=None, ge=0)
    new_followers: int | None = Field(default=None, ge=0)
    evidence: dict[str, Any] | None = Field(
        default=None, description="证据 JSON（识别链路的 evidence 原样回传，可不给）"
    )

    @field_validator("captured_at")
    @classmethod
    def _assume_local_when_naive(cls, value: datetime) -> datetime:
        """无时区的输入按 Asia/Shanghai（UTC+8）解释，避免落库时按服务器时区错位。"""
        if value.tzinfo is None:
            return value.replace(tzinfo=LOCAL_TIMEZONE)
        return value


class MetricSnapshotOut(BaseModel):
    """单条快照输出（逐字段对齐 ``note_metrics`` 表）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    note_id: uuid.UUID
    captured_at: datetime
    data_source: DataSource
    views: int | None
    likes: int | None
    collects: int | None
    comments: int | None
    shares: int | None
    new_followers: int | None
    evidence: dict[str, Any] | None
    screenshot_path: str | None = Field(
        default=None, description="截图留档文件名（仅文件名，不含任何目录）"
    )
    created_at: datetime

    @field_serializer("screenshot_path")
    def _file_name_only(self, value: str | None) -> str | None:
        """序列化时只回传文件名（防御：即使库里是路径形态的旧值，也不泄漏目录）。"""
        if not value:
            return None
        return value.replace("\\", "/").rsplit("/", 1)[-1] or None


class MetricSnapshotListOut(BaseModel):
    """``GET /api/v1/metrics`` 响应：某笔记的数据时间线（``captured_at`` 倒序）。"""

    note_id: uuid.UUID
    items: list[MetricSnapshotOut]


class MetricRecognizeResult(BaseModel):
    """``POST /api/v1/metrics/recognize`` 的识别候选项（**绝不写库**，人工核对后才落库）。

    对齐识别 Agent 的 output 契约：六指标候选 + evidence + missing / uncertain
    + 解析统计 + warnings；另附 ``note_id`` / ``note_title`` 供人工核对的界面回显。
    绝不含 base64 / 绝对路径 / ``llm_raw`` / API key。
    """

    data_source: DataSource = Field(
        default=DataSource.SCREENSHOT, description="恒为 screenshot（识别来源）"
    )
    metrics: MetricValues = Field(description="六指标候选（取不到即 null）")
    evidence: dict[str, Any] | None = Field(
        default=None, description="每字段来源 / 置信度 / missing / uncertain 明细"
    )
    captured_at: datetime = Field(description="识别时刻（服务端生成）")
    missing_fields: list[str] = Field(default_factory=list, description="未能识别的字段")
    uncertain_fields: list[str] = Field(default_factory=list, description="识别出但不确信的字段")
    parse_attempts: int = Field(default=0, description="JSON 解析尝试次数（正常为 1）")
    warnings: list[str] = Field(default_factory=list)
    note_id: uuid.UUID | None = Field(default=None, description="关联笔记 id（若提交了）")
    note_title: str | None = Field(default=None, description="关联笔记标题（供人工核对）")
    screenshot_file: str | None = Field(
        default=None,
        description="截图留档文件名（仅当 SAVE_SCREENSHOT=true 且用户确认保存时给出）",
    )
    run_id: str | None = Field(default=None, description="本次识别在 agent_runs 的运行 id")


__all__ = [
    "MetricValues",
    "MetricSnapshotCreate",
    "MetricSnapshotOut",
    "MetricSnapshotListOut",
    "MetricRecognizeResult",
]
