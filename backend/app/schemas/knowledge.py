"""知识库 schema（第 18 步 · RAG；第 19 步 · 人工维护扩展）。

第 18 步模型：

- ``SnippetOut``：知识块（列表 / 详情共用；**不含 embedding 向量**）；
- ``SnippetListOut``：分页列表（items + total / limit / offset）；
- ``ExtractResultOut``：``POST /extract`` 响应（规律提炼结果摘要；
  ``insufficient_sample=True`` 表示样本不足、零写入，``data_gaps`` 给出指引）；
- ``SnippetSearchRequest`` / ``SearchHit`` / ``SnippetSearchOut``：检索测试
  （命中含 similarity 分数；**不含向量、不含 llm_raw**）；
- ``SnippetUpdateRequest``：状态切换（第 19 步扩展为可编辑 title / content）。

第 19 步新增模型：

- ``SnippetManualCreateRequest``：``POST /manual`` 手动创建（六类型 + 可选人设关联）；
- ``SnippetWriteOut``：创建 / 编辑响应（= 知识块 + ``warnings`` 非阻断提示）；
- ``SeedForbiddenWordsOut``：``POST /seed-forbidden-words`` 幂等同步内置词表结果；
- ``ImportTextRequest`` / ``ImportResultOut``：文档导入（文本 / 文件共用结果；
  如实计数 total_chunks / inserted / skipped / failed）。

红线（自检会断言）：任何响应都不含 ``embedding`` 原始向量 / ``llm_raw`` /
API key / 绝对路径。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: 知识块类型白名单（与 models.enums.ChunkType 一致）
ChunkTypeName = Literal[
    "hit_pattern",
    "forbidden_word",
    "persona_knowledge",
    "topic_template",
    "review_insight",
    "general",
]

#: 知识块状态
SnippetStatusName = Literal["active", "archived"]


class SnippetOut(BaseModel):
    """单条知识块（**不含 embedding 向量**）。"""

    model_config = ConfigDict(extra="ignore")

    id: uuid.UUID
    chunk_type: ChunkTypeName
    ref_type: str | None = None
    title: str | None = None
    content: str
    meta: dict[str, Any] | None = Field(
        default=None, description="来源 / 证据 / 样本量 / 置信度 / content_hash 等"
    )
    status: SnippetStatusName
    created_at: datetime


class SnippetListOut(BaseModel):
    """``GET /api/v1/knowledge`` 列表（``created_at`` 倒序）。"""

    model_config = ConfigDict(extra="ignore")

    items: list[SnippetOut] = Field(default_factory=list)
    total: int = Field(description="过滤后的总数（不受分页影响）")
    limit: int
    offset: int


class ExtractResultOut(BaseModel):
    """``POST /api/v1/knowledge/extract`` 响应（规律提炼结果摘要）。"""

    model_config = ConfigDict(extra="ignore")

    run_id: uuid.UUID = Field(description="本次运行 id（可在 agent_runs 复查）")
    status: str = Field(description="success / partial（样本不足或有条目被剔除）")
    insufficient_sample: bool = Field(
        description="样本不足：未调用模型、未写入任何知识块（data_gaps 有指引）"
    )
    sample_size: int | None = Field(default=None, description="当前已发布笔记数")
    min_required: int | None = Field(default=None, description="样本门槛")
    generated: int = Field(default=0, description="模型产出且合格的规律条数")
    persisted: int = Field(default=0, description="本次新写入条数")
    reused: int = Field(default=0, description="content_hash 已存在（幂等跳过）条数")
    skipped: int = Field(default=0, description="批内重复跳过条数")
    snippet_ids: list[str] = Field(default_factory=list)
    rejected: int = Field(default=0, description="被剔除的不合格规律条数")
    data_gaps: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    parse_attempts: int | None = Field(default=None, description="JSON 解析尝试（理想为 1）")
    prompt_version: str | None = None


class SnippetSearchRequest(BaseModel):
    """``POST /api/v1/knowledge/search`` 请求体（检索测试）。"""

    model_config = ConfigDict(extra="ignore")

    query: str = Field(min_length=1, max_length=500, description="查询文本")
    chunk_types: list[ChunkTypeName] | None = Field(
        default=None, description="限定知识块类型；缺省 = 不限定"
    )
    k: int = Field(default=5, ge=1, le=20, description="返回条数")


class SearchHit(BaseModel):
    """检索命中（**不含 embedding 向量**）。"""

    model_config = ConfigDict(extra="ignore")

    id: uuid.UUID
    chunk_type: ChunkTypeName
    title: str | None = None
    content: str
    meta: dict[str, Any] | None = None
    status: SnippetStatusName
    created_at: datetime
    similarity: float = Field(description="余弦相似度（-1~1；越大越相似）")


class SnippetSearchOut(BaseModel):
    """``POST /api/v1/knowledge/search`` 响应（top-k，按相似度降序）。"""

    model_config = ConfigDict(extra="ignore")

    query: str
    items: list[SearchHit] = Field(default_factory=list)


class SnippetUpdateRequest(BaseModel):
    """``PATCH /api/v1/knowledge/{id}`` 请求体（第 19 步扩展；无物理删除）。

    - 仅传 ``status`` 时行为与第 18 步**完全一致**（active / archived 切换）；
    - ``title`` / ``content`` 显式传入才更新（缺省 = 不修改；空串标题 = 清空）；
    - ``content`` 变化触发重算 ``meta.content_hash`` 并重新向量化
      （失败置空向量 + 响应 ``warnings``，正文照常保存）。
    """

    model_config = ConfigDict(extra="ignore")

    status: SnippetStatusName | None = Field(
        default=None, description="仅允许 active / archived 间切换"
    )
    title: str | None = Field(
        default=None, max_length=200, description="新标题（缺省不改；空串 = 清空）"
    )
    content: str | None = Field(
        default=None,
        min_length=1,
        max_length=20000,
        description="新正文（变化时重算 content_hash 并重新向量化）",
    )


class SnippetManualCreateRequest(BaseModel):
    """``POST /api/v1/knowledge/manual`` 请求体（第 19 步 · 手动创建知识块）。"""

    model_config = ConfigDict(extra="ignore")

    chunk_type: ChunkTypeName = Field(description="知识块类型（六选一）")
    title: str | None = Field(default=None, max_length=200)
    content: str = Field(min_length=1, max_length=20000, description="知识块正文")
    category: str | None = Field(
        default=None,
        max_length=64,
        description="违禁词类别（仅 chunk_type=forbidden_word 使用；缺省 manual）",
    )
    ref_type: Literal["persona", "manual_doc"] | None = Field(
        default=None, description="可选弱关联类型（persona 需带 ref_id）"
    )
    ref_id: uuid.UUID | None = Field(
        default=None, description="ref_type=persona 时为必填（人设 id，须存在）"
    )


class SnippetWriteOut(BaseModel):
    """写操作（手动创建 / 编辑）响应：知识块 + ``warnings``（**不含 embedding 向量**）。"""

    model_config = ConfigDict(extra="ignore")

    id: uuid.UUID
    chunk_type: ChunkTypeName
    ref_type: str | None = None
    title: str | None = None
    content: str
    meta: dict[str, Any] | None = None
    status: SnippetStatusName
    created_at: datetime
    warnings: list[str] = Field(
        default_factory=list,
        description="非阻断提示（如重向量化失败后 embedding 已置空的说明）",
    )


class SeedForbiddenWordsOut(BaseModel):
    """``POST /api/v1/knowledge/seed-forbidden-words`` 响应（幂等同步内置词表）。"""

    model_config = ConfigDict(extra="ignore")

    total: int = Field(description="内置词表总词数")
    inserted: int = Field(description="本次新写入条数（dry_run 时恒 0）")
    reused: int = Field(description="content_hash 已存在（幂等跳过）条数")
    would_insert: int = Field(description="本次将写入条数（dry_run 可预检）")
    dry_run: bool


class ImportTextRequest(BaseModel):
    """``POST /api/v1/knowledge/import-text`` 请求体（第 19 步 · 文档导入）。"""

    model_config = ConfigDict(extra="ignore")

    content: str = Field(min_length=1, max_length=200000, description="待导入文本")
    chunk_type: Literal["general", "persona_knowledge"] = Field(
        default="general", description="默认 general；persona_knowledge 需带 ref_id"
    )
    ref_id: uuid.UUID | None = Field(
        default=None, description="chunk_type=persona_knowledge 时必填（人设 id）"
    )
    chunk_size: int | None = Field(
        default=None, ge=200, le=4000, description="切分块大小（字符；缺省 800）"
    )
    chunk_overlap: int | None = Field(
        default=None, ge=0, le=500, description="块间重叠（字符；缺省 100）"
    )


class ImportResultOut(BaseModel):
    """文档导入结果（文本 / 文件共用；**如实计数**）。"""

    model_config = ConfigDict(extra="ignore")

    total_chunks: int = Field(description="切分总块数")
    inserted: int = Field(description="写入条数")
    skipped: int = Field(description="跳过条数（库中已存在 / 批内重复）")
    failed: int = Field(description="向量化失败条数（如实计数、不影响其它块）")
    snippet_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    chunk_size: int
    chunk_overlap: int
    splitter: str = Field(description="实际使用的切分实现（官方 / 内置等效）")


__all__ = [
    "ChunkTypeName",
    "ExtractResultOut",
    "ImportResultOut",
    "ImportTextRequest",
    "SearchHit",
    "SeedForbiddenWordsOut",
    "SnippetListOut",
    "SnippetManualCreateRequest",
    "SnippetOut",
    "SnippetSearchOut",
    "SnippetSearchRequest",
    "SnippetStatusName",
    "SnippetUpdateRequest",
    "SnippetWriteOut",
]
