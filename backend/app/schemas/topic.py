"""topics 的 Pydantic schema（第 10 步）。

- ``TopicGenerateRequest``：触发选题生成（同步接口）；
- ``TopicUpdateRequest``：人工微调（本步仅允许 draft→approved / draft→discarded）；
- ``TopicListItem``：列表项（含人设名 / 账号名 / 溯源首条热点，**均非数据库列**）；
- ``TopicOut``：详情（含 ``meta``；**不含** ``llm_raw`` —— 它在 ``agent_runs``）；
- ``TopicRunOut``：选题 Agent 的运行审计精简记录。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from pydantic.functional_validators import AfterValidator

from app.models.enums import TopicDifficulty, TopicStatus
from app.schemas.common import OptText, blank_to_none

TrimmedTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
OptPillar = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=100),
    AfterValidator(blank_to_none),
] | None


def _clean_str_list(value: list[str] | None) -> list[str] | None:
    """去空白、去空项、去重（保留顺序）。"""
    if value is None:
        return None
    result: list[str] = []
    for item in value:
        token = item.strip()
        if token and token not in result:
            result.append(token)
    return result


def _clean_tags(value: list[str] | None) -> list[str] | None:
    """话题标签：剥前导 ``#``、去空白 / 空项、去重（保留顺序）。"""
    if value is None:
        return None
    result: list[str] = []
    for item in value:
        token = item.strip().lstrip("#").strip()
        if token and token not in result:
            result.append(token)
    return result


OptStrList = Annotated[list[str], AfterValidator(_clean_str_list)] | None
OptTagList = Annotated[list[str], AfterValidator(_clean_tags)] | None


class TopicGenerateRequest(BaseModel):
    """选题生成请求。"""

    persona_id: uuid.UUID = Field(description="人设 id（必填）")
    hotspot_ids: list[uuid.UUID] | None = Field(
        default=None, description="指定热点；为空则按当天优先 + 热度自动取"
    )
    per_hotspot: int = Field(default=3, ge=1, le=20, description="每个热点生成条数")
    limit_hotspots: int = Field(default=3, ge=1, le=10, description="自动取热点个数上限")
    force: bool = Field(default=False, description="true 时跳过「已覆盖」判断，强制重生成")


class TopicUpdateRequest(BaseModel):
    """人工微调选题（本步 status 仅允许 draft → approved / discarded）。"""

    title: TrimmedTitle | None = None
    summary: OptText | None = None
    angle: OptText | None = None
    pillar: OptPillar = None
    content_outline: OptStrList = None
    tags: OptTagList = None
    target_keywords: OptStrList = None
    review_note: OptText | None = None
    status: TopicStatus | None = None


class TopicListItem(BaseModel):
    """选题列表项。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID | None
    persona_id: uuid.UUID | None
    title: str
    angle: str | None
    summary: str | None
    pillar: str | None
    tags: list[str] | None
    heat_score: float | None
    difficulty: TopicDifficulty | None
    score: float | None
    status: TopicStatus
    source_hotspot_ids: list[Any] | None
    generated_at: datetime | None
    created_at: datetime
    # 展示用（非数据库列）
    persona_name: str | None = None
    account_name: str | None = None
    source_title: str | None = None
    source_platform: str | None = None

    @classmethod
    def from_row(
        cls,
        topic: object,
        persona_name: str | None = None,
        account_name: str | None = None,
    ) -> "TopicListItem":
        """由 ORM 对象 + join 名 + 溯源首条热点摘要组装。"""
        trace = getattr(topic, "source_trace", None)
        first = trace[0] if isinstance(trace, list) and trace else {}
        if not isinstance(first, dict):
            first = {}
        return cls.model_validate(topic).model_copy(
            update={
                "persona_name": persona_name,
                "account_name": account_name,
                "source_title": first.get("title"),
                "source_platform": first.get("source"),
            }
        )


class TopicOut(BaseModel):
    """选题详情（不含 ``llm_raw``）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID | None
    persona_id: uuid.UUID | None
    title: str
    angle: str | None
    summary: str | None
    pillar: str | None
    content_outline: list[str] | None
    tags: list[str] | None
    target_keywords: list[str] | None
    source_hotspot_ids: list[Any] | None
    source_trace: list[Any] | None
    heat_score: float | None
    difficulty: TopicDifficulty | None
    score: float | None
    llm_model: str | None
    meta: dict[str, Any] | None
    review_note: str | None
    status: TopicStatus
    generated_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # 展示用（非数据库列）
    persona_name: str | None = None
    account_name: str | None = None

    @classmethod
    def from_row(
        cls,
        topic: object,
        persona_name: str | None = None,
        account_name: str | None = None,
    ) -> "TopicOut":
        """由 ORM 对象 + join 名组装。"""
        return cls.model_validate(topic).model_copy(
            update={"persona_name": persona_name, "account_name": account_name}
        )


class TopicRunOut(BaseModel):
    """选题 Agent 运行记录（默认不含 llm_raw 大字段）。"""

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
    generated: int | None = None
    persisted: int | None = None
    rejected: int | None = None
    skipped: int | None = None
    topic_ids: list[str] | None = None


__all__ = [
    "TopicGenerateRequest",
    "TopicUpdateRequest",
    "TopicListItem",
    "TopicOut",
    "TopicRunOut",
]
