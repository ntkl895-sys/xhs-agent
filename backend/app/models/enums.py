"""枚举定义（集中管理）。

约定（第 7 步）：

- **一律用字符串存储**，不建 PostgreSQL 原生 ENUM 类型。
  原因：原生 ENUM 后期新增取值需要 `ALTER TYPE`，迁移成本高；
  用 ``Enum(native_enum=False)`` 时数据库侧就是普通 ``VARCHAR``，
  取值合法性由应用层（Pydantic / ORM）保证。
- 通过 :func:`sa_enum` 构造列类型，确保落库时存的是**枚举值**（如 ``"draft"``）
  而不是成员的**名字**（如 ``"DRAFT"``）——这是 SQLAlchemy ``Enum`` 的默认行为，
  必须用 ``values_callable`` 显式纠正。
"""

from __future__ import annotations

from enum import Enum
from typing import TypeVar

from sqlalchemy import Enum as SAEnum

E = TypeVar("E", bound=Enum)


# --------------------------------------------------------------------------
# accounts / personas
# --------------------------------------------------------------------------
class AccountStatus(str, Enum):
    """小红书账号状态（不做物理删除，用 archived 归档）。"""

    ACTIVE = "active"
    ARCHIVED = "archived"


class PersonaStatus(str, Enum):
    """写作人设状态。"""

    ACTIVE = "active"
    ARCHIVED = "archived"


# --------------------------------------------------------------------------
# hotspots
# --------------------------------------------------------------------------
class HotspotStatus(str, Enum):
    """热搜条目的处理状态。"""

    NEW = "new"
    ADOPTED = "adopted"      # 已被选为选题来源
    IGNORED = "ignored"      # 人工忽略


# --------------------------------------------------------------------------
# topics
# --------------------------------------------------------------------------
class TopicStatus(str, Enum):
    """选题生命周期状态机。"""

    DRAFT = "draft"          # 生成 / 暂存
    APPROVED = "approved"    # 人工评审通过
    WRITING = "writing"      # 正在据此写笔记
    PUBLISHED = "published"  # 已产出并发布
    DISCARDED = "discarded"  # 废弃


class TopicDifficulty(str, Enum):
    """选题创作难度（模型给的粗粒度估计）。"""

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


# --------------------------------------------------------------------------
# notes
# --------------------------------------------------------------------------
class ComplianceStatus(str, Enum):
    """违禁词 / 合规自检结论。"""

    PENDING = "pending"
    PASSED = "passed"
    WARNING = "warning"


class NoteStatus(str, Enum):
    """笔记作品状态机（人工审核 + 手动发布）。"""

    DRAFT = "draft"
    READY = "ready"          # 已定稿，等待人工发布
    PUBLISHED = "published"  # 人工已发布，回填 publish_url
    ARCHIVED = "archived"


# --------------------------------------------------------------------------
# note_metrics
# --------------------------------------------------------------------------
class DataSource(str, Enum):
    """数据快照来源。"""

    SCREENSHOT = "screenshot"  # 截图识别（第 15 步）
    MANUAL = "manual"          # 人工录入


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------
class DiagnosisType(str, Enum):
    """单篇诊断类型。"""

    COLD_START = "cold_start"  # 冷启动诊断
    REVIEW = "review"          # 复盘
    ANOMALY = "anomaly"        # 异常波动


# --------------------------------------------------------------------------
# knowledge_snippets
# --------------------------------------------------------------------------
class ChunkType(str, Enum):
    """知识块类型（第 18 / 19 步 RAG 用）。"""

    HIT_PATTERN = "hit_pattern"            # 爆款规律
    FORBIDDEN_WORD = "forbidden_word"      # 违禁词
    PERSONA_KNOWLEDGE = "persona_knowledge"
    TOPIC_TEMPLATE = "topic_template"
    REVIEW_INSIGHT = "review_insight"
    GENERAL = "general"


class RefType(str, Enum):
    """知识块的多态来源类型（不加外键，弱关联）。

    ``PERSONA`` 于第 19 步（知识库人工维护）增补：人设知识库
    （话术 / 风格 / 禁忌 / 口头禅），``ref_id`` 弱关联 personas.id。
    本枚举落库为 ``VARCHAR(32)``（``native_enum=False``），新增取值**无 DDL / 迁移**。
    """

    NOTE = "note"
    DIAGNOSTIC = "diagnostic"
    TOPIC = "topic"
    MANUAL_DOC = "manual_doc"
    PERSONA = "persona"


class SnippetStatus(str, Enum):
    """知识块状态。"""

    ACTIVE = "active"
    ARCHIVED = "archived"


# --------------------------------------------------------------------------
# agent_runs
# --------------------------------------------------------------------------
class RunType(str, Enum):
    """Agent 运行类型。

    ``HOTSPOT_FETCH`` 于第 9 步（热搜服务化）增补：热搜采集**不接 LLM**，
    但仍按同一条审计链路写入 ``agent_runs``（``llm_*`` 字段留空）。
    ``METRIC_RECOGNIZE`` 于第 15 步（数据回填）增补：截图识别走视觉模型。
    ``WEEKLY_REPORT`` 于第 17 步（运营看板 + 周报）增补：周报结论**不落业务表**，
    整体写入 ``agent_runs.outputs``。
    ``INSIGHT`` 于第 18 步（爆款规律 RAG 反哺）增补：规律提炼（第六个业务图）
    的结论写入 ``knowledge_snippets``（chunk_type=hit_pattern / review_insight）。
    ``CONTENT_VARIANTS`` 于第 23 步 · V2 增强（二）增补：同选题多版本创作
    （第七个业务图）的产出为**同批次 N 条 notes**（variant_group_id = run_id）。
    本枚举落库为 ``VARCHAR(32)``（``native_enum=False``），新增取值**无需 DDL / 迁移**。
    """

    HOTSPOT_FETCH = "hotspot_fetch"
    TOPIC_GENERATION = "topic_generation"
    CONTENT_CREATION = "content_creation"
    CONTENT_VARIANTS = "content_variants"
    METRIC_RECOGNIZE = "metric_recognize"
    DIAGNOSIS = "diagnosis"
    WEEKLY_REPORT = "weekly_report"
    INSIGHT = "insights"
    EMBEDDING = "embedding"
    OTHER = "other"


class RunTrigger(str, Enum):
    """触发方式。"""

    MANUAL = "manual"
    SCHEDULED = "scheduled"


class RunStatus(str, Enum):
    """运行结果。"""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


def sa_enum(enum_cls: type[E], *, length: int, name: str | None = None) -> SAEnum:
    """构造「字符串存储」的枚举列类型。

    - ``native_enum=False``：数据库侧落为 ``VARCHAR(length)``，不建 PG 原生 ENUM；
    - ``values_callable``：落库存**枚举值**（``"draft"``）而非成员名（``"DRAFT"``）；
    - 不生成 CHECK 约束（SQLAlchemy 2.x 默认 ``create_constraint=False``），
      避免后期加值需要迁移。
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        name=name,
        values_callable=lambda e: [m.value for m in e],
    )
