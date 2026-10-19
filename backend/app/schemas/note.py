"""notes 的 Pydantic schema（第 12 步 · 创作 Agent；第 14 步 · 创作台增补）。

- ``NoteCreateRequest``：触发内容创作（同步接口；``account_id`` 可选，仅作兜底提示）；
- ``NoteUpdateRequest``（第 14 步）：人工编辑与状态流转（全可选；``status`` 出现即流转）；
- ``NoteListItem``：列表项（含人设名 / 账号名，**均非数据库列**）；
- ``NoteOut``：详情（含正文 / 合规自检信息；``llm_meta`` **只返回非敏感项**）；
- ``NoteRunOut``：创作 Agent 的运行审计精简记录。

安全约定：**任何响应模型都不包含 ``llm_raw``** —— 它只写进 ``agent_runs``，
接口一律不返回（与第 10 步同一口径）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ComplianceStatus, NoteStatus

#: ``llm_meta`` 对外只暴露的白名单键（其余一律不返回，防止未来误塞敏感项）
_LLM_META_WHITELIST = ("prompt_version", "json_parse_attempts", "model")


def sanitize_llm_meta(value: Any) -> dict[str, Any] | None:
    """把 ``notes.llm_meta`` 过滤成只含白名单键的 dict。"""
    if not isinstance(value, dict):
        return None
    filtered = {key: value[key] for key in _LLM_META_WHITELIST if key in value}
    return filtered or None


class NoteCreateRequest(BaseModel):
    """内容创作请求。"""

    topic_id: uuid.UUID = Field(description="选题 id（必填；必须存在且 status=approved）")
    account_id: uuid.UUID | None = Field(
        default=None,
        description="账号 id（可选兜底；归属账号以选题关联为准，此处仅在选题未关联账号时使用）",
    )


class NoteUpdateRequest(BaseModel):
    """人工编辑与状态流转请求（第 14 步 · 创作台）。

    语义要点：

    - 全字段可选；**「未提供」与「显式 null」不同** —— ``title`` 为非空列，
      显式 null 由路由拦为 422；``status`` 显式 null 同样 422（不流转就不要携带该字段）；
    - ``status`` 出现即视为**状态流转**请求；流转请求**不得**同时携带编辑字段
      （title / body / tags），由路由层判定为 400；
    - ``publish_url`` / ``xhs_note_id`` 仅在 ``status=published``（标记发布）时回填，
      两者均可留空；其余流转携带这两个字段会被拒（400）；
    - 状态机：``draft↔ready``、``ready→published``（标记发布）、``published→ready``（撤销发布）；
      禁止跳级（draft→published / published→draft）与 archived（本步不处理）；
    - 多余键按项目约定忽略（``extra="ignore"``）。
    """

    model_config = ConfigDict(extra="ignore")

    title: str | None = Field(
        default=None,
        max_length=100,
        description="标题（varchar(100) 非空列；显式 null 拒绝）",
    )
    body: str | None = Field(default=None, description="正文")
    tags: list[str] | None = Field(default=None, description="话题标签数组")
    status: NoteStatus | None = Field(default=None, description="目标状态（出现即状态流转）")
    publish_url: str | None = Field(
        default=None, description="作品链接（仅标记发布时回填，可留空）"
    )
    xhs_note_id: str | None = Field(
        default=None, max_length=64, description="小红书笔记 id（仅标记发布时回填，可留空）"
    )


class NoteListItem(BaseModel):
    """笔记列表项。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID | None
    persona_id: uuid.UUID | None
    topic_id: uuid.UUID | None
    title: str
    tags: list[str] | None
    compliance_status: ComplianceStatus
    status: NoteStatus
    publish_url: str | None = Field(
        default=None, description="作品链接（第 14 步增补：列表展示已发布外链所需）"
    )
    version: int
    created_at: datetime
    updated_at: datetime
    # 展示用（非数据库列）
    persona_name: str | None = None
    account_name: str | None = None
    # 合规概要（非数据库列：由 content_check 派生，便于列表快速辨识）
    forbidden_hits: int | None = None

    @classmethod
    def from_row(
        cls,
        note: object,
        persona_name: str | None = None,
        account_name: str | None = None,
    ) -> "NoteListItem":
        """由 ORM 对象 + join 名组装。"""
        content_check = getattr(note, "content_check", None)
        hits = content_check.get("hits") if isinstance(content_check, dict) else None
        return cls.model_validate(note).model_copy(
            update={
                "persona_name": persona_name,
                "account_name": account_name,
                "forbidden_hits": len(hits) if isinstance(hits, list) else None,
            }
        )


class NoteOut(BaseModel):
    """笔记详情（不含 ``llm_raw``；``llm_meta`` 只含非敏感项）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID | None
    persona_id: uuid.UUID | None
    topic_id: uuid.UUID | None
    title: str
    body: str | None
    tags: list[str] | None
    content_check: dict[str, Any] | None
    compliance_status: ComplianceStatus
    status: NoteStatus
    publish_url: str | None
    xhs_note_id: str | None
    published_at: datetime | None
    version: int
    llm_meta: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    # 展示用（非数据库列）
    persona_name: str | None = None
    account_name: str | None = None

    @classmethod
    def from_row(
        cls,
        note: object,
        persona_name: str | None = None,
        account_name: str | None = None,
    ) -> "NoteOut":
        """由 ORM 对象 + join 名组装（``llm_meta`` 过滤为白名单键）。"""
        return cls.model_validate(note).model_copy(
            update={
                "persona_name": persona_name,
                "account_name": account_name,
                "llm_meta": sanitize_llm_meta(getattr(note, "llm_meta", None)),
            }
        )


class NoteRunOut(BaseModel):
    """创作 Agent 运行记录（不含 ``llm_raw`` 大字段）。"""

    run_id: uuid.UUID
    trigger: str | None
    status: str | None
    created_at: datetime
    finished_at: datetime | None
    latency_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    json_parse_attempts: int | None
    error_message: str | None
    note_id: str | None = None
    title: str | None = None
    compliance_status: str | None = None
    forbidden_hits: int | None = None
    tags_count: int | None = None
    generated: int | None = None
    degraded: bool | None = None


__all__ = [
    "NoteCreateRequest",
    "NoteListItem",
    "NoteOut",
    "NoteRunOut",
    "NoteUpdateRequest",
    "sanitize_llm_meta",
]
