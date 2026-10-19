"""第 19 步 · 知识库人工维护离线自检（违禁词入库 / 手动 CRUD / 人设知识 / 文档导入）。

覆盖（对任务书第九节的逐条对应）：

0. 契约与结构：RefType.PERSONA、自定义词表扫描（build_word_index / wordlist 参数）、
   切分器就绪（官方优先 / 零依赖兜底）、embedding 闸门探测函数；
1. 违禁词三路径：空库回退内置 / 知识库自定义词命中（且不回退内置）/ 读取异常回退内置；
2. seed 幂等：dry_run 零写入、首次写入、二次 reused / 新增 0；forbidden_word 允许空向量；
3. 手动创建（HTTP /manual）：各类型落库、forbidden_word 无向量可建、人设关联与 404/400、
   向量化失败 400 不落库、**未装依赖时 400 且违禁词仍可离线维护**；
4. 编辑（PATCH 扩展）：title / content 重向量化成功与失败置空（正文保留 + warnings）、
   仅改 status 与第 18 步一致、404 / 422、forbidden_word 编辑不触发向量化；
5. 人设知识 RAG：纳入渲染、他人设隔离、归档不纳入、读取失败降级、与向量检索融合去重；
6. 文档导入：文本多块 / 幂等跳过 / 部分失败计数 / 人设关联、文件 .txt/.md 接受与
   .pdf / 非 UTF-8 / 空文件拒绝、切分参数校验；
7. 创作图合规双路径：知识库自定义词命中 / 回退内置词表命中（graph.invoke 真跑图）；
8. 接口面与零迁移：方法精确（无 PUT / DELETE）、静态路径先注册、422、响应无敏感字段、
   include_router ≥ 12 且 lifespan 唯一、路由层零 SQLAlchemy 表达式、迁移恰 2 支；
9. 合规扫描与清理复查：新增/修改文件无小红书域名 / 登录态凭据 / 密钥前缀、
   全前端平台域名仍恰 1 处；测试数据按 id + 前缀双口径删除、残留 0。

运行方式（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\selftest_knowledge_admin.py

依赖本地 docker PostgreSQL；**不联网、不下载向量模型、不需要真实 key**
（向量化经注入假 embedder / 语义分级 patch）。退出码：0 = 全绿；1 = 有失败。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

# ---- 入口先摘掉沙箱注入的代理变量（本机全部真实请求进程的共同约定）----
for _key in (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
):
    os.environ.pop(_key, None)

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
FRONTEND_SRC = ROOT_DIR / "frontend" / "src"
VERSIONS_DIR = BACKEND_DIR / "migrations" / "versions"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.agent.embedding.service as embedding_service  # noqa: E402
import app.services.forbidden_source as forbidden_source  # noqa: E402
import app.services.knowledge_admin as knowledge_admin  # noqa: E402
import app.services.rag as rag_service  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from pydantic import Field  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.agent.checkpoint import close_checkpointer, init_checkpointer  # noqa: E402
from app.agent.content.forbidden_words import (  # noqa: E402
    FORBIDDEN_WORDS,
    build_word_index,
    scan_note,
)
from app.agent.content.graph import build_content_graph  # noqa: E402
from app.agent.embedding.base import EmbeddingNotAvailableError  # noqa: E402
from app.agent.embedding.service import (  # noqa: E402
    describe_embedding_blocker,
    describe_embedding_readiness,
    reset_embedder,
)
from app.core.config import settings  # noqa: E402
from app.core.timeutil import now_local  # noqa: E402
from app.crud.knowledge import crud_knowledge  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.models.enums import (  # noqa: E402
    ChunkType,
    PersonaStatus,
    RefType,
    SnippetStatus,
    TopicDifficulty,
    TopicStatus,
)
from app.models.persona import Persona  # noqa: E402
from app.models.topic import Topic  # noqa: E402
from app.services.doc_splitter import (  # noqa: E402
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    describe_splitter,
    split_text,
)
from app.services.forbidden_source import (  # noqa: E402
    scan_with_knowledge,
    seed_builtin_words,
    word_hash,
)
from app.services.rag import RAG_BLOCK_TITLE, safe_rag_block  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
PREFIX = "[SELFTEST]知识管理"

#: 禁止出现的字样（小红书域名 / 登录态凭据 / 密钥前缀）
FORBIDDEN_PATTERNS = (
    "xiaohongshu",
    "xhslink",
    "xhs.com",
    "redbook.com",
    "Cookie",
    "cookie",
    "access_token",
    "refresh_token",
    "session_id",
    "sk-",
)

#: 业务表（public schema，不含 alembic_version）期望张数
EXPECTED_TABLES = 10

#: 业务表（第 17 步口径的八张，用于 diff 断言）
BUSINESS_TABLES_8 = (
    "accounts",
    "personas",
    "notes",
    "topics",
    "note_metrics",
    "diagnostics",
    "covers",
    "hotspots",
)

#: 假密钥串（运行时拼接，源码内不出现连续字样，避免合规扫描误报）
FAKE_SECRET = "s" + "k-" + "test-1234567890abcdef"

#: 自检专用自定义违禁词（仅存在于自检写入的知识库；内置词表中不存在）
CUSTOM_WORD = "自检违禁词甲乙"
CUSTOM_WORD_B = "自检违禁词乙丙"

#: 仅存在于**内置**词表的词（用于回退路径断言；内置表含「稳赚」与「稳赚不赔」）
BUILTIN_WORD = "稳赚不赔"

#: 文件导入「部分失败」的注入标记（只出现在一块里）
MARK_FAIL = "块失败标记Z9"

#: 自检新增/修改的文件（合规扫描用；全部为本步交付面）
SCANNED_BACKEND_FILES = (
    BACKEND_DIR / "app" / "models" / "enums.py",
    BACKEND_DIR / "app" / "agent" / "content" / "forbidden_words.py",
    BACKEND_DIR / "app" / "agent" / "content" / "graph.py",
    BACKEND_DIR / "app" / "agent" / "topics" / "graph.py",
    BACKEND_DIR / "app" / "agent" / "embedding" / "service.py",
    BACKEND_DIR / "app" / "crud" / "knowledge.py",
    BACKEND_DIR / "app" / "services" / "forbidden_source.py",
    BACKEND_DIR / "app" / "services" / "doc_splitter.py",
    BACKEND_DIR / "app" / "services" / "knowledge_admin.py",
    BACKEND_DIR / "app" / "services" / "rag.py",
    BACKEND_DIR / "app" / "schemas" / "knowledge.py",
    BACKEND_DIR / "app" / "api" / "routes" / "knowledge.py",
)

SCANNED_SCRIPT_FILES = (
    ROOT_DIR / "scripts" / "seed_forbidden_words.py",
    ROOT_DIR / "scripts" / "selftest_knowledge_admin.py",
)

SCANNED_FRONTEND_FILES = (
    FRONTEND_SRC / "lib" / "types.ts",
    FRONTEND_SRC / "app" / "knowledge" / "page.tsx",
    FRONTEND_SRC / "app" / "api" / "v1" / "knowledge" / "manual" / "route.ts",
    FRONTEND_SRC / "app" / "api" / "v1" / "knowledge" / "seed-forbidden-words" / "route.ts",
    FRONTEND_SRC / "app" / "api" / "v1" / "knowledge" / "import-text" / "route.ts",
    FRONTEND_SRC / "app" / "api" / "v1" / "knowledge" / "import-file" / "route.ts",
    FRONTEND_SRC / "app" / "knowledge" / "knowledge-admin.tsx",
)


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0
        self.skipped: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"[PASS] {label}")
        else:
            self.failures.append(label)
            print(f"[FAIL] {label}" + (f"  ->  {detail}" if detail else ""))

    def skip(self, label: str) -> None:
        self.skipped.append(label)
        print(f"[SKIP] {label}")


# ---------------------------------------------------------------------------
# 假 embedder（确定性：同文本 → 同向量；不发请求、不加载模型）
# ---------------------------------------------------------------------------
class FakeEmbedder:
    """确定性假向量化器：sha256(text) 决定随机种子 → 同文本恒同向量。"""

    def __init__(self, *, dim: int = 1024) -> None:
        self.dim = dim
        self.calls: list[int] = []
        self.batches: list[list[str]] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        self.batches.append(list(texts))
        return [self._vec(text) for text in texts]

    def _vec(self, text: str) -> list[float]:
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        rng = random.Random(seed)
        return [rng.uniform(-1.0, 1.0) for _ in range(self.dim)]


class ThrowingEmbedder:
    """恒抛「依赖未安装」错误的 embedder（验证向量化失败路径）。"""

    dim = 1024

    def __init__(self) -> None:
        self.calls: list[int] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        raise EmbeddingNotAvailableError("本地向量化依赖未安装（自检注入）。")


class SelectiveThrowEmbedder(FakeEmbedder):
    """对**含指定标记**的文本抛错（验证「部分块失败、其它块照常」）。"""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        self.batches.append(list(texts))
        for item in texts:
            if MARK_FAIL in item:
                raise EmbeddingNotAvailableError("自检注入：含失败标记的块抛错")
        return [self._vec(text) for text in texts]


# ---------------------------------------------------------------------------
# 假模型（照 selftest_rag 的确定性脚本模型）
# ---------------------------------------------------------------------------
class AdminScriptModel(BaseChatModel):
    """确定性假模型：按调用次序取响应；末项会被重复使用。"""

    responses: list[Any] = Field(default_factory=list)
    calls: int = 0
    prompts: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "admin-script"

    @staticmethod
    def _text_of(messages: list[Any]) -> str:
        parts: list[str] = []
        for message in messages:
            content = getattr(message, "content", "")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        parts.append(block["text"])
        return "\n".join(parts)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # noqa: ANN001
        self.prompts.append(self._text_of(messages))
        sequence: list[Any] = list(self.responses)
        index = min(self.calls, len(sequence) - 1) if sequence else -1
        item = sequence[index] if sequence else "{}"
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        content = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])


# ---------------------------------------------------------------------------
# 注入上下文管理器（退出时恢复原样；自检不污染真实环境）
# ---------------------------------------------------------------------------
class _AdminEnv:
    """组合 patch：模拟「本地依赖已装 + 模型就绪」的运行环境。

    - ``knowledge_admin.describe_embedding_blocker`` → ``None``（闸门放行；
      **只 mock 探测结果**，闸门逻辑本身照常运行）；
    - ``embedding_service.get_embedder`` → 注入的假 embedder
      （``embed_texts(embedder=None)`` 路径由此命中假实现）。
    """

    def __init__(self, embedder: Any | None = None, *, gate_open: bool = True) -> None:
        self.embedder = embedder
        self.gate_open = gate_open
        self._orig_blocker: Any = None
        self._orig_get: Any = None

    def __enter__(self) -> "_AdminEnv":
        self._orig_blocker = knowledge_admin.describe_embedding_blocker
        self._orig_get = embedding_service.get_embedder
        if self.gate_open:
            knowledge_admin.describe_embedding_blocker = lambda: None
        if self.embedder is not None:
            embedding_service.get_embedder = lambda: self.embedder
        reset_embedder()  # 清进程内向量缓存，避免跨用例命中
        return self

    def __exit__(self, *exc_info: Any) -> None:
        knowledge_admin.describe_embedding_blocker = self._orig_blocker
        embedding_service.get_embedder = self._orig_get
        reset_embedder()


class _NoPersonaLoader:
    """把 ``rag._load_persona_snippets`` 替换为恒抛错（验证人设知识失败降级）。"""

    def __enter__(self) -> None:
        self._original = rag_service._load_persona_snippets  # noqa: SLF001

        def _raise(db: Any, *, persona_id: str) -> Any:  # noqa: ARG001
            raise RuntimeError("自检注入：人设知识读取失败")

        rag_service._load_persona_snippets = _raise  # noqa: SLF001

    def __exit__(self, *exc_info: Any) -> None:
        rag_service._load_persona_snippets = self._original  # noqa: SLF001


class _NoWordlistLoader:
    """把 ``forbidden_source.load_active_wordlist`` 替换为恒抛错（验证读取异常回退）。"""

    def __enter__(self) -> None:
        self._original = forbidden_source.load_active_wordlist

        def _raise(db: Any) -> Any:  # noqa: ARG001
            raise RuntimeError("自检注入：违禁词读取失败")

        forbidden_source.load_active_wordlist = _raise

    def __exit__(self, *exc_info: Any) -> None:
        forbidden_source.load_active_wordlist = self._original


class _InjectedWordlist:
    """把词表加载替换为受控列表（空表 / 仅自定义词），与环境里的库内容解耦。"""

    def __init__(self, words: list[dict[str, str]]) -> None:
        self.words = [dict(item) for item in words]

    def __enter__(self) -> "_InjectedWordlist":
        self._original = forbidden_source.load_active_wordlist
        words = [dict(item) for item in self.words]

        def _load(db: Any) -> list[dict[str, str]]:  # noqa: ARG001
            return [dict(item) for item in words]

        forbidden_source.load_active_wordlist = _load
        return self

    def __exit__(self, *exc_info: Any) -> None:
        forbidden_source.load_active_wordlist = self._original


class _EmbeddingDepsMissing:
    """把 ``embedding.service`` 实际读取的依赖探测临时置 False（验证 400 闸门）。

    注入点必须是 ``app.agent.embedding.service.local_deps_installed``：
    ``describe_embedding_blocker`` / ``embed_texts`` 都在该模块内按模块全局名查找，
    patch ``embedding.local`` 里的原函数不会影响已 import 的引用。
    """

    def __enter__(self) -> "_EmbeddingDepsMissing":
        self._original = embedding_service.local_deps_installed
        embedding_service.local_deps_installed = lambda: False
        reset_embedder()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        embedding_service.local_deps_installed = self._original
        reset_embedder()


# ---------------------------------------------------------------------------
# 低层查询助手
# ---------------------------------------------------------------------------
def _snippet_ids_now() -> set[str]:
    with SessionLocal() as db:
        rows = db.execute(text("SELECT id FROM knowledge_snippets")).all()
    return {str(row[0]) for row in rows}


def _note_ids_now() -> set[str]:
    with SessionLocal() as db:
        rows = db.execute(text("SELECT id FROM notes")).all()
    return {str(row[0]) for row in rows}


def _run_ids_now() -> set[str]:
    with SessionLocal() as db:
        rows = db.execute(text("SELECT id FROM agent_runs")).all()
    return {str(row[0]) for row in rows}


def _threads_now() -> set[str]:
    with SessionLocal() as db:
        try:
            rows = db.execute(text("SELECT DISTINCT thread_id FROM langgraph.checkpoints")).all()
        except Exception:  # noqa: BLE001 —— 表可能尚未建
            return set()
    return {str(row[0]) for row in rows}


def _count_rows(sql: str, params: dict[str, Any] | None = None) -> int:
    with SessionLocal() as db:
        return int(db.execute(text(sql), params or {}).scalar_one())


def _active_forbidden_count() -> int:
    with SessionLocal() as db:
        rows = crud_knowledge.list_active_snippets(
            db, chunk_type=ChunkType.FORBIDDEN_WORD, limit=500
        )
    return len(rows)


def _active_forbidden_hashes() -> set[str]:
    with SessionLocal() as db:
        rows = crud_knowledge.list_active_snippets(
            db, chunk_type=ChunkType.FORBIDDEN_WORD, limit=500
        )
    out: set[str] = set()
    for row in rows:
        meta = row.meta if isinstance(row.meta, dict) else {}
        value = str(meta.get("content_hash") or "")
        if value:
            out.add(value)
    return out


def _snippet_row(snippet_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT chunk_type, ref_type, title, content, "
                "(embedding IS NOT NULL) AS has_vector, "
                "CASE WHEN embedding IS NULL THEN NULL ELSE vector_dims(embedding) END AS vec_dim, "
                "meta, status FROM knowledge_snippets WHERE id = :sid"
            ),
            {"sid": snippet_id},
        ).all()
    if not rows:
        return {}
    row = rows[0]
    return {
        "chunk_type": row[0],
        "ref_type": row[1],
        "title": row[2],
        "content": row[3],
        "has_vector": row[4],
        "vec_dim": row[5],
        "meta": row[6] if isinstance(row[6], dict) else {},
        "status": row[7],
    }


def _embedding_text(snippet_id: str) -> str | None:
    with SessionLocal() as db:
        row = db.execute(
            text("SELECT embedding::text FROM knowledge_snippets WHERE id = :sid"),
            {"sid": snippet_id},
        ).first()
    return None if row is None or row[0] is None else str(row[0])


def _fetch_note(note_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT title, body, content_check, compliance_status, status "
                "FROM notes WHERE id = :nid"
            ),
            {"nid": note_id},
        ).all()
    if not rows:
        return {}
    row = rows[0]
    return {
        "title": row[0],
        "body": row[1],
        "content_check": row[2] if isinstance(row[2], dict) else {},
        "compliance_status": row[3],
        "status": row[4],
    }


def _draft_body(anchor: str, repeat: int = 30) -> str:
    """构造 200~1000 字的合规正文（含指定锚词）。"""
    filler = "通勤穿搭分享，简单实用最重要。"
    return f"开场：{anchor}。中间：{filler * repeat}结尾：欢迎留言交流。"


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------
class Fixture:
    def __init__(self) -> None:
        self.account_id: uuid.UUID | None = None
        self.persona_id: uuid.UUID | None = None
        self.persona_b_id: uuid.UUID | None = None
        self.topic_ids: list[uuid.UUID] = []
        self.recorded_snippet_ids: list[uuid.UUID] = []
        self.recorded_note_ids: list[uuid.UUID] = []
        self.before_threads: set[str] = set()
        self.before_run_ids: set[str] = set()
        self.before_snippet_ids: set[str] = set()
        self.before_note_ids: set[str] = set()
        self.initial_forbidden_count = 0
        self.initial_forbidden_hashes: set[str] = set()
        self.initial_not_tracked = True
        self.tag = uuid.uuid4().hex[:8]

    # ------------------------------------------------------------- 工具
    def record_snippet(self, snippet_id: Any) -> str:
        value = uuid.UUID(str(snippet_id))
        if value not in self.recorded_snippet_ids:
            self.recorded_snippet_ids.append(value)
        return str(value)

    def record_note(self, note_id: Any) -> str:
        value = uuid.UUID(str(note_id))
        if value not in self.recorded_note_ids:
            self.recorded_note_ids.append(value)
        return str(value)

    def topic(self, index: int) -> uuid.UUID:
        return self.topic_ids[index]

    # ------------------------------------------------------------- 建立
    def setup(self) -> None:
        self.before_threads = _threads_now()
        self.before_run_ids = _run_ids_now()
        self.before_note_ids = _note_ids_now()
        # 前置清扫：清掉本前缀的异常遗留（自我保护；绝不碰其他数据）
        with SessionLocal() as db:
            for table, column in (
                ("knowledge_snippets", "title"),
                ("knowledge_snippets", "content"),
                ("notes", "title"),
                ("topics", "title"),
                ("personas", "name"),
                ("accounts", "name"),
            ):
                db.execute(
                    text(f"DELETE FROM {table} WHERE {column} LIKE :p"),
                    {"p": f"%{PREFIX}%"},
                )
            db.execute(
                text("DELETE FROM knowledge_snippets WHERE meta::text LIKE :p"),
                {"p": f"%{PREFIX}%"},
            )
            db.commit()
        self.before_snippet_ids = _snippet_ids_now()
        self.initial_forbidden_count = _active_forbidden_count()
        self.initial_forbidden_hashes = _active_forbidden_hashes()

        now = now_local()
        with SessionLocal() as db:
            account = Account(name=f"{PREFIX}账号", status="active", created_at=now, updated_at=now)
            db.add(account)
            db.flush()
            persona = Persona(
                account_id=account.id,
                name=f"{PREFIX}人设A",
                positioning="通勤穿搭分享",
                target_audience="上班族",
                tone="亲和口语",
                content_pillars=["穿搭", "效率"],
                taboo=["不吹功效"],
                status=PersonaStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
            persona_b = Persona(
                account_id=account.id,
                name=f"{PREFIX}人设B",
                positioning="美食探店",
                target_audience="吃货",
                tone="活泼",
                content_pillars=["美食"],
                status=PersonaStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
            db.add(persona)
            db.add(persona_b)
            db.flush()
            self.account_id = account.id
            self.persona_id = persona.id
            self.persona_b_id = persona_b.id
            for index in range(2):
                topic = Topic(
                    account_id=account.id,
                    persona_id=persona.id,
                    title=f"{PREFIX}创作选题{index}",
                    angle="从日常痛点切入",
                    summary="写给上班族的通勤穿搭",
                    pillar="穿搭",
                    content_outline=["开场", "方法", "示例"],
                    target_keywords=[f"关键词{index}"],
                    difficulty=TopicDifficulty.EASY,
                    status=TopicStatus.APPROVED,
                    created_at=now,
                    updated_at=now,
                )
                db.add(topic)
                db.flush()
                self.topic_ids.append(topic.id)
            db.commit()

    # ------------------------------------------------------------- 清理
    def cleanup(self) -> None:
        like = f"%{PREFIX}%"
        with SessionLocal() as db:
            if self.recorded_snippet_ids:
                db.execute(
                    text(
                        "DELETE FROM knowledge_snippets WHERE id = ANY(:ids) "
                        "OR title LIKE :p OR content LIKE :p OR meta::text LIKE :p"
                    ),
                    {"ids": [str(item) for item in self.recorded_snippet_ids], "p": like},
                )
            else:
                db.execute(
                    text(
                        "DELETE FROM knowledge_snippets WHERE title LIKE :p "
                        "OR content LIKE :p OR meta::text LIKE :p"
                    ),
                    {"p": like},
                )
            if self.recorded_note_ids:
                db.execute(
                    text("DELETE FROM notes WHERE id = ANY(:ids) OR title LIKE :p"),
                    {"ids": [str(item) for item in self.recorded_note_ids], "p": like},
                )
            else:
                db.execute(
                    text("DELETE FROM notes WHERE title LIKE :p"), {"p": like}
                )
            if self.topic_ids:
                db.execute(
                    text("DELETE FROM topics WHERE id = ANY(:tids) OR title LIKE :p"),
                    {"tids": [str(item) for item in self.topic_ids], "p": like},
                )
            if self.persona_id is not None and self.persona_b_id is not None:
                db.execute(
                    text("DELETE FROM personas WHERE id IN (:pa, :pb) OR name LIKE :p"),
                    {"pa": str(self.persona_id), "pb": str(self.persona_b_id), "p": like},
                )
            if self.account_id is not None:
                db.execute(
                    text("DELETE FROM accounts WHERE id = :aid OR name LIKE :p"),
                    {"aid": str(self.account_id), "p": like},
                )
            db.commit()

    # ------------------------------------------------------------- 复查
    def verify_residue(self) -> dict[str, int]:
        like = f"%{PREFIX}%"
        ids = [str(item) for item in self.recorded_snippet_ids]
        note_ids = [str(item) for item in self.recorded_note_ids]
        new_runs = _run_ids_now() - self.before_run_ids
        new_threads = _threads_now() - self.before_threads
        return {
            "snippets_by_id": (
                _count_rows(
                    "SELECT count(*) FROM knowledge_snippets WHERE id = ANY(:ids)",
                    {"ids": ids},
                )
                if ids
                else 0
            ),
            "snippets_by_prefix": _count_rows(
                "SELECT count(*) FROM knowledge_snippets WHERE title LIKE :p "
                "OR content LIKE :p OR meta::text LIKE :p",
                {"p": like},
            ),
            "snippets_choice_delta": max(0, len(_snippet_ids_now()) - len(self.before_snippet_ids)),
            "notes_by_id": (
                _count_rows(
                    "SELECT count(*) FROM notes WHERE id = ANY(:ids)", {"ids": note_ids}
                )
                if note_ids
                else 0
            ),
            "notes_by_prefix": _count_rows(
                "SELECT count(*) FROM notes WHERE title LIKE :p", {"p": like}
            ),
            "note_delta": max(0, len(_note_ids_now()) - len(self.before_note_ids)),
            "topics_by_id": _count_rows(
                "SELECT count(*) FROM topics WHERE id = ANY(:ids)",
                {"ids": [str(item) for item in self.topic_ids]},
            ),
            "topics_by_prefix": _count_rows(
                "SELECT count(*) FROM topics WHERE title LIKE :p", {"p": like}
            ),
            "personas": _count_rows(
                "SELECT count(*) FROM personas WHERE name LIKE :p", {"p": like}
            ),
            "accounts": _count_rows(
                "SELECT count(*) FROM accounts WHERE name LIKE :p", {"p": like}
            ),
            "agent_runs_delta": len(new_runs),
            "threads_delta": len(new_threads),
        }


# ---------------------------------------------------------------------------
# 组 0：契约与结构（词表增强 / 切分器 / 闸门探测）
# ---------------------------------------------------------------------------
def group0_contracts(c: Checker) -> None:
    c.check(
        RefType.PERSONA.value == "persona",
        "RefType 新增 PERSONA=persona（native_enum=False 落 VARCHAR，无 DDL）",
        f"{[item.value for item in RefType]}",
    )
    # 内置词表规模（任务书：约 73 词 / 7 类别；只设下限防止未来扩充被误判）
    c.check(
        len(FORBIDDEN_WORDS) >= 70,
        "内置词表规模 ≥ 70（当前 %d）" % len(FORBIDDEN_WORDS),
        f"{len(FORBIDDEN_WORDS)}",
    )

    # build_word_index：归一化 + 首次出现优先 + 空词跳过
    index = build_word_index([("加-微-信", "cat_a"), ("加微信", "cat_b"), ("  ", "x")])
    c.check(
        index == {"加微信": "cat_a"},
        "build_word_index：归一化去标点 + 首次出现优先 + 空词跳过",
        f"{index}",
    )

    # 缺省（无 wordlist）行为与第 12 步一致：内置词命中
    defaults = scan_note(body="这个是稳赚不赔的买卖")
    c.check(
        any(item["word"] == BUILTIN_WORD for item in defaults)
        and all(item["field"] == "body" for item in defaults),
        "scan_note 缺省（无 wordlist）沿用内置词表（第 12 步行为不变）",
        f"{defaults}",
    )
    # 自定义词表：只认传入词表（内置词不再命中）
    custom_only = scan_note(body="这个是稳赚不赔的买卖", wordlist=[(CUSTOM_WORD, "cat_x")])
    c.check(
        custom_only == [],
        "scan_note(wordlist=…) 只认传入词表（内置词不命中，证明词表被替换）",
        f"{custom_only}",
    )
    custom_hit = scan_note(body=f"这里有{CUSTOM_WORD}出现", wordlist=[(CUSTOM_WORD, "cat_x")])
    c.check(
        len(custom_hit) == 1 and custom_hit[0]["category"] == "cat_x",
        "scan_note(wordlist=…) 命中自定义词且类别来自词表",
        f"{custom_hit}",
    )

    # 切分器：就绪快照 + 基本切分语义（不依赖官方包是否安装）
    snap = describe_splitter()
    c.check(
        snap["official_available"] in (True, False)
        and isinstance(snap["impl"], str)
        and snap["default_chunk_size"] == DEFAULT_CHUNK_SIZE
        and snap["default_chunk_overlap"] == DEFAULT_CHUNK_OVERLAP,
        "describe_splitter：官方优先 / 零依赖兜底二选一就绪",
        f"{snap['impl']}",
    )
    long_text = "第一段内容测试，这里有足够长的文字用来验证切分。" * 30
    parts = split_text(long_text, chunk_size=250, chunk_overlap=50)
    c.check(
        len(parts) >= 3 and all(len(part) <= 250 for part in parts) and all(part.strip() for part in parts),
        "split_text：多块 / 单块 ≤ chunk_size / 无空块",
        f"parts={len(parts)} max={max(len(p) for p in parts)}",
    )
    c.check(
        any(parts[i][-40:] in parts[i + 1] for i in range(len(parts) - 1)),
        "split_text：相邻块保留 overlap（语义边界不丢）",
    )
    one = split_text("短文本。", chunk_size=250, chunk_overlap=50)
    c.check(one == ["短文本。"], "split_text：短文本单块返回", f"{one}")
    try:
        split_text("x" * 300, chunk_size=200, chunk_overlap=200)
        bad_overlap = False
    except ValueError:
        bad_overlap = True
    c.check(bad_overlap, "split_text：overlap ≥ size 时 ValueError（边界校验）")

    # embedding 闸门探测（新增函数）：就绪 12 键不变 + blocker 语义
    readiness = describe_embedding_readiness()
    c.check(
        len(readiness) == 12 and isinstance(readiness["local_deps_installed"], bool),
        "describe_embedding_readiness 仍为恰 12 键（第 18 步口径不变）",
        f"{sorted(readiness.keys())}",
    )
    blocker = describe_embedding_blocker()
    c.check(
        blocker is None or (isinstance(blocker, dict) and "provider" in blocker),
        "describe_embedding_blocker：None=就绪 / dict=含 provider 的未就绪原因",
        f"{blocker}",
    )


# ---------------------------------------------------------------------------
# 组 1：违禁词三路径（空库回退 / 自定义命中 + 内置隔离 / 读取异常回退）
# ---------------------------------------------------------------------------
def group1_word_source(c: Checker, client: TestClient, fx: Fixture) -> None:
    # ---- (a) 空库回退内置（用受控空词表，不依赖业务库当前内容）----
    with _InjectedWordlist([]):
        hits = scan_with_knowledge(body="这个是稳赚不赔的买卖")
    c.check(
        any(item["word"] == BUILTIN_WORD for item in hits),
        "知识库为空 → 回退内置词表（注入空表，命中内置词「稳赚不赔」）",
        f"{hits}",
    )

    # ---- (b) 插入自定义词（HTTP /manual，forbidden_word 无需向量）----
    resp = client.post(
        "/api/v1/knowledge/manual",
        json={
            "chunk_type": "forbidden_word",
            "content": CUSTOM_WORD,
            "category": PREFIX,
            "title": f"{PREFIX}-词A",
        },
    )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    c.check(resp.status_code == 201, "POST /manual 创建违禁词 → 201", f"{resp.status_code} {resp.text[:200]}")
    if resp.status_code == 201:
        fx.record_snippet(body.get("id"))

    hits_custom = scan_with_knowledge(body=f"这句话里有{CUSTOM_WORD}出现")
    c.check(
        any(item["word"] == CUSTOM_WORD and item["category"] == PREFIX for item in hits_custom),
        "知识库有自定义词表 → 命中自定义词且类别取自知识库",
        f"{hits_custom}",
    )

    # 内置隔离：受控传入「仅自定义词」的非空词表 → 不再回退内置
    with _InjectedWordlist([{"word": CUSTOM_WORD, "category": PREFIX}]):
        hits_builtin = scan_with_knowledge(body="这个是稳赚不赔的买卖")
    c.check(
        not any("稳赚" in item["word"] for item in hits_builtin),
        "库非空（仅自定义词表）→ 内置词不命中（注入非空词表，证明未回退内置）",
        f"{hits_builtin}",
    )

    # ---- (c) 读取异常回退内置 ----
    with _NoWordlistLoader():
        hits_exc = scan_with_knowledge(body="这个是稳赚不赔的买卖")
    c.check(
        any(item["word"] == BUILTIN_WORD for item in hits_exc),
        "知识库读取异常 → 回退内置词表（保守兜底，不跳过检查）",
        f"{hits_exc}",
    )

    # ---- 清理自定义词（恢复到自检开始时的库状态，供后续 seed 幂等测试）----
    with SessionLocal() as db:
        db.execute(
            text("DELETE FROM knowledge_snippets WHERE content = :word"),
            {"word": CUSTOM_WORD},
        )
        db.commit()
    c.check(
        _active_forbidden_count() == fx.initial_forbidden_count,
        "自定义词清理完成：active 违禁词数回到自检开始时",
        f"{_active_forbidden_count()} vs {fx.initial_forbidden_count}",
    )


# ---------------------------------------------------------------------------
# 组 2：seed 幂等（服务层 + HTTP dry_run）
# ---------------------------------------------------------------------------
def group2_seed(c: Checker, client: TestClient, fx: Fixture) -> None:
    builtin_total = len(FORBIDDEN_WORDS)
    builtin_hashes = {word_hash(item.word) for item in FORBIDDEN_WORDS}
    already = builtin_hashes & fx.initial_forbidden_hashes
    expected_missing = builtin_total - len(already)
    before_ids = _snippet_ids_now()

    # ---- dry_run：零写入 ----
    with SessionLocal() as db:
        dry = seed_builtin_words(db, dry_run=True)
    c.check(
        dry["dry_run"] is True and dry["inserted"] == 0 and dry["would_insert"] == expected_missing,
        "seed dry_run：inserted=0 且 would_insert = 缺失数（零写入）",
        f"{dry} expected_missing={expected_missing}",
    )
    c.check(
        _snippet_ids_now() == before_ids,
        "seed dry_run 后库内容量不变（零写入）",
    )

    # ---- 首次真实写入 ----
    with SessionLocal() as db:
        first = seed_builtin_words(db)
    c.check(
        first["inserted"] == expected_missing and first["reused"] == len(already),
        "seed 首次：inserted=缺失数、reused=已存在数",
        f"{first}",
    )
    new_ids = _snippet_ids_now() - before_ids
    c.check(
        len(new_ids) == expected_missing,
        "seed 首次写入条数与 inserted 一致（按库差集复核）",
        f"{len(new_ids)} vs {expected_missing}",
    )

    # ---- 二次运行：幂等（新增 0 / reused=全部）----
    with SessionLocal() as db:
        second = seed_builtin_words(db)
    c.check(
        second["inserted"] == 0 and second["reused"] == builtin_total,
        "seed 二次：inserted=0、reused=内置词总数（幂等跳过）",
        f"{second}",
    )

    # ---- HTTP 端点（dry_run=true；此时库内已全量）----
    resp = client.post("/api/v1/knowledge/seed-forbidden-words?dry_run=true")
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    c.check(
        resp.status_code == 200 and body.get("would_insert") == 0 and body.get("reused") == builtin_total,
        "POST /seed-forbidden-words?dry_run=true → 200 且 would_insert=0（经接口复核幂等）",
        f"{resp.status_code} {resp.text[:200]}",
    )

    # ---- forbidden_word 允许 embedding 为空（本步核心口径）----
    c.check(
        _count_rows(
            "SELECT count(*) FROM knowledge_snippets WHERE chunk_type = 'forbidden_word' "
            "AND status = 'active' AND embedding IS NOT NULL AND meta::text LIKE :p",
            {"p": f"%{PREFIX}%"},
        )
        == 0,
        "forbidden_word 允许 embedding 为空（seed 不强制向量模型）",
    )
    c.check(
        _count_rows(
            "SELECT count(*) FROM knowledge_snippets WHERE chunk_type = 'forbidden_word' AND status = 'active'"
        )
        >= builtin_total,
        "seed 后库内 active 违禁词 ≥ 内置词总数",
    )

    # ---- 清理：删除本次 seed 新增的行（按 id 差集，绝不碰自检前已有数据）----
    if new_ids:
        with SessionLocal() as db:
            db.execute(
                text("DELETE FROM knowledge_snippets WHERE id = ANY(:ids)"),
                {"ids": sorted(new_ids)},
            )
            db.commit()
    c.check(
        _active_forbidden_count() == fx.initial_forbidden_count,
        "seed 测试清理完成：active 违禁词数回到自检开始时",
        f"{_active_forbidden_count()} vs {fx.initial_forbidden_count}",
    )


# ---------------------------------------------------------------------------
# 组 3：手动创建（HTTP /manual）
# ---------------------------------------------------------------------------
def group3_manual_create(c: Checker, client: TestClient, fx: Fixture) -> None:
    fake = FakeEmbedder()

    # ---- 未装依赖的真实闸门：general 400 / forbidden_word 201（注入依赖缺失）----
    with _EmbeddingDepsMissing():
        resp400 = client.post(
            "/api/v1/knowledge/manual",
            json={"chunk_type": "general", "content": f"{PREFIX}-未就绪测试"},
        )
        body400 = resp400.json() if resp400.headers.get("content-type", "").startswith("application/json") else {}
    c.check(
        resp400.status_code == 400
        and "未就绪" in str(body400.get("message") or "")
        and isinstance(body400.get("details"), dict)
        and body400.get("details", {}).get("provider") == "local",
        "未装向量化依赖：POST /manual(general) → 400 + 就绪指引（零副作用）",
        f"{resp400.status_code} {resp400.text[:220]}",
    )

    resp_off = client.post(
        "/api/v1/knowledge/manual",
        json={
            "chunk_type": "forbidden_word",
            "content": CUSTOM_WORD_B,
            "category": PREFIX,
            "title": f"{PREFIX}-词B（离线可建）",
        },
    )
    body_off = resp_off.json() if resp_off.headers.get("content-type", "").startswith("application/json") else {}
    c.check(
        resp_off.status_code == 201,
        "forbidden_word 离线可建（无向量依赖也成功）",
        f"{resp_off.status_code} {resp_off.text[:200]}",
    )
    if resp_off.status_code == 201:
        sid = fx.record_snippet(body_off.get("id"))
        row = _snippet_row(sid)
        c.check(
            row.get("has_vector") is False and row.get("meta", {}).get("category") == PREFIX,
            "forbidden_word 行：embedding 为空 + meta.category 就位",
            f"{row}",
        )

    # ---- 创建 general（假 embedder）：向量入库 ----
    with _AdminEnv(fake):
        resp_g = client.post(
            "/api/v1/knowledge/manual",
            json={
                "chunk_type": "general",
                "title": f"{PREFIX}-通用块",
                "content": f"{PREFIX} 通用知识内容，用于自检。{fx.tag}",
            },
        )
        g_body = resp_g.json() if resp_g.headers.get("content-type", "").startswith("application/json") else {}
        if resp_g.status_code == 201:
            g_id = fx.record_snippet(g_body.get("id"))
        else:
            g_id = ""
    c.check(resp_g.status_code == 201, "POST /manual(general) → 201", f"{resp_g.status_code} {resp_g.text[:200]}")
    if g_id:
        row_g = _snippet_row(g_id)
        c.check(
            row_g.get("has_vector") is True and row_g.get("vec_dim") == 1024,
            "general 行：向量已写入且维度=1024",
            f"has_vector={row_g.get('has_vector')} dim={row_g.get('vec_dim')}",
        )
        c.check(
            isinstance(row_g.get("meta", {}).get("content_hash"), str)
            and len(row_g["meta"]["content_hash"]) == 64,
            "general 行：meta.content_hash 已写入（sha256 64 位）",
            f"{row_g.get('meta')}",
        )
        c.check(
            fake.calls and fake.calls[-1] == 1,
            "general 创建走 embed_texts（假 embedder 记录到单条批）",
            f"{fake.calls}",
        )
        c.check(
            "embedding" not in json.dumps(g_body) and "llm_raw" not in json.dumps(g_body),
            "创建响应不含 embedding / llm_raw 字样",
        )

    # ---- 创建 persona_knowledge + 人设关联 ----
    with _AdminEnv(fake):
        resp_p = client.post(
            "/api/v1/knowledge/manual",
            json={
                "chunk_type": "persona_knowledge",
                "title": f"{PREFIX}-人设话术",
                "content": "话术：用通勤场景开场，先讲痛点再给方案。",
                "ref_type": "persona",
                "ref_id": str(fx.persona_id),
            },
        )
        p_body = resp_p.json() if resp_p.headers.get("content-type", "").startswith("application/json") else {}
        if resp_p.status_code == 201:
            p_id = fx.record_snippet(p_body.get("id"))
        else:
            p_id = ""
    c.check(
        resp_p.status_code == 201 and p_body.get("ref_type") == "persona",
        "POST /manual(persona_knowledge + ref_type=persona) → 201 且回显 ref_type",
        f"{resp_p.status_code} {resp_p.text[:200]}",
    )
    if p_id:
        row_p = _snippet_row(p_id)
        c.check(
            row_p.get("ref_type") == "persona" and row_p.get("chunk_type") == "persona_knowledge",
            "persona_knowledge 行：ref_type=persona 落库",
            f"{row_p}",
        )

    # ---- 人设引用校验：不存在 → 400；缺 ref_id → 400 ----
    resp_missing = client.post(
        "/api/v1/knowledge/manual",
        json={
            "chunk_type": "persona_knowledge",
            "content": f"{PREFIX}-坏引用",
            "ref_type": "persona",
            "ref_id": str(uuid.uuid4()),
        },
    )
    c.check(
        resp_missing.status_code == 400,
        "关联人设不存在 → 400（不落库）",
        f"{resp_missing.status_code} {resp_missing.text[:200]}",
    )
    resp_noref = client.post(
        "/api/v1/knowledge/manual",
        json={"chunk_type": "persona_knowledge", "content": f"{PREFIX}-缺引用"},
    )
    c.check(
        resp_noref.status_code == 400,
        "chunk_type=persona_knowledge 未带 ref_id → 400",
        f"{resp_noref.status_code} {resp_noref.text[:200]}",
    )

    # ---- 向量化失败：400 且不落库 ----
    throwing = ThrowingEmbedder()
    marker = f"{PREFIX}-失败不落库-{fx.tag}"
    with _AdminEnv(throwing):
        resp_fail = client.post(
            "/api/v1/knowledge/manual",
            json={"chunk_type": "general", "content": marker},
        )
    c.check(
        resp_fail.status_code == 400,
        "创建 general 时向量化失败 → 400",
        f"{resp_fail.status_code} {resp_fail.text[:200]}",
    )
    c.check(
        _count_rows(
            "SELECT count(*) FROM knowledge_snippets WHERE content = :c", {"c": marker}
        )
        == 0,
        "向量化失败时**不落库**（无半成品）",
    )

    # ---- 422 参数校验 ----
    for payload, label in (
        ({"chunk_type": "general", "content": ""}, "content 为空"),
        ({"chunk_type": "unknown_type", "content": "x"}, "chunk_type 非法"),
        ({"chunk_type": "general", "content": "x", "ref_type": "bad_type"}, "ref_type 非法"),
        ({"chunk_type": "general", "content": "x", "title": "长" * 201}, "title 超长"),
    ):
        resp_bad = client.post("/api/v1/knowledge/manual", json=payload)
        c.check(
            resp_bad.status_code == 422,
            f"POST /manual 参数校验（{label}）→ 422",
            f"{resp_bad.status_code}",
        )

    # ---- 列表接口按类型过滤（前端违禁词视图口径）----
    resp_list = client.get(
        "/api/v1/knowledge?chunk_type=forbidden_word&status=active&limit=100"
    )
    if resp_list.status_code == 200:
        items = resp_list.json().get("items") or []
        hit = [item for item in items if item.get("id") == str(resp_off.json().get("id"))]
        c.check(
            len(hit) == 1 and (hit[0].get("meta") or {}).get("category") == PREFIX,
            "GET /knowledge?chunk_type=forbidden_word 列表带出 category（前端视图口径）",
            f"{items[:2]}",
        )
    else:
        c.check(False, "GET /knowledge?chunk_type=forbidden_word 列表", f"{resp_list.status_code}")


# ---------------------------------------------------------------------------
# 组 4：编辑（PATCH 扩展）
# ---------------------------------------------------------------------------
def group4_edit(c: Checker, client: TestClient, fx: Fixture) -> None:
    fake = FakeEmbedder()

    # ---- 新建 general 块作为编辑对象 ----
    with _AdminEnv(fake):
        resp = client.post(
            "/api/v1/knowledge/manual",
            json={
                "chunk_type": "general",
                "title": f"{PREFIX}-编辑对象",
                "content": f"{PREFIX} 编辑前正文 {fx.tag}",
            },
        )
    sid = fx.record_snippet(resp.json().get("id")) if resp.status_code == 201 else ""
    c.check(resp.status_code == 201 and bool(sid), "编辑测试前置：general 块创建成功", f"{resp.status_code}")
    if not sid:
        return
    old_vec = _embedding_text(sid)
    old_hash = _snippet_row(sid).get("meta", {}).get("content_hash")

    # ---- 编辑 title ----
    resp_t = client.patch(f"/api/v1/knowledge/{sid}", json={"title": f"{PREFIX}-编辑后标题"})
    c.check(
        resp_t.status_code == 200 and resp_t.json().get("title") == f"{PREFIX}-编辑后标题",
        "PATCH title → 200 且标题更新",
        f"{resp_t.status_code} {resp_t.text[:200]}",
    )

    # ---- 编辑 content（假 embedder）：content_hash 与向量均更新 ----
    new_content = f"{PREFIX} 编辑后正文，内容发生了变化 {fx.tag}"
    with _AdminEnv(fake):
        resp_c = client.patch(
            f"/api/v1/knowledge/{sid}",
            json={"content": new_content},
        )
    c.check(
        resp_c.status_code == 200 and resp_c.json().get("content") == new_content,
        "PATCH content → 200 且正文更新",
        f"{resp_c.status_code} {resp_c.text[:200]}",
    )
    row_after = _snippet_row(sid)
    new_vec = _embedding_text(sid)
    c.check(
        new_vec is not None and new_vec != old_vec,
        "content 变化 → 向量已重新生成（新旧向量不同）",
    )
    c.check(
        row_after.get("meta", {}).get("content_hash") != old_hash
        and len(str(row_after.get("meta", {}).get("content_hash"))) == 64,
        "content 变化 → meta.content_hash 更新",
        f"{old_hash} -> {row_after.get('meta', {}).get('content_hash')}",
    )

    # ---- 重向量化失败：置空向量 + warnings，正文保留 ----
    fail_content = f"{PREFIX} 重向量化失败正文 {fx.tag}"
    with _AdminEnv(ThrowingEmbedder()):
        resp_f = client.patch(f"/api/v1/knowledge/{sid}", json={"content": fail_content})
    f_body = resp_f.json() if resp_f.headers.get("content-type", "").startswith("application/json") else {}
    c.check(
        resp_f.status_code == 200 and resp_f.json().get("content") == fail_content,
        "重向量化失败 → 200 且**正文照常保存**（不阻断、不丢正文）",
        f"{resp_f.status_code} {resp_f.text[:240]}",
    )
    c.check(
        bool(f_body.get("warnings")) and any("向量" in str(w) for w in f_body.get("warnings", [])),
        "重向量化失败 → 响应 warnings 给出提示",
        f"{f_body.get('warnings')}",
    )
    c.check(
        _snippet_row(sid).get("has_vector") is False,
        "重向量化失败 → embedding 置空",
    )
    warnings_text = json.dumps(f_body.get("warnings") or [], ensure_ascii=False)
    c.check(
        "D:\\" not in warnings_text and "sk-" not in warnings_text and "/Users" not in warnings_text,
        "警告文本不含绝对路径 / 密钥前缀",
        f"{warnings_text[:160]}",
    )

    # ---- 仅改 status：与第 18 步行为一致（active → archived → active）----
    resp_a = client.patch(f"/api/v1/knowledge/{sid}", json={"status": "archived"})
    c.check(
        resp_a.status_code == 200 and resp_a.json().get("status") == "archived",
        "PATCH 仅 status=archived → 归档成功（第 18 步行为一致）",
        f"{resp_a.status_code}",
    )
    resp_r = client.patch(f"/api/v1/knowledge/{sid}", json={"status": "active"})
    c.check(
        resp_r.status_code == 200 and resp_r.json().get("status") == "active",
        "PATCH 仅 status=active → 恢复成功",
        f"{resp_r.status_code}",
    )

    # ---- forbidden_word 编辑：content_hash 更新、不触发向量化 ----
    resp_w = client.post(
        "/api/v1/knowledge/manual",
        json={"chunk_type": "forbidden_word", "content": f"{CUSTOM_WORD}-编辑前", "category": PREFIX},
    )
    wid = fx.record_snippet(resp_w.json().get("id")) if resp_w.status_code == 201 else ""
    if wid:
        w_fake = FakeEmbedder()
        old_w_hash = _snippet_row(wid).get("meta", {}).get("content_hash")
        with _AdminEnv(w_fake):
            resp_we = client.patch(
                f"/api/v1/knowledge/{wid}", json={"content": f"{CUSTOM_WORD}-编辑后"}
            )
        c.check(
            resp_we.status_code == 200 and w_fake.calls == [],
            "forbidden_word 编辑：不触发向量化（假 embedder 零调用）",
            f"calls={w_fake.calls}",
        )
        c.check(
            _snippet_row(wid).get("meta", {}).get("content_hash") != old_w_hash,
            "forbidden_word 编辑：content_hash 重算",
        )

    # ---- 404 / 422 ----
    resp_404 = client.patch(f"/api/v1/knowledge/{uuid.uuid4()}", json={"title": "x"})
    c.check(resp_404.status_code == 404, "PATCH 不存在的知识块 → 404", f"{resp_404.status_code}")
    resp_422 = client.patch(f"/api/v1/knowledge/{sid}", json={"content": ""})
    c.check(resp_422.status_code == 422, "PATCH 空正文 → 422", f"{resp_422.status_code}")


# ---------------------------------------------------------------------------
# 组 5：人设知识 RAG（纳入 / 隔离 / 归档 / 降级 / 融合）
# ---------------------------------------------------------------------------
def group5_persona_rag(c: Checker, client: TestClient, fx: Fixture) -> None:
    fake = FakeEmbedder()
    persona_content = f"话术：用通勤场景开场，先讲痛点再给方案。{fx.tag}"

    with _AdminEnv(fake):
        resp = client.post(
            "/api/v1/knowledge/manual",
            json={
                "chunk_type": "persona_knowledge",
                "title": f"{PREFIX}-人设知识P1",
                "content": persona_content,
                "ref_type": "persona",
                "ref_id": str(fx.persona_id),
            },
        )
    p1 = fx.record_snippet(resp.json().get("id")) if resp.status_code == 201 else ""
    c.check(resp.status_code == 201 and bool(p1), "人设知识块创建成功（前置）", f"{resp.status_code}")
    if not p1:
        return

    # ---- (1) 纳入渲染（query 为空也纳入人设知识）----
    with SessionLocal() as db:
        block = safe_rag_block(
            db, query_text="", persona_id=str(fx.persona_id), source="selftest-人设纳入"
        )
    c.check(
        persona_content in block and "人设知识" in block,
        "safe_rag_block(persona_id)：人设知识无条件纳入且带「人设知识」标注",
        f"{block[:200]}",
    )
    c.check(
        RAG_BLOCK_TITLE in rag_service.render_rag_prompt_section(block),
        "人设知识渲染进提示词时同样带「仅供参考」标题",
    )

    # ---- (2) 他人设隔离 ----
    with SessionLocal() as db:
        block_b = safe_rag_block(
            db, query_text="", persona_id=str(fx.persona_b_id), source="selftest-人设隔离"
        )
    c.check(block_b == "", "他人设（无知识块）→ 空串（ref_id 过滤生效）", f"{block_b!r}")

    # ---- (3) 归档后不再纳入 ----
    # 注：本组只归档自己创建的 p1；同一人设下 group3 创建的 active 块（内容不同）
    # 仍会正常渲染，因此断言「p1 正文不再出现」而非「block 为空串」。
    client.patch(f"/api/v1/knowledge/{p1}", json={"status": "archived"})
    with SessionLocal() as db:
        block_arch = safe_rag_block(
            db, query_text="", persona_id=str(fx.persona_id), source="selftest-人设归档"
        )
    c.check(
        persona_content not in block_arch,
        "人设知识归档后不纳入（归档块从渲染中剔除）",
        f"{block_arch!r}",
    )
    client.patch(f"/api/v1/knowledge/{p1}", json={"status": "active"})

    # ---- (4) 读取失败降级（不抛错、按无人设知识继续）----
    raised = False
    degraded: str | None = None
    with _NoPersonaLoader():
        try:
            with SessionLocal() as db:
                degraded = safe_rag_block(
                    db, query_text="", persona_id=str(fx.persona_id), source="selftest-人设降级"
                )
        except Exception:  # noqa: BLE001 —— 若抛异常即为「阻断主流程」的回归
            raised = True
    c.check(
        raised is False and degraded == "",
        "人设知识读取失败 → 只 WARNING 降级（返回空串，主流程不阻断）",
        f"raised={raised} degraded={degraded!r}",
    )

    # ---- (5) 与向量检索融合（去重、人设知识在前）----
    fusion_text = f"{PREFIX} 融合检索目标文本 {fx.tag}"
    with _AdminEnv(fake):
        resp_h = client.post(
            "/api/v1/knowledge/manual",
            json={
                "chunk_type": "hit_pattern",
                "title": f"{PREFIX}-融合块",
                "content": fusion_text,
            },
        )
    h1 = fx.record_snippet(resp_h.json().get("id")) if resp_h.status_code == 201 else ""
    c.check(resp_h.status_code == 201 and bool(h1), "融合测试前置：规律块创建成功", f"{resp_h.status_code}")
    if h1:
        with _AdminEnv(fake):
            with SessionLocal() as db:
                block_f = safe_rag_block(
                    db,
                    query_text=fusion_text,
                    chunk_types=("hit_pattern",),
                    persona_id=str(fx.persona_id),
                    source="selftest-融合",
                )
        c.check(
            persona_content in block_f and fusion_text in block_f,
            "融合：人设知识与向量检索命中同时纳入",
            f"{block_f[:220]}",
        )
        c.check(
            block_f.find(persona_content) < block_f.find(fusion_text),
            "融合顺序：人设知识排在通用检索之前",
        )

    # ---- (6) 无 persona 时行为与本步之前一致（空 query 直接空串）----
    with SessionLocal() as db:
        block_none = safe_rag_block(
            db, query_text="   ", persona_id=None, source="selftest-无persona"
        )
    c.check(block_none == "", "无 persona + 空 query → 空串（与第 18 步一致）", f"{block_none!r}")


# ---------------------------------------------------------------------------
# 组 6：文档导入（文本 / 文件 / 部分失败 / 校验）
# ---------------------------------------------------------------------------
def group6_import(c: Checker, client: TestClient, fx: Fixture) -> None:
    fake = FakeEmbedder()
    long_text = (
        f"{PREFIX} 文档导入测试 {fx.tag}。\n\n"
        + "通勤穿搭的技巧分享。\n\n".join([f"第{i}段：先讲痛点再给方案，实用优先。" * 4 for i in range(6)])
    )

    # ---- (1) 文本导入（多块）----
    with _AdminEnv(fake):
        resp = client.post(
            "/api/v1/knowledge/import-text",
            json={"content": long_text, "chunk_type": "general", "chunk_size": 250, "chunk_overlap": 50},
        )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    c.check(resp.status_code == 201, "POST /import-text → 201", f"{resp.status_code} {resp.text[:200]}")
    if resp.status_code == 201:
        for sid in body.get("snippet_ids") or []:
            fx.record_snippet(sid)
    total = int(body.get("total_chunks") or 0)
    c.check(
        total >= 3 and body.get("inserted") == total and body.get("skipped") == 0 and body.get("failed") == 0,
        "文本导入：多块切分、全部写入、零跳过零失败",
        f"{body}",
    )
    c.check(
        body.get("chunk_size") == 250 and body.get("chunk_overlap") == 50 and bool(body.get("splitter")),
        "导入响应回显切分参数与实现（chunk_size / chunk_overlap / splitter）",
        f"size={body.get('chunk_size')} overlap={body.get('chunk_overlap')} splitter={body.get('splitter')}",
    )
    c.check(
        "embedding" not in resp.text and "llm_raw" not in resp.text,
        "导入响应不含 embedding / llm_raw 字样",
    )

    # ---- (2) 同文本再导：幂等跳过 ----
    with _AdminEnv(fake):
        resp2 = client.post(
            "/api/v1/knowledge/import-text",
            json={"content": long_text, "chunk_type": "general", "chunk_size": 250, "chunk_overlap": 50},
        )
    body2 = resp2.json() if resp2.headers.get("content-type", "").startswith("application/json") else {}
    c.check(
        resp2.status_code == 201
        and body2.get("inserted") == 0
        and body2.get("skipped") == int(body2.get("total_chunks") or 0),
        "同文本再导：全部跳过（查重先于向量化，幂等）",
        f"{body2}",
    )
    if resp2.status_code == 201:
        for sid in body2.get("snippet_ids") or []:
            fx.record_snippet(sid)

    # ---- (3) 部分块失败：如实计数、其它块照常 ----
    # 注：这里显式 chunk_overlap=0 —— 使失败标记只落在**唯一一块**内。
    # 若保留默认重叠（100），标记会被「保留尾部」语义带进相邻块（切分器行为正确：
    # 实测会得到 2 块含标记 / failed=2）。重叠传递本身已在组 0 单独断言
    # （「相邻块保留 overlap（语义边界不丢）」），此处不再混用两种语义。
    selective = SelectiveThrowEmbedder()
    partial_text = (
        f"{PREFIX} 部分失败导入 {fx.tag}。\n\n"
        + ("甲段填充内容。" * 30)
        + "\n\n"
        + MARK_FAIL
        + "\n\n"
        + ("乙段填充内容。" * 30)
    )
    with _AdminEnv(selective):
        resp3 = client.post(
            "/api/v1/knowledge/import-text",
            json={
                "content": partial_text,
                "chunk_type": "general",
                "chunk_size": 250,
                "chunk_overlap": 0,
            },
        )
    body3 = resp3.json() if resp3.headers.get("content-type", "").startswith("application/json") else {}
    if resp3.status_code == 201:
        for sid in body3.get("snippet_ids") or []:
            fx.record_snippet(sid)
    c.check(
        resp3.status_code == 201 and body3.get("failed") == 1,
        "部分块向量化失败：failed 如实计 1",
        f"{resp3.status_code} {resp3.text[:220]}",
    )
    c.check(
        body3.get("inserted") == int(body3.get("total_chunks") or 0) - 1
        and body3.get("inserted") >= 1,
        "部分块失败不影响其它块（inserted = total - failed）",
        f"{body3}",
    )
    c.check(
        len(body3.get("warnings") or []) == 1,
        "部分失败给出 1 条 warning（逐块如实计数）",
        f"{body3.get('warnings')}",
    )

    # ---- (4) persona_knowledge 导入（关联人设）----
    with _AdminEnv(fake):
        resp4 = client.post(
            "/api/v1/knowledge/import-text",
            json={
                "content": f"{PREFIX} 人设知识导入 {fx.tag}。{('补充内容。' * 30)}",
                "chunk_type": "persona_knowledge",
                "ref_id": str(fx.persona_id),
            },
        )
    body4 = resp4.json() if resp4.headers.get("content-type", "").startswith("application/json") else {}
    if resp4.status_code == 201:
        for sid in body4.get("snippet_ids") or []:
            fx.record_snippet(sid)
    c.check(resp4.status_code == 201 and body4.get("inserted") == body4.get("total_chunks"),
            "persona_knowledge 导入（关联人设）→ 201 全写入", f"{resp4.status_code} {resp4.text[:200]}")

    resp4b = client.post(
        "/api/v1/knowledge/import-text",
        json={"content": f"{PREFIX} 缺人设导入", "chunk_type": "persona_knowledge"},
    )
    c.check(resp4b.status_code == 400, "persona_knowledge 导入未带 ref_id → 400", f"{resp4b.status_code}")

    # ---- (5) 请求校验：空文本 422 / 超长 422 / overlap ≥ size 400 ----
    resp_e = client.post("/api/v1/knowledge/import-text", json={"content": ""})
    c.check(resp_e.status_code == 422, "import-text 空文本 → 422", f"{resp_e.status_code}")
    resp_l = client.post("/api/v1/knowledge/import-text", json={"content": "字" * 200001})
    c.check(resp_l.status_code == 422, "import-text 超长（>200000）→ 422", f"{resp_l.status_code}")
    with _AdminEnv(fake):
        resp_ov = client.post(
            "/api/v1/knowledge/import-text",
            json={"content": f"{PREFIX} overlap 校验 {fx.tag}", "chunk_size": 200, "chunk_overlap": 200},
        )
    c.check(resp_ov.status_code == 400, "import-text overlap ≥ size → 400", f"{resp_ov.status_code}")

    # ---- (6) 文件上传：.txt / .md 接受；.pdf / 非 UTF-8 / 空文件拒绝 ----
    with _AdminEnv(fake):
        resp_txt = client.post(
            "/api/v1/knowledge/import-file",
            files={"file": (f"selftest_{fx.tag}.txt", f"{PREFIX} 文件导入 {fx.tag}。内容正文。".encode("utf-8"), "text/plain")},
            data={"chunk_type": "general"},
        )
    body_txt = resp_txt.json() if resp_txt.headers.get("content-type", "").startswith("application/json") else {}
    if resp_txt.status_code == 201:
        for sid in body_txt.get("snippet_ids") or []:
            fx.record_snippet(sid)
    c.check(resp_txt.status_code == 201 and body_txt.get("inserted", 0) >= 1,
            "上传 .txt（UTF-8）→ 201 并写入", f"{resp_txt.status_code} {resp_txt.text[:200]}")

    with _AdminEnv(fake):
        resp_md = client.post(
            "/api/v1/knowledge/import-file",
            files={"file": (f"selftest_{fx.tag}.md", f"# {PREFIX} markdown 导入 {fx.tag}\n\n正文段落。".encode("utf-8"), "text/markdown")},
        )
    body_md = resp_md.json() if resp_md.headers.get("content-type", "").startswith("application/json") else {}
    if resp_md.status_code == 201:
        for sid in body_md.get("snippet_ids") or []:
            fx.record_snippet(sid)
    c.check(resp_md.status_code == 201, "上传 .md → 201", f"{resp_md.status_code} {resp_md.text[:200]}")

    resp_pdf = client.post(
        "/api/v1/knowledge/import-file",
        files={"file": ("bad.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    c.check(resp_pdf.status_code == 400, "上传 .pdf → 400（明确拒绝并提示）", f"{resp_pdf.status_code} {resp_pdf.text[:200]}")

    resp_enc = client.post(
        "/api/v1/knowledge/import-file",
        files={"file": ("bad_enc.txt", b"\xff\xfe\x00bad bytes", "text/plain")},
    )
    c.check(resp_enc.status_code == 400, "上传非 UTF-8 文件 → 400", f"{resp_enc.status_code} {resp_enc.text[:200]}")

    resp_empty = client.post(
        "/api/v1/knowledge/import-file",
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    c.check(resp_empty.status_code == 400, "上传空文件 → 400", f"{resp_empty.status_code}")

    resp_badref = client.post(
        "/api/v1/knowledge/import-file",
        files={"file": ("x.txt", f"{PREFIX} 文本".encode("utf-8"), "text/plain")},
        data={"chunk_type": "persona_knowledge", "ref_id": "not-a-uuid"},
    )
    c.check(resp_badref.status_code == 400, "上传表单 ref_id 非法 → 400", f"{resp_badref.status_code}")

    # ---- (7) 未装依赖时的真实闸门（import 也是 400 零写入；注入依赖缺失）----
    marker = f"{PREFIX} 未就绪导入 {fx.tag}"
    with _EmbeddingDepsMissing():
        resp_gate = client.post("/api/v1/knowledge/import-text", json={"content": marker})
    c.check(
        resp_gate.status_code == 400,
        "未装依赖：import-text → 400（向量化未就绪，零写入）",
        f"{resp_gate.status_code} {resp_gate.text[:200]}",
    )
    c.check(
        _count_rows("SELECT count(*) FROM knowledge_snippets WHERE content = :c", {"c": marker}) == 0,
        "未就绪导入零写入（库内无比对文本）",
    )


# ---------------------------------------------------------------------------
# 组 7：创作图合规双路径（真跑图；graph.invoke）
# ---------------------------------------------------------------------------
def _run_content_graph(topic_id: uuid.UUID, body_text: str, fx: Fixture) -> dict[str, Any]:
    """跑一次创作图（无检查点；**临时关闭内容 RAG**，隔离违禁词来源变量）。"""
    draft = {
        "title": f"{PREFIX}-创作标题{fx.tag}",
        "body": body_text,
        "tags": ["自检标签a", "自检标签b", "自检标签c"],
    }
    model = AdminScriptModel(responses=[draft])
    graph = build_content_graph(model=model, use_checkpointer=False)
    orig = settings.CONTENT_RAG_ENABLED
    settings.CONTENT_RAG_ENABLED = False
    try:
        state = graph.invoke({"topic_id": str(topic_id)}, {})
    finally:
        settings.CONTENT_RAG_ENABLED = orig
    return {"state": state, "model": model}


def group7_content_graph(c: Checker, client: TestClient, fx: Fixture) -> None:
    # ---- 路径 1：知识库自定义词命中 ----
    resp_word = client.post(
        "/api/v1/knowledge/manual",
        json={"chunk_type": "forbidden_word", "content": CUSTOM_WORD, "category": PREFIX},
    )
    fx.record_snippet(resp_word.json().get("id")) if resp_word.status_code == 201 else None
    c.check(resp_word.status_code == 201, "双路径前置：自定义词写入知识库", f"{resp_word.status_code}")

    run1 = _run_content_graph(fx.topic(0), _draft_body(f"这里有{CUSTOM_WORD}出现"), fx)
    note1 = run1["state"].get("note_id")
    if note1:
        fx.record_note(note1)
    row1 = _fetch_note(str(note1))
    check1 = row1.get("content_check") or {}
    hit1_words = [str(item.get("word")) for item in (check1.get("hits") or [])]
    c.check(
        bool(note1) and row1.get("compliance_status") == "warning" and CUSTOM_WORD in hit1_words,
        "路径1：知识库有自定义词表 → 创作图命中自定义词（content_check.hits）",
        f"note={note1} hits={hit1_words}",
    )
    c.check(
        check1.get("status") == "warning"
        and check1.get("checked_fields") == ["title", "body", "tags"]
        and bool(check1.get("checked_at")),
        "content_check 结构不变（status / hits / checked_fields / checked_at）",
        f"{check1}",
    )
    c.check(
        row1.get("status") == "draft",
        "命中风险词仍落库为 draft（不阻断、不改写）",
        f"{row1.get('status')}",
    )

    # ---- 路径 2：回退内置词表命中（注入空词表，与环境里的库内容解耦）----
    with _InjectedWordlist([]):
        run2 = _run_content_graph(fx.topic(1), _draft_body("这个是稳赚不赔的买卖"), fx)
    note2 = run2["state"].get("note_id")
    if note2:
        fx.record_note(note2)
    row2 = _fetch_note(str(note2))
    hits2 = [str(item.get("word")) for item in ((row2.get("content_check") or {}).get("hits") or [])]
    c.check(
        bool(note2) and row2.get("compliance_status") == "warning" and BUILTIN_WORD in hits2,
        "路径2：知识库为空 → 回退内置词表命中（创作图链路，注入空词表）",
        f"note={note2} hits={hits2}",
    )

    # ---- 静态证据：创作图节点确经 scan_with_knowledge 扫描 ----
    graph_src = (BACKEND_DIR / "app" / "agent" / "content" / "graph.py").read_text(encoding="utf-8")
    c.check(
        "scan_with_knowledge(" in graph_src and "scan_note(title=title, body=body, tags=tags)" not in graph_src,
        "创作图 validate_compliance 经 scan_with_knowledge（词表来源已切换）",
    )

    # ---- 清理路径 1 的自定义词（恢复库状态）----
    with SessionLocal() as db:
        db.execute(
            text("DELETE FROM knowledge_snippets WHERE content = :word"), {"word": CUSTOM_WORD}
        )
        db.commit()


# ---------------------------------------------------------------------------
# 组 8：接口面 / 零迁移 / 合规扫描
# ---------------------------------------------------------------------------
def group8_surface(c: Checker, client: TestClient) -> None:
    paths = app.openapi()["paths"]

    expected = {
        "/api/v1/knowledge": ["get"],
        "/api/v1/knowledge/extract": ["post"],
        "/api/v1/knowledge/search": ["post"],
        "/api/v1/knowledge/manual": ["post"],
        "/api/v1/knowledge/seed-forbidden-words": ["post"],
        "/api/v1/knowledge/import-text": ["post"],
        "/api/v1/knowledge/import-file": ["post"],
        "/api/v1/knowledge/{snippet_id}": ["get", "patch"],
    }
    for path, methods in expected.items():
        c.check(
            sorted(paths.get(path, {})) == methods,
            f"{path} 方法恰为 {methods}",
            f"{sorted(paths.get(path, {}))}",
        )

    bad_methods = [
        path
        for path in paths
        if "/knowledge" in path
        and any(method in paths[path] for method in ("put", "delete"))
    ]
    c.check(not bad_methods, "knowledge 路由不存在 PUT / DELETE（无物理删除）", f"{bad_methods}")

    # 静态路径先于 /{snippet_id} 注册（OpenAPI dict 保序）
    keys = list(paths.keys())
    id_index = keys.index("/api/v1/knowledge/{snippet_id}")
    static_paths = [p for p in expected if p != "/api/v1/knowledge/{snippet_id}"]
    order_ok = all(keys.index(p) < id_index for p in static_paths)
    c.check(order_ok, "静态路径全部先于 /{snippet_id} 注册（顺序断言）", f"{[keys.index(p) for p in static_paths]} < {id_index}")

    # 响应 schema 无敏感字段（新旧模型全扫）
    schemas = app.openapi()["components"]["schemas"]
    forbidden = {"embedding", "llm_raw", "api_key", "secret", "password", "cookie", "authorization"}
    model_names = (
        "SnippetOut",
        "SnippetListOut",
        "ExtractResultOut",
        "SnippetSearchRequest",
        "SearchHit",
        "SnippetSearchOut",
        "SnippetUpdateRequest",
        "SnippetManualCreateRequest",
        "SnippetWriteOut",
        "SeedForbiddenWordsOut",
        "ImportTextRequest",
        "ImportResultOut",
    )
    for name in model_names:
        def _props_of(schema: dict[str, Any]) -> set[str]:
            props = set((schema.get("properties") or {}).keys())
            for sub in schema.get("allOf") or []:
                props |= _props_of(sub)
            return props

        props = _props_of(schemas.get(name) or {})
        hits = [flag for flag in forbidden if any(flag in prop.lower() for prop in props)]
        c.check(not hits, f"{name} 无敏感字段（embedding / llm_raw / key / 凭据）", f"hits={hits}")

    # 422：分页边界 / 快照请求体
    resp_lim = client.get("/api/v1/knowledge?limit=0")
    c.check(resp_lim.status_code == 422, "GET /knowledge?limit=0 → 422（分页边界）", f"{resp_lim.status_code}")

    # main.py：include_router ≥ 12 且 lifespan 唯一
    main_src = (BACKEND_DIR / "app" / "main.py").read_text(encoding="utf-8")
    router_count = len(re.findall(r"app\.include_router\(", main_src))
    c.check(router_count >= 12, f"main.py include_router ≥ 12（当前 {router_count}；本步零新增 router）", f"{router_count}")
    c.check(
        len(re.findall(r"async def lifespan", main_src)) == 1,
        "lifespan 仍恰 1 个（本步无新常驻组件）",
    )

    # 路由层零 SQLAlchemy 表达式（与 selftest_api 同 pattern）
    route_src = (BACKEND_DIR / "app" / "api" / "routes" / "knowledge.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in route_src.splitlines() if not line.strip().startswith("#"))
    offenders = [pat for pat in (r"\bdb\.execute\(", r"\bdb\.query\(", r"\bselect\(") if re.search(pat, code)]
    c.check(not offenders, "knowledge 路由层零 SQLAlchemy 表达式", f"{offenders}")

    # 零迁移
    files = sorted(p.name for p in VERSIONS_DIR.glob("*.py") if not p.name.startswith("__"))
    c.check(len(files) == 2, "migrations/versions 仍恰 2 支（本步零迁移）", f"{files}")
    c.check(
        all(name.startswith(("0001", "0002")) for name in files),
        "迁移文件名仍为 0001 / 0002",
        f"{files}",
    )
    with SessionLocal() as db:
        tables = sorted(
            row[0]
            for row in db.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename != 'alembic_version'"
                )
            ).all()
        )
    c.check(
        len(tables) == EXPECTED_TABLES,
        f"public 业务表仍恰 {EXPECTED_TABLES} 张（RefType 只加枚举成员，无 DDL）",
        f"{len(tables)}: {tables}",
    )
    c.check(
        set(BUSINESS_TABLES_8).issubset(set(tables)),
        "第 17 步八张业务表仍在册（口径延续）",
    )
    env = dict(os.environ)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "current"],
            cwd=str(BACKEND_DIR),
            capture_output=True,
            text=True,
            env=env,
            timeout=90,
        )
        out = (proc.stdout + proc.stderr).strip()
        c.check(
            proc.returncode == 0 and "0002" in out and "(head)" in out,
            "alembic current = 0002 (head)",
            out[-200:],
        )
    except Exception as exc:  # noqa: BLE001
        c.check(False, "alembic current = 0002 (head)", f"执行异常：{exc}")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "check"],
            cwd=str(BACKEND_DIR),
            capture_output=True,
            text=True,
            env=env,
            timeout=90,
        )
        out = (proc.stdout + proc.stderr).strip()
        c.check(proc.returncode == 0, "alembic check 零漂移（退出码 0）", out[-200:])
    except Exception as exc:  # noqa: BLE001
        c.check(False, "alembic check 零漂移（退出码 0）", f"执行异常：{exc}")


def group9_compliance(c: Checker) -> None:
    self_path = Path(__file__).resolve()
    for path in SCANNED_BACKEND_FILES + SCANNED_SCRIPT_FILES + SCANNED_FRONTEND_FILES:
        if path.resolve() == self_path:
            # 自指豁免（可审计）：本文件是**扫描器本身**，源码内必须定义
            # FORBIDDEN_PATTERNS（禁词模式元组）与假密钥拼接串，必然命中自身扫描。
            # 其合规性由「其余 20 个产物文件逐一扫描 + 全前端域名计数 == 1 +
            # 接口面 / 响应 schema / 路由层静态断言」共同背书；豁免仅限于此文件本身。
            c.skip(f"{path.relative_to(ROOT_DIR).as_posix()}：扫描器自指豁免（不代表通过）")
            continue
        src = path.read_text(encoding="utf-8")
        hits = [pattern for pattern in FORBIDDEN_PATTERNS if pattern in src]
        c.check(
            not hits,
            f"{path.relative_to(ROOT_DIR).as_posix()} 不含小红书域名 / 登录态凭据 / 密钥前缀",
            f"{hits}",
        )

    ts_files = sorted(FRONTEND_SRC.rglob("*.ts")) + sorted(FRONTEND_SRC.rglob("*.tsx"))
    total = 0
    holders: list[str] = []
    for path in ts_files:
        src = path.read_text(encoding="utf-8")
        count = src.count("xiaohongshu")
        if count:
            total += count
            holders.append(path.relative_to(ROOT_DIR).as_posix())
    c.check(
        total == 1 and holders == ["frontend/src/app/notes/[id]/note-studio-client.tsx"],
        "全前端平台域名仍恰 1 处（第 14 步发布外链；本步未新增）",
        f"total={total} holders={holders}",
    )

    # 前端新 BFF 不写 export const dynamic（Next 16 范式）
    for path in SCANNED_FRONTEND_FILES:
        if path.suffix == ".ts" and "api" in path.as_posix():
            src = path.read_text(encoding="utf-8")
            c.check(
                "export const dynamic" not in src,
                f"{path.relative_to(ROOT_DIR).as_posix()} 未使用 export const dynamic",
            )

    # 提示词 / 违禁词服务不外泄外部数字口径（本步红线延续）
    fs_src = (BACKEND_DIR / "app" / "services" / "forbidden_source.py").read_text(encoding="utf-8")
    c.check(
        "保守兜底" in fs_src and "回退" in fs_src,
        "forbidden_source 明文保留「保守兜底 / 回退」口径（可审计）",
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    c = Checker()
    print("=" * 78)
    print("第 19 步 · 知识库人工维护（违禁词 / 手动块 / 人设知识 / 文档导入）离线自检")
    print("（不联网、不下载 bge 模型、不需要 API Key）")
    print("=" * 78)

    try:
        init_checkpointer()
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] 检查点初始化失败（需本地 PostgreSQL）：{exc}")
        return 1

    client = TestClient(app, raise_server_exceptions=False)
    fx = Fixture()
    try:
        fx.setup()
        group0_contracts(c)
        group1_word_source(c, client, fx)
        group2_seed(c, client, fx)
        group3_manual_create(c, client, fx)
        group4_edit(c, client, fx)
        group5_persona_rag(c, client, fx)
        group6_import(c, client, fx)
        group7_content_graph(c, client, fx)
        group8_surface(c, client)
        group9_compliance(c)
    except Exception as exc:  # noqa: BLE001 —— 未预期异常也要走清理
        import traceback

        c.check(
            False,
            "自检全程未抛未预期异常",
            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
        )
    finally:
        try:
            fx.cleanup()
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] 清理阶段异常：{type(exc).__name__}: {exc}")
        close_checkpointer()

    try:
        residue = fx.verify_residue()
        bad = {key: value for key, value in residue.items() if value != 0}
        c.check(not bad, "清理复查：双口径残留全 0（含 agent_runs / 检查点线程差集）", f"{residue}")
    except Exception as exc:  # noqa: BLE001
        c.check(False, "清理复查", f"{type(exc).__name__}: {exc}")

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项，跳过 {len(c.skipped)} 项。")
    if c.skipped:
        for item in c.skipped:
            print(f"  - SKIP: {item}")
    if c.failures:
        for item in c.failures:
            print(f"  - FAIL: {item}")
        return 1
    print("全部 PASS ✔（离线自检：真实本地 PostgreSQL + 假 embedder，零真实请求）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
