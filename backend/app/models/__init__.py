"""app.models 包入口。

**必须在此导入全部 10 张表的模型**，让 Alembic 的 ``target_metadata``
（即 ``app.db.base.Base.metadata``）能够发现所有表；
否则 autogenerate / 迁移会漏表。

表清单（10 张）：
    accounts, personas, hotspots, topics, notes, covers,
    note_metrics, diagnostics, knowledge_snippets, agent_runs
"""

from __future__ import annotations

from app.models.account import Account
from app.models.agent_run import AgentRun
from app.models.diagnostic import Diagnostic
from app.models.enums import (
    AccountStatus,
    ChunkType,
    ComplianceStatus,
    DataSource,
    DiagnosisType,
    HotspotStatus,
    NoteStatus,
    PersonaStatus,
    RefType,
    RunStatus,
    RunTrigger,
    RunType,
    SnippetStatus,
    TopicDifficulty,
    TopicStatus,
)
from app.models.hotspot import Hotspot
from app.models.knowledge import EMBEDDING_DIM, KnowledgeSnippet
from app.models.metric import NoteMetric
from app.models.note import Cover, Note
from app.models.persona import Persona
from app.models.topic import Topic

__all__ = [
    # 模型
    "Account",
    "Persona",
    "Hotspot",
    "Topic",
    "Note",
    "Cover",
    "NoteMetric",
    "Diagnostic",
    "KnowledgeSnippet",
    "AgentRun",
    # 常量
    "EMBEDDING_DIM",
    # 枚举
    "AccountStatus",
    "PersonaStatus",
    "HotspotStatus",
    "TopicStatus",
    "TopicDifficulty",
    "ComplianceStatus",
    "NoteStatus",
    "DataSource",
    "DiagnosisType",
    "ChunkType",
    "RefType",
    "SnippetStatus",
    "RunType",
    "RunTrigger",
    "RunStatus",
]
