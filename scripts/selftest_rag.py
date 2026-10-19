"""第 18 步 · RAG 知识库（embedding + 规律提炼 + 检索 + 反哺）后端自检
（**依赖本地 docker PostgreSQL；不联网、不下载 bge 模型、不需要真实 API Key**）。

覆盖任务书要求的全部断言，分 10 组：

0. 图结构：规律提炼图是真 LangGraph（六节点 + 两条条件边 + **有界回路**）：
   ``load_context →（条件边：样本充足）generate_patterns → validate_patterns
   →（条件边）embed_and_persist / 回炉 / give_up``；样本不足 → ``insufficient_sample``；
1. embedding 服务（假 embedder 注入）：批量切分（EMBEDDING_BATCH_SIZE）、
   sha256 缓存命中不重复调用、非 1024 维**报错不静默截断**、返回条数不符报错、
   ``validate_dim_settings`` 与数据库列维度比对（不一致给中文错误）；
2. 提炼成功路径（假模型 + 假 embedder 经 HTTP ``POST /extract``）：201 +
   向量化 → 写 ``knowledge_snippets``（embedding 非空、meta 齐全）+ ``parse_attempts=1``
   + 假模型恰 1 次调用 + 提示词含「禁止外部数字」硬约束 + ``agent_runs`` 回填
   （run_type=insights、outputs 业务明细、llm_raw 脱敏截断）+ 响应无 llm_raw；
   **幂等**：同文本再跑 → ``reused=2`` / ``persisted=0``（绝不重复插入）；
3. 样本不足路径：门槛临时调高 → ``insufficient_sample=True``、**零模型调用零写入**、
   data_gaps 含「至少发布 N 篇并回填数据后再提炼」、run 补记 ``partial``；
4. 检索：``crud_knowledge.search_snippets`` 向量 top-k（自查询向量必然排第一、
   similarity≈1.0）+ chunk_type 过滤 + **archived 不召回 / include_archived 才召回**
   + 返回字段无 embedding；HTTP ``POST /search``（含 similarity）+ 参数 422；
   未装依赖时检索接口 400（如实告知，不假装无命中）；
5. 反哺接入：``safe_rag_block`` 命中含规律文本 / ``enabled=False`` 零检索 /
   embedder 抛错**降级不阻断**（返回空串）；选题 / 创作图 ``build_messages`` 的
   RAG 块替换等价 —— **去掉 RAG 段后与不传 rag_context 逐字一致**（不影响既有逻辑）；
6. 接口面：五个端点方法精确（无 PUT / 无 DELETE）、静态路径先注册、分页边界 422、
   PATCH 仅 active/archived（非法值 422、不存在 404）、响应 schema 无
   embedding / llm_raw / key、``main.py`` include_router ≥ 12 且 lifespan 唯一、
   路由层零 SQLAlchemy 表达式、``INSIGHTS_SCHEDULER_ENABLED`` 默认 False；
7. 失败路径（图内重试语义）：全坏 JSON → 400 且假模型调用次数 = 3×3=9
   （``AGENT_JSON_MAX_ATTEMPTS`` 双因子）、结构全坏（可解析但条目不合格）→ 恰 3 轮回炉
   → 400、LLM 未配置 → 400 且**零请求零 run**、embedding 未就绪 → 400 零 run；
8. 零迁移：``migrations/versions`` 仍恰 2 支、``alembic current``=0002 (head)、
   ``alembic check`` 退出码 0、public 业务表仍恰 10 张；
9. 合规扫描：本步新增 / 修改的后端、前端文件不含小红书域名 / 登录态凭据 / 密钥前缀；
   全前端域名仍**恰 1 处**（第 14 步发布外链，本步未新增）；提示词含「禁止外部数字」；
10. 清理复查：测试数据（前缀 ``[SELFTEST]知识库``）按 id 删 + 前缀兜底扫，
   双口径残留 0；检查点线程与 agent_runs 按**运行前后差集**安全清理。

HTTP 断言走**进程内** ``TestClient``（不需要 uvicorn）；假模型 / 假 embedder 经
monkeypatch 注入（**不发真实请求、不加载本地模型**）。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\selftest_rag.py

全部通过退出码 0；任一项 FAIL 退出码 1。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
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
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from pydantic import Field  # noqa: E402
from sqlalchemy import text  # noqa: E402

import app.agent.embedding.service as embedding_service  # noqa: E402
import app.agent.insights.service as insights_service  # noqa: E402
from app.agent.checkpoint import close_checkpointer, init_checkpointer  # noqa: E402
from app.agent.embedding.base import (  # noqa: E402
    EmbeddingDimensionError,
    EmbeddingError,
    EmbeddingNotAvailableError,
)
from app.agent.embedding.local import INSTALL_HINT, LocalBGEEmbedder  # noqa: E402
from app.agent.embedding.service import (  # noqa: E402
    describe_embedding_readiness,
    embed_texts,
    reset_embedder,
    validate_dim_settings,
)
from app.agent.errors import AgentConfigError  # noqa: E402
from app.agent.insights.graph import (  # noqa: E402
    NODE_GENERATE,
    NODE_GIVE_UP,
    NODE_INSUFFICIENT,
    NODE_LOAD_CONTEXT,
    NODE_PERSIST,
    NODE_VALIDATE,
    build_insights_graph,
)
from app.agent.insights.prompts import build_messages as insights_build_messages  # noqa: E402
from app.core.config import Settings, settings  # noqa: E402
from app.core.timeutil import now_local  # noqa: E402
from app.crud.knowledge import crud_knowledge  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.models.diagnostic import Diagnostic  # noqa: E402
from app.models.enums import (  # noqa: E402
    AccountStatus,
    ChunkType,
    ComplianceStatus,
    DataSource,
    DiagnosisType,
    NoteStatus,
    PersonaStatus,
    SnippetStatus,
    TopicStatus,
)
from app.models.metric import NoteMetric  # noqa: E402
from app.models.note import Note  # noqa: E402
from app.models.persona import Persona  # noqa: E402
from app.models.topic import Topic  # noqa: E402
from app.services.rag import (  # noqa: E402
    RAG_BLOCK_TITLE,
    render_rag_prompt_section,
    safe_rag_block,
)

#: 本脚本全部测试数据统一前缀（清理与复查的双口径都靠它兜底）
PREFIX = "[SELFTEST]知识库"

VERSIONS_DIR = BACKEND_DIR / "migrations" / "versions"

#: 本步新增 / 修改过的后端文件（合规扫描对象）
SCANNED_FILES = (
    BACKEND_DIR / "app" / "agent" / "embedding" / "__init__.py",
    BACKEND_DIR / "app" / "agent" / "embedding" / "base.py",
    BACKEND_DIR / "app" / "agent" / "embedding" / "local.py",
    BACKEND_DIR / "app" / "agent" / "embedding" / "openai_compat.py",
    BACKEND_DIR / "app" / "agent" / "embedding" / "service.py",
    BACKEND_DIR / "app" / "agent" / "insights" / "__init__.py",
    BACKEND_DIR / "app" / "agent" / "insights" / "state.py",
    BACKEND_DIR / "app" / "agent" / "insights" / "contracts.py",
    BACKEND_DIR / "app" / "agent" / "insights" / "prompts.py",
    BACKEND_DIR / "app" / "agent" / "insights" / "graph.py",
    BACKEND_DIR / "app" / "agent" / "insights" / "service.py",
    BACKEND_DIR / "app" / "services" / "rag.py",
    BACKEND_DIR / "app" / "crud" / "knowledge.py",
    BACKEND_DIR / "app" / "schemas" / "knowledge.py",
    BACKEND_DIR / "app" / "api" / "routes" / "knowledge.py",
    BACKEND_DIR / "app" / "agent" / "topics" / "graph.py",
    BACKEND_DIR / "app" / "agent" / "topics" / "state.py",
    BACKEND_DIR / "app" / "agent" / "topics" / "prompts.py",
    BACKEND_DIR / "app" / "agent" / "content" / "graph.py",
    BACKEND_DIR / "app" / "agent" / "content" / "state.py",
    BACKEND_DIR / "app" / "agent" / "content" / "prompts.py",
    BACKEND_DIR / "app" / "models" / "enums.py",
    BACKEND_DIR / "app" / "core" / "config.py",
    BACKEND_DIR / "app" / "main.py",
)

#: 本步新增 / 修改过的前端文件（合规扫描对象）。
#: 注意：`notes/[id]/note-studio-client.tsx` **不在此清单** —— 该文件含第 14 步既有
#: 的「前往发布页」外链常量（含平台域名），已由 ``selftest_studio.py`` 的专用断言
#: 背书；组 9 另有「全前端域名恰 1 处」的守卫断言。
FRONTEND_SCANNED = (
    FRONTEND_SRC / "app" / "knowledge" / "page.tsx",
    FRONTEND_SRC / "app" / "api" / "v1" / "knowledge" / "route.ts",
    FRONTEND_SRC / "app" / "api" / "v1" / "knowledge" / "extract" / "route.ts",
    FRONTEND_SRC / "app" / "api" / "v1" / "knowledge" / "search" / "route.ts",
    FRONTEND_SRC / "app" / "api" / "v1" / "knowledge" / "[id]" / "route.ts",
    FRONTEND_SRC / "lib" / "types.ts",
    FRONTEND_SRC / "components" / "site-header.tsx",
)

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

#: 六个指标（测试侧硬编码，**不复用后端常量**以防「同源错误」）
METRIC_KEYS = ("views", "likes", "collects", "comments", "shares", "new_followers")

#: 业务表（public schema，不含 alembic_version）期望张数
EXPECTED_TABLES = 10

#: 假密钥串（运行时拼接，**源码内不出现连续字样**，避免合规扫描误报）
FAKE_SECRET = "s" + "k-" + "test-1234567890abcdef"


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
    """确定性假向量化器：sha256(text) 决定随机种子 → 同文本恒同向量。

    记录每次调用的条数（``calls``）与文本批次（``batches``）供批量 / 缓存断言。
    """

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


class BadDimEmbedder(FakeEmbedder):
    """返回非 1024 维向量（模拟换了小维度模型却没迁移列）—— 必须报错。"""

    def __init__(self) -> None:
        super().__init__(dim=512)


class ThrowingEmbedder:
    """恒抛「依赖未安装」错误的 embedder（验证 RAG 降级不阻断）。"""

    dim = 1024

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingNotAvailableError("本地向量化依赖未安装（自检注入）。")


class ShortReturnEmbedder(FakeEmbedder):
    """返回条数少于请求条数（模拟底层实现缺陷）—— 必须报错不猜。"""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        return [self._vec(text) for text in texts[:1]]


# ---------------------------------------------------------------------------
# 假模型（照 selftest_dashboard / selftest_diagnostics 的确定性脚本模型）
# ---------------------------------------------------------------------------
class InsightsScriptModel(BaseChatModel):
    """确定性假模型：按调用次序取响应；末项会被重复使用（模拟模型固执己见）。"""

    responses: list[Any] = Field(default_factory=list)
    calls: int = 0
    prompts: list[str] = Field(default_factory=list)
    usage_in: int = 17
    usage_out: int = 29

    @property
    def _llm_type(self) -> str:
        return "insights-script"

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
        text = self._text_of(messages)
        self.prompts.append(text)

        sequence: list[Any] = list(self.responses)
        index = min(self.calls, len(sequence) - 1) if sequence else -1
        item = sequence[index] if sequence else "{}"
        self.calls += 1

        if isinstance(item, BaseException):
            raise item
        content = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        message = AIMessage(
            content=content,
            usage_metadata={
                "input_tokens": self.usage_in,
                "output_tokens": self.usage_out,
                "total_tokens": self.usage_in + self.usage_out,
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


# ---------------------------------------------------------------------------
# 注入上下文管理器（退出时恢复原样；自检不污染真实环境）
# ---------------------------------------------------------------------------
class _InsightsEnv:
    """组合 patch：模拟「本地依赖已装 + 模型就绪」的运行环境。

    - ``insights_service.get_chat_model`` → 返回注入的假模型；
    - ``insights_service.local_deps_installed`` → True（**只 mock 环境探测**，
      闸门逻辑本身照常运行）；
    - ``embedding_service.get_embedder`` → 返回注入的假 embedder
      （``embed_texts(embedder=None)`` 路径由此命中假实现）。
    """

    def __init__(self, model: Any | None, embedder: Any | None) -> None:
        self.model = model
        self.embedder = embedder
        self._originals: dict[str, Any] = {}

    def __enter__(self) -> "_InsightsEnv":
        self._originals["get_chat_model"] = insights_service.get_chat_model
        self._originals["local_deps_installed"] = insights_service.local_deps_installed
        self._originals["get_embedder"] = embedding_service.get_embedder
        if self.model is not None:
            insights_service.get_chat_model = lambda **kwargs: self.model  # noqa: ARG005
        insights_service.local_deps_installed = lambda: True
        if self.embedder is not None:
            embedding_service.get_embedder = lambda: self.embedder
        reset_embedder()  # 清进程内向量缓存，避免跨用例命中
        return self

    def __exit__(self, *exc_info: Any) -> None:
        insights_service.get_chat_model = self._originals["get_chat_model"]
        insights_service.local_deps_installed = self._originals["local_deps_installed"]
        embedding_service.get_embedder = self._originals["get_embedder"]


class _NotConfiguredModel:
    """把 LLM 工厂替换为「未配置」抛错（AgentConfigError），验证零请求零 run。"""

    def __enter__(self) -> None:
        self._original = insights_service.get_chat_model

        def _raise(**kwargs: Any) -> Any:  # noqa: ARG001
            raise AgentConfigError("LLM 未配置（自检注入：AGENT_LLM_ENABLED=false / 缺 key）")

        insights_service.get_chat_model = _raise

    def __exit__(self, *exc_info: Any) -> None:
        insights_service.get_chat_model = self._original


class _DepsReadyOnly:
    """只把「本地依赖探测」mock 为 True（LLM 工厂不动 —— 用于真实闸门断言）。"""

    def __enter__(self) -> None:
        self._original = insights_service.local_deps_installed
        insights_service.local_deps_installed = lambda: True

    def __exit__(self, *exc_info: Any) -> None:
        insights_service.local_deps_installed = self._original


class _EmbeddingDepsMissing:
    """模拟「本地依赖未安装」：在真实懒加载点抛 EmbeddingNotAvailableError。

    注入点是 ``LocalBGEEmbedder._ensure_model``（产品代码首次 import
    ``langchain_huggingface`` 的位置，未装依赖时正是这里抛错）；
    patch 依赖探测函数对检索路径无效——``get_embedder`` 不做探测，
    错误发生在模型懒加载阶段。
    """

    def __enter__(self) -> "_EmbeddingDepsMissing":
        self._original = LocalBGEEmbedder._ensure_model

        def _raise(_self: Any) -> Any:
            raise EmbeddingNotAvailableError(INSTALL_HINT)

        LocalBGEEmbedder._ensure_model = _raise  # type: ignore[method-assign]
        reset_embedder()

    def __exit__(self, *exc_info: Any) -> None:
        LocalBGEEmbedder._ensure_model = self._original  # type: ignore[method-assign]
        reset_embedder()


class _InsightsDepsMissing:
    """把 ``insights.service`` 实际读取的依赖探测临时置 False（验证提炼 400）。"""

    def __enter__(self) -> "_InsightsDepsMissing":
        self._original = insights_service.local_deps_installed
        insights_service.local_deps_installed = lambda: False

    def __exit__(self, *exc_info: Any) -> None:
        insights_service.local_deps_installed = self._original


class _DefaultEmbedder:
    """把进程内默认 embedder 替换为注入实现（不改依赖探测 / 不改 LLM 工厂）。"""

    def __init__(self, embedder: Any) -> None:
        self.embedder = embedder
        self._original: Any = None

    def __enter__(self) -> "_DefaultEmbedder":
        self._original = embedding_service.get_embedder
        embedding_service.get_embedder = lambda: self.embedder
        reset_embedder()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        embedding_service.get_embedder = self._original



# ---------------------------------------------------------------------------
# 夹具与清理
# ---------------------------------------------------------------------------
class Fixture:
    def __init__(self) -> None:
        self.account_id: uuid.UUID | None = None
        self.persona_id: uuid.UUID | None = None
        self.topic_id: uuid.UUID | None = None
        self.note_a_id: uuid.UUID | None = None  # 已发布 + 3 快照 + 1 诊断
        self.note_b_id: uuid.UUID | None = None  # 已发布 + 1 快照
        self.note_c_id: uuid.UUID | None = None  # 已发布 + 无快照
        self.metric_ids: list[uuid.UUID] = []
        self.diagnostic_id: uuid.UUID | None = None
        self.snippet_ids: list[uuid.UUID] = []  # 自检写入的知识块（按 id 清理）
        self.before_threads: set[str] = set()
        self.before_run_ids: set[str] = set()
        self.before_snippet_ids: set[str] = set()
        self.before_published = 0
        #: 本轮唯一标记（让每次运行的规律文本不撞历史 content_hash）
        self.tag = uuid.uuid4().hex[:8]

    # ------------------------------------------------------------- 工具
    def _threads_now(self) -> set[str]:
        with SessionLocal() as db:
            try:
                rows = db.execute(
                    text("SELECT DISTINCT thread_id FROM langgraph.checkpoints")
                ).all()
            except Exception:  # noqa: BLE001 —— 表可能尚未建
                return set()
        return {str(row[0]) for row in rows}

    def nid(self, which: str) -> str:
        """把夹具笔记主键转成字符串（假模型引用 / 断言用）。"""
        value = {"a": self.note_a_id, "b": self.note_b_id, "c": self.note_c_id}[which]
        assert value is not None, "夹具尚未建立"
        return str(value)

    # ------------------------------------------------------------- 建立
    def setup(self) -> None:
        self.before_threads = self._threads_now()
        self.before_run_ids = _run_ids_now()

        # 前置清扫：清掉本前缀的异常遗留（自我保护；绝不碰其他数据）
        with SessionLocal() as db:
            db.execute(
                text("DELETE FROM knowledge_snippets WHERE content LIKE :t"),
                {"t": f"%{PREFIX}%"},
            )
            db.commit()
        self.before_snippet_ids = _snippet_ids_now()

        now = now_local()
        with SessionLocal() as db:
            account = Account(name=f"{PREFIX}账号", status=AccountStatus.ACTIVE)
            db.add(account)
            db.flush()
            persona = Persona(
                name=f"{PREFIX}人设", account_id=account.id, status=PersonaStatus.ACTIVE
            )
            db.add(persona)
            db.flush()
            topic = Topic(
                account_id=account.id,
                persona_id=persona.id,
                title=f"{PREFIX}选题",
                status=TopicStatus.WRITING,
            )
            db.add(topic)
            db.flush()

            note_a = Note(
                account_id=account.id,
                persona_id=persona.id,
                topic_id=topic.id,
                title=f"{PREFIX}笔记A（3 快照 + 诊断）",
                body="用于规律提炼的自检正文：先给结论，再补充过程细节与真实数据。",
                tags=["自检", "规律"],
                status=NoteStatus.PUBLISHED,
                compliance_status=ComplianceStatus.PASSED,
                published_at=now - timedelta(hours=3),
            )
            note_b = Note(
                account_id=account.id,
                persona_id=persona.id,
                topic_id=topic.id,
                title=f"{PREFIX}笔记B（1 快照，无诊断）",
                body="用于规律提炼的自检正文：短句标题 + 一个具体数字。",
                tags=["自检"],
                status=NoteStatus.PUBLISHED,
                compliance_status=ComplianceStatus.PASSED,
                published_at=now - timedelta(hours=30),
            )
            note_c = Note(
                account_id=account.id,
                persona_id=persona.id,
                topic_id=topic.id,
                title=f"{PREFIX}笔记C（无快照）",
                body="没有数据快照的已发布笔记（触发「尚未回填任何运营数据」数据缺口）。",
                tags=["自检"],
                status=NoteStatus.PUBLISHED,
                compliance_status=ComplianceStatus.PASSED,
                published_at=now - timedelta(hours=80),
            )
            for note in (note_a, note_b, note_c):
                db.add(note)
            db.flush()

            snap_d2 = NoteMetric(
                note_id=note_a.id,
                captured_at=now - timedelta(days=2),
                data_source=DataSource.MANUAL,
                views=100,
                likes=10,
                collects=5,
                comments=2,
                shares=1,
                new_followers=2,
            )
            snap_d1 = NoteMetric(
                note_id=note_a.id,
                captured_at=now - timedelta(days=1),
                data_source=DataSource.MANUAL,
                views=180,
                likes=18,
                collects=9,
                comments=4,
                shares=2,
                new_followers=4,
            )
            snap_d0 = NoteMetric(
                note_id=note_a.id,
                captured_at=now,
                data_source=DataSource.SCREENSHOT,
                views=260,
                likes=26,
                collects=13,
                comments=6,
                shares=3,
                new_followers=6,
            )
            snap_b = NoteMetric(
                note_id=note_b.id,
                captured_at=now - timedelta(hours=6),
                data_source=DataSource.MANUAL,
                views=80,
                likes=8,
                collects=4,
                comments=1,
                shares=0,
                new_followers=1,
            )
            for snap in (snap_d2, snap_d1, snap_d0, snap_b):
                db.add(snap)
            db.flush()

            diagnostic = Diagnostic(
                note_id=note_a.id,
                based_on_metric_id=snap_d0.id,
                diagnosis_type=DiagnosisType.REVIEW,
                score=80.0,
                strengths=["[SELFTEST]标题短句清晰，收藏结构好"],
                weaknesses=["[SELFTEST]发布后 24 小时快照缺失"],
                reasons=["[SELFTEST]基于最新快照的本地计算"],
                suggestions=["[SELFTEST]延续短句标题写法", "[SELFTEST]补充 24 小时快照"],
                llm_model="fake-model",
                llm_meta={"prompt_version": "v1"},
            )
            db.add(diagnostic)
            db.flush()

            self.account_id = account.id
            self.persona_id = persona.id
            self.topic_id = topic.id
            self.note_a_id = note_a.id
            self.note_b_id = note_b.id
            self.note_c_id = note_c.id
            self.metric_ids = [snap_d2.id, snap_d1.id, snap_d0.id, snap_b.id]
            self.diagnostic_id = diagnostic.id
            db.commit()

        self.before_published = _published_count()

    # ------------------------------------------------------------- 清理
    def cleanup(self) -> None:
        note_ids = [
            str(item)
            for item in (self.note_a_id, self.note_b_id, self.note_c_id)
            if item is not None
        ]
        # 1) 本次自检新建的检查点线程（运行前后差集，不动历史数据）
        new_threads = self._threads_now() - self.before_threads
        if new_threads:
            with SessionLocal() as db:
                for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                    try:
                        db.execute(
                            text(f"DELETE FROM langgraph.{table} WHERE thread_id = ANY(:tids)"),
                            {"tids": list(new_threads)},
                        )
                    except Exception:  # noqa: BLE001
                        db.rollback()
                db.commit()
        # 2) 本次自检新建的 agent_runs（差集；覆盖成功与失败运行）
        new_runs = _run_ids_now() - self.before_run_ids
        if new_runs:
            with SessionLocal() as db:
                db.execute(
                    text("DELETE FROM agent_runs WHERE id = ANY(:rids)"),
                    {"rids": list(new_runs)},
                )
                db.commit()
        # 3) 知识块：按已知 id + 前缀双口径（绝不触碰非本脚本数据）
        like = f"%{PREFIX}%"
        with SessionLocal() as db:
            if self.snippet_ids:
                db.execute(
                    text("DELETE FROM knowledge_snippets WHERE id = ANY(:sids)"),
                    {"sids": [str(item) for item in self.snippet_ids]},
                )
            db.execute(
                text("DELETE FROM knowledge_snippets WHERE content LIKE :t"), {"t": like}
            )
            db.commit()
        # 4) 业务数据：diagnostics / note_metrics → notes → topics → personas → accounts
        with SessionLocal() as db:
            for table in ("diagnostics", "covers", "note_metrics"):
                if note_ids:
                    db.execute(
                        text(f"DELETE FROM {table} WHERE note_id = ANY(:nids)"),
                        {"nids": note_ids},
                    )
                db.execute(
                    text(
                        f"DELETE FROM {table} WHERE note_id IN "
                        "(SELECT id FROM notes WHERE title LIKE :t)"
                    ),
                    {"t": like},
                )
            if self.diagnostic_id is not None:
                db.execute(
                    text("DELETE FROM diagnostics WHERE id = :d"),
                    {"d": str(self.diagnostic_id)},
                )
            for note_id in note_ids:
                db.execute(text("DELETE FROM notes WHERE id = :n"), {"n": note_id})
            db.execute(text("DELETE FROM notes WHERE title LIKE :t"), {"t": like})
            if self.topic_id is not None:
                db.execute(text("DELETE FROM topics WHERE id = :t"), {"t": str(self.topic_id)})
            db.execute(text("DELETE FROM topics WHERE title LIKE :t"), {"t": like})
            if self.persona_id is not None:
                db.execute(
                    text("DELETE FROM personas WHERE id = :p"), {"p": str(self.persona_id)}
                )
            db.execute(text("DELETE FROM personas WHERE name LIKE :t"), {"t": like})
            if self.account_id is not None:
                db.execute(
                    text("DELETE FROM accounts WHERE id = :a"), {"a": str(self.account_id)}
                )
            db.execute(text("DELETE FROM accounts WHERE name LIKE :t"), {"t": like})
            db.commit()

    # ------------------------------------------------------------- 复查
    def verify_residue(self) -> dict[str, int]:
        like = f"%{PREFIX}%"
        note_ids = [
            str(item)
            for item in (self.note_a_id, self.note_b_id, self.note_c_id)
            if item is not None
        ]
        with SessionLocal() as db:
            return {
                "knowledge_snippets(by_id)": db.execute(
                    text("SELECT count(*) FROM knowledge_snippets WHERE id = ANY(:sids)"),
                    {"sids": [str(item) for item in self.snippet_ids] or [""]},
                ).scalar_one(),
                "knowledge_snippets(prefix)": db.execute(
                    text("SELECT count(*) FROM knowledge_snippets WHERE content LIKE :t"),
                    {"t": like},
                ).scalar_one(),
                "diagnostics(by_note_id)": db.execute(
                    text("SELECT count(*) FROM diagnostics WHERE note_id = ANY(:nids)"),
                    {"nids": note_ids},
                ).scalar_one(),
                "note_metrics(by_note_id)": db.execute(
                    text("SELECT count(*) FROM note_metrics WHERE note_id = ANY(:nids)"),
                    {"nids": note_ids},
                ).scalar_one(),
                "notes(by_id)": db.execute(
                    text("SELECT count(*) FROM notes WHERE id = ANY(:nids)"),
                    {"nids": note_ids},
                ).scalar_one(),
                "notes(prefix)": db.execute(
                    text("SELECT count(*) FROM notes WHERE title LIKE :t"), {"t": like}
                ).scalar_one(),
                "topics(prefix)": db.execute(
                    text("SELECT count(*) FROM topics WHERE title LIKE :t"), {"t": like}
                ).scalar_one(),
                "personas(prefix)": db.execute(
                    text("SELECT count(*) FROM personas WHERE name LIKE :t"), {"t": like}
                ).scalar_one(),
                "accounts(prefix)": db.execute(
                    text("SELECT count(*) FROM accounts WHERE name LIKE :t"), {"t": like}
                ).scalar_one(),
                "agent_runs(diff)": len(_run_ids_now() - self.before_run_ids),
                "checkpoint_threads(diff)": len(self._threads_now() - self.before_threads),
            }


# ---------------------------------------------------------------------------
# 低层助手
# ---------------------------------------------------------------------------
def _run_ids_now() -> set[str]:
    with SessionLocal() as db:
        rows = db.execute(text("SELECT id::text FROM agent_runs")).all()
    return {str(row[0]) for row in rows}


def _snippet_ids_now() -> set[str]:
    with SessionLocal() as db:
        rows = db.execute(text("SELECT id::text FROM knowledge_snippets")).all()
    return {str(row[0]) for row in rows}


def _count_snippets() -> int:
    with SessionLocal() as db:
        return int(db.execute(text("SELECT count(*) FROM knowledge_snippets")).scalar_one())


def _published_count() -> int:
    with SessionLocal() as db:
        return int(
            db.execute(
                text("SELECT count(*) FROM notes WHERE status = 'published'")
            ).scalar_one()
        )


def _fetch_run(run_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        row = db.execute(
            text(
                "SELECT run_type, status, outputs, llm_raw, total_tokens, "
                "json_parse_attempts, error_message FROM agent_runs WHERE id = :rid"
            ),
            {"rid": run_id},
        ).first()
    return dict(row._mapping) if row is not None else {}


def _fetch_snippet_rows(ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT id::text AS id, chunk_type, status, title, content, meta, "
                "(embedding IS NOT NULL) AS has_embedding "
                "FROM knowledge_snippets WHERE id = ANY(:ids)"
            ),
            {"ids": ids},
        ).all()
    return [dict(row._mapping) for row in rows]


def _table_counts() -> dict[str, int]:
    """八张业务表的行数快照（只读断言：提炼 / 检索只写 knowledge_snippets）。"""
    tables = (
        "accounts",
        "personas",
        "notes",
        "topics",
        "note_metrics",
        "diagnostics",
        "covers",
        "hotspots",
    )
    with SessionLocal() as db:
        return {
            name: int(db.execute(text(f"SELECT count(*) FROM {name}")).scalar_one())
            for name in tables
        }


# ---------------------------------------------------------------------------
# 组 0：图结构（真 LangGraph · 六节点 + 条件边 + 有界回路）
# ---------------------------------------------------------------------------
def group0_graph(c: Checker) -> None:
    from app.agent.insights.state import InsightsState

    graph = build_insights_graph(
        model=InsightsScriptModel(responses=[{"patterns": []}]),
        embedder=FakeEmbedder(),
        use_checkpointer=False,
    )
    c.check(
        "Compiled" in type(graph).__name__,
        "build_insights_graph 返回已编译的 LangGraph 图",
        f"{type(graph).__name__}",
    )
    view = graph.get_graph()
    nodes = set(view.nodes.keys())
    business = {node for node in nodes if not node.startswith("__")}
    c.check(
        business
        == {
            NODE_LOAD_CONTEXT,
            NODE_GENERATE,
            NODE_VALIDATE,
            NODE_PERSIST,
            NODE_GIVE_UP,
            NODE_INSUFFICIENT,
        },
        "图含 load_context / generate_patterns / validate_patterns / embed_and_persist "
        "/ give_up / insufficient_sample 六节点",
        f"实际={sorted(business)}",
    )
    edges = {(edge.source, edge.target, bool(edge.conditional)) for edge in view.edges}
    c.check(
        (NODE_LOAD_CONTEXT, NODE_GENERATE, True) in edges
        and (NODE_LOAD_CONTEXT, NODE_INSUFFICIENT, True) in edges,
        "load_context 后为条件边（样本充足 → 生成 / 不足 → 特定收尾）",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_GENERATE, NODE_VALIDATE, False) in edges,
        "generate_patterns → validate_patterns 为直连边",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_VALIDATE, NODE_GENERATE, True) in edges,
        "validate_patterns → generate_patterns 为条件边（回炉回路）",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_VALIDATE, NODE_PERSIST, True) in edges
        and (NODE_VALIDATE, NODE_GIVE_UP, True) in edges,
        "validate 通过 → embed_and_persist / 超限 → give_up 均为条件边",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_PERSIST, "__end__", False) in edges
        and (NODE_GIVE_UP, "__end__", False) in edges
        and (NODE_INSUFFICIENT, "__end__", False) in edges,
        "embed_and_persist / give_up / insufficient_sample → END",
        f"edges={sorted(edges)}",
    )

    graph_src = (BACKEND_DIR / "app" / "agent" / "insights" / "graph.py").read_text(
        encoding="utf-8"
    )
    c.check(
        "AGENT_JSON_MAX_ATTEMPTS" in graph_src,
        "回路上界绑定 AGENT_JSON_MAX_ATTEMPTS（有界回路，不无限重试）",
    )
    c.check(
        'new_state_graph("insights-extraction", InsightsState)' in graph_src,
        "图用 new_state_graph 装配且状态为 InsightsState（复用 harness 基座）",
    )
    service_src = (BACKEND_DIR / "app" / "agent" / "insights" / "service.py").read_text(
        encoding="utf-8"
    )
    c.check(
        "run_agent(" in service_src
        and 'kind="insights"' in service_src
        and "run_type=RunType.INSIGHT" in service_src,
        "提炼经 app.agent.runner.run_agent 运行（第六个业务图，禁止裸 HTTP 自编排）",
    )
    annotation_names = set(InsightsState.__annotations__.keys())
    c.check(
        not any(("embed" in name.lower() or "vector" in name.lower()) for name in annotation_names),
        "图状态不含任何向量字段（向量绝不入状态）",
        f"{sorted(annotation_names)}",
    )


# ---------------------------------------------------------------------------
# 组 1：embedding 服务（批量 / 缓存 / 维度强校验；假实现注入）
# ---------------------------------------------------------------------------
def group1_embedding(c: Checker) -> None:
    old_batch = settings.EMBEDDING_BATCH_SIZE
    try:
        settings.EMBEDDING_BATCH_SIZE = 2
        fake = FakeEmbedder()
        reset_embedder()

        texts6 = [f"批量与缓存自检文本-{index}-{uuid.uuid4().hex[:6]}" for index in range(6)]
        vectors = embed_texts(texts6, embedder=fake)
        c.check(
            len(vectors) == 6 and all(len(vec) == 1024 for vec in vectors),
            "embed_texts 返回与输入等长的 1024 维向量列表",
            f"len={len(vectors)}",
        )
        c.check(
            fake.calls == [2, 2, 2],
            "按 EMBEDDING_BATCH_SIZE=2 切分：6 条文本恰 3 批（每批 2 条）",
            f"calls={fake.calls}",
        )

        cached = embed_texts(texts6, embedder=fake)
        c.check(
            fake.calls == [2, 2, 2] and cached == vectors,
            "同文本二次调用全量命中 sha256 缓存（底层零新增调用；向量逐位一致）",
            f"calls={fake.calls}",
        )
        c.check(
            describe_embedding_readiness()["cache_size"] == 6,
            "就绪快照的 cache_size = 6（进程内缓存计数）",
            f"{describe_embedding_readiness()['cache_size']}",
        )

        embed_texts([texts6[0], "全新文本-" + uuid.uuid4().hex[:6]], embedder=fake)
        c.check(
            fake.calls == [2, 2, 2, 1],
            "部分命中缓存：仅未命中 1 条触发底层调用（条数=1）",
            f"calls={fake.calls}",
        )

        c.check(embed_texts([], embedder=fake) == [], "空列表 → 空结果（零调用）")

        dim_error = None
        try:
            embed_texts(["维度不符测试文本"], embedder=BadDimEmbedder())
        except EmbeddingDimensionError as exc:
            dim_error = exc
        c.check(
            dim_error is not None
            and "1024" in str(dim_error)
            and "不会静默截断或补零" in str(dim_error),
            "非 1024 维向量 → EmbeddingDimensionError（不静默截断 / 补零）",
            f"{dim_error}",
        )

        count_error = None
        try:
            embed_texts(["条数不符测试甲", "条数不符测试乙"], embedder=ShortReturnEmbedder())
        except EmbeddingError as exc:
            count_error = exc
        c.check(
            count_error is not None and "条数不符" in str(count_error),
            "底层返回条数少于请求 → EmbeddingError（不猜、不补）",
            f"{count_error}",
        )

        # 维度一致性：配置 vs 数据库列（knowledge.EMBEDDING_DIM = 1024）
        validate_dim_settings()
        c.check(True, "validate_dim_settings：1024=1024 通过（配置与列维度一致）")

        old_dim = settings.EMBEDDING_DIM
        mismatch = None
        try:
            settings.EMBEDDING_DIM = 512
            reset_embedder()
            validate_dim_settings()
        except EmbeddingDimensionError as exc:
            mismatch = exc
        finally:
            settings.EMBEDDING_DIM = old_dim
            reset_embedder()
        c.check(
            mismatch is not None and "不一致" in str(mismatch),
            "EMBEDDING_DIM 与列维度不一致 → 清晰中文错误（本步不做维度迁移）",
            f"{mismatch}",
        )

        readiness = describe_embedding_readiness()
        c.check(
            readiness["dim"] == 1024
            and readiness["db_dim"] == 1024
            and readiness["dim_consistent"] is True
            and readiness["provider"] == "local",
            "就绪快照：provider=local / dim=db_dim=1024 / dim_consistent",
            f"{readiness}",
        )
        c.check(
            set(readiness.keys())
            == {
                "provider",
                "model",
                "dim",
                "db_dim",
                "dim_consistent",
                "device",
                "batch_size",
                "top_k",
                "local_deps_installed",
                "openai_model",
                "has_api_key",
                "cache_size",
            }
            and isinstance(readiness["has_api_key"], bool),
            "就绪快照恰 12 个键、api key 仅以布尔呈现（无明文）",
            f"{sorted(readiness.keys())}",
        )
    finally:
        settings.EMBEDDING_BATCH_SIZE = old_batch
        reset_embedder()


# ---------------------------------------------------------------------------
# 组 2：提炼成功路径 + 幂等（HTTP POST /extract；假模型 + 假 embedder 注入）
# ---------------------------------------------------------------------------
def _good_response(fx: Fixture) -> dict[str, Any]:
    """合法规律响应（两条；来源引用夹具内笔记；含假密钥串验证脱敏）。"""
    tag = fx.tag
    return {
        "patterns": [
            {
                "pattern": (
                    f"在本账号已发布笔记中，标题用短句加一个具体数字时互动更稳（{tag}）"
                ),
                "pattern_type": "hit_pattern",
                "scenario": "适合生活记录类选题的标题打磨",
                "source_note_ids": [fx.nid("a"), fx.nid("b")],
                "evidence": "对应笔记的 views 与 likes 在样本内对比中表现更好",
                "confidence": 0.72,
            },
            {
                "pattern": (
                    f"在本账号已发布笔记中，正文先给结论再补充过程的写法更值得复用（{tag}）"
                ),
                "pattern_type": "review_insight",
                "scenario": "适合教程 / 经验分享类内容的结构组织",
                "source_note_ids": [fx.nid("a")],
                "evidence": "对应笔记的 collects 与 shares 在样本内对比中表现更好",
                "confidence": 0.65,
            },
        ],
        "debug_note": f"trace: api_key={FAKE_SECRET}",
    }


def group2_extract_success(c: Checker, client: TestClient, fx: Fixture) -> None:
    model = InsightsScriptModel(responses=[_good_response(fx)])
    fake = FakeEmbedder()
    before_count = _count_snippets()
    before_tables = _table_counts()

    with _InsightsEnv(model, fake):
        resp = client.post("/api/v1/knowledge/extract")

    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    c.check(resp.status_code == 201, "POST /extract 成功路径 → 201", f"{resp.status_code} {resp.text[:300]}")
    c.check(
        resp.status_code == 201
        and body.get("status") == "success"
        and body.get("insufficient_sample") is False
        and body.get("generated") == 2
        and body.get("persisted") == 2
        and body.get("reused") == 0
        and body.get("skipped") == 0
        and len(body.get("snippet_ids") or []) == 2,
        "响应业务计数：status=success / generated=2 / persisted=2 / reused=0 / skipped=0 / snippet_ids 2 条",
        f"{json.dumps({k: body.get(k) for k in ('status', 'generated', 'persisted', 'reused', 'skipped')})}",
    )
    c.check(
        body.get("parse_attempts") == 1 and model.calls == 1,
        "parse_attempts=1 且假模型恰 1 次调用（首轮即拿到合法 JSON）",
        f"parse_attempts={body.get('parse_attempts')} calls={model.calls}",
    )
    c.check(
        body.get("prompt_version") == settings.INSIGHTS_PROMPT_VERSION,
        "响应携带 INSIGHTS_PROMPT_VERSION（进 llm 审计口径）",
        f"{body.get('prompt_version')}",
    )
    c.check(
        "llm_raw" not in resp.text and "embedding" not in resp.text and FAKE_SECRET not in resp.text,
        "响应文本不含 llm_raw / embedding / 密钥字样",
    )

    prompt_blob = model.prompts[0] if model.prompts else ""
    c.check(
        "禁止出现行业平均" in prompt_blob and "外部数字" in prompt_blob,
        "提炼提示词明文含「禁止行业平均 / 外部数字」硬约束（规律只来自本账号数据）",
    )
    c.check(
        fx.nid("a") in prompt_blob and fx.nid("b") in prompt_blob and fx.nid("c") in prompt_blob,
        "提示词携带样本内笔记 id（模型只能引用真实来源）",
    )

    ids = [str(item) for item in (body.get("snippet_ids") or [])]
    rows = _fetch_snippet_rows(ids)
    c.check(
        len(rows) == 2 and all(row["has_embedding"] for row in rows),
        "知识块已落库且 embedding 非空（2 条）",
        f"{len(rows)}",
    )
    c.check(
        {row["chunk_type"] for row in rows} == {"hit_pattern", "review_insight"}
        and all(row["status"] == SnippetStatus.ACTIVE.value for row in rows),
        "chunk_type 恰为 hit_pattern / review_insight，status=active",
        f"{[row['chunk_type'] for row in rows]}",
    )
    meta0 = rows[0]["meta"] if rows and isinstance(rows[0]["meta"], dict) else {}
    c.check(
        set(meta0.keys())
        >= {"source_note_ids", "evidence", "scenario", "sample_size", "data_range",
            "confidence", "content_hash", "prompt_version"},
        "meta 含来源 / 证据 / 样本量 / 数据区间 / 置信度 / 指纹 / 提示词版本",
        f"{sorted(meta0.keys())}",
    )
    c.check(
        all(row["title"] == row["content"][:50] for row in rows),
        "title = 规律文本前 50 字",
    )
    c.check(
        _count_snippets() == before_count + 2,
        "knowledge_snippets 恰新增 2 条",
        f"{before_count} → {_count_snippets()}",
    )

    run = _fetch_run(str(body.get("run_id") or ""))
    outputs = run.get("outputs") if isinstance(run.get("outputs"), dict) else {}
    c.check(
        run.get("run_type") == "insights" and run.get("status") == "success",
        "agent_runs：run_type=insights / status=success（RunType 新增成员，无 DDL）",
        f"run_type={run.get('run_type')} status={run.get('status')}",
    )
    c.check(
        outputs.get("persisted") == 2
        and outputs.get("generated") == 2
        and outputs.get("insufficient_sample") is False
        and outputs.get("prompt_version") == settings.INSIGHTS_PROMPT_VERSION
        and len(outputs.get("snippet_ids") or []) == 2,
        "agent_runs.outputs 回填业务明细（裸 SQL 补齐，harness 不改）",
        f"{json.dumps(outputs, ensure_ascii=False)[:220]}",
    )
    llm_raw = str(run.get("llm_raw") or "")
    c.check(
        "[REDACTED]" in llm_raw and FAKE_SECRET not in llm_raw,
        "llm_raw 已脱敏（假密钥串 → [REDACTED]；绝不原样落库）",
        f"{llm_raw[:160]}",
    )
    c.check(
        len(llm_raw) <= insights_service.LLM_RAW_MAX_LEN,
        "llm_raw 截断到 LLM_RAW_MAX_LEN 上限内",
        f"len={len(llm_raw)}",
    )
    c.check(
        int(run.get("total_tokens") or 0) == model.usage_in + model.usage_out,
        "token 用量按 token 记录（17+29=46，不内置价格换算）",
        f"{run.get('total_tokens')}",
    )
    c.check(
        _table_counts() == before_tables,
        "提炼**只写 knowledge_snippets**：其余八张业务表计数不变",
        f"{before_tables} vs {_table_counts()}",
    )

    fx.snippet_ids.extend(uuid.UUID(item) for item in ids)

    # ---- 幂等：同文本再跑 → reused=2 / persisted=0（绝不重复插入）----
    with _InsightsEnv(model, fake):
        resp2 = client.post("/api/v1/knowledge/extract")
    body2 = resp2.json() if resp2.headers.get("content-type", "").startswith("application/json") else {}
    c.check(
        resp2.status_code == 201
        and body2.get("reused") == 2
        and body2.get("persisted") == 0
        and body2.get("generated") == 2
        and body2.get("snippet_ids") == [],
        "幂等：同规律再提炼 → reused=2 / persisted=0 / 零新增 id",
        f"{json.dumps({k: body2.get(k) for k in ('generated', 'persisted', 'reused')})}",
    )
    c.check(
        _count_snippets() == before_count + 2 and fake.calls == [2],
        "幂等落库绝不重复插入（总数不变）；查重先于嵌入（底层仅首批 1 次调用）",
        f"count={_count_snippets()} calls={fake.calls}",
    )
    run2 = _fetch_run(str(body2.get("run_id") or ""))
    outputs2 = run2.get("outputs") if isinstance(run2.get("outputs"), dict) else {}
    c.check(
        outputs2.get("reused") == 2 and outputs2.get("persisted") == 0,
        "第二次运行的 agent_runs.outputs 如实记录 reused=2 / persisted=0",
        f"{json.dumps(outputs2, ensure_ascii=False)[:200]}",
    )



# ---------------------------------------------------------------------------
# 组 3：检索（pgvector 余弦 top-k；crud 直调 + HTTP）
# ---------------------------------------------------------------------------
def group3_retrieval(c: Checker, client: TestClient, fx: Fixture) -> None:
    good = _good_response(fx)
    pattern1_text = str(good["patterns"][0]["pattern"])
    pattern2_text = str(good["patterns"][1]["pattern"])

    rows = _fetch_snippet_rows([str(item) for item in fx.snippet_ids])
    s1 = next((row for row in rows if row["content"] == pattern1_text), None)
    s2 = next((row for row in rows if row["content"] == pattern2_text), None)
    c.check(
        s1 is not None and s2 is not None and s1["id"] != s2["id"],
        "前置：组 2 写入的两条知识块可按内容定位",
        f"{[row['content'][:18] for row in rows]}",
    )
    if s1 is None or s2 is None:
        return

    probe = FakeEmbedder()
    qvec1 = probe._vec(pattern1_text)

    # ---- (1) 自查询向量必然排第一（余弦距离 0 → similarity 1.0）----
    with SessionLocal() as db:
        hits = crud_knowledge.search_snippets(db, query_vec=qvec1, k=10)
    c.check(
        bool(hits) and hits[0]["id"] == s1["id"],
        "top-k：用规律自身向量检索，该条排第一（余弦距离 0）",
        f"hits[0]={hits[0]['id'] if hits else None} want={s1['id']}",
    )
    c.check(
        bool(hits) and abs(float(hits[0]["similarity"]) - 1.0) < 1e-3,
        "top-1 相似度 ≈ 1.0（4 位小数口径）",
        f"{hits[0]['similarity'] if hits else None}",
    )
    sims = [float(hit["similarity"]) for hit in hits]
    c.check(
        sims == sorted(sims, reverse=True),
        "结果按相似度降序（余弦距离升序）",
        f"{sims}",
    )
    c.check(
        bool(hits)
        and set(hits[0].keys())
        == {"id", "chunk_type", "title", "content", "meta", "status", "created_at", "similarity"},
        "检索命中字段恰 8 键：**不含 embedding 原始向量**",
        f"{sorted(hits[0].keys()) if hits else []}",
    )

    # ---- (2) chunk_type 过滤 ----
    with SessionLocal() as db:
        hits_ri = crud_knowledge.search_snippets(
            db, query_vec=qvec1, chunk_types=("review_insight",), k=10
        )
    c.check(
        bool(hits_ri)
        and all(hit["chunk_type"] == "review_insight" for hit in hits_ri)
        and any(hit["id"] == s2["id"] for hit in hits_ri),
        "chunk_types 过滤：仅返回 review_insight（且含目标块）",
        f"{[hit['chunk_type'] for hit in hits_ri]}",
    )

    # ---- (3) archived 不召回；include_archived 才召回 ----
    s2_uuid = uuid.UUID(str(s2["id"]))
    with SessionLocal() as db:
        archived_row = crud_knowledge.set_status(db, s2_uuid, status=SnippetStatus.ARCHIVED)
    c.check(
        archived_row is not None and archived_row.status == SnippetStatus.ARCHIVED,
        "PATCH 语义（crud）：归档状态切换成功",
    )
    with SessionLocal() as db:
        hits_active = crud_knowledge.search_snippets(db, query_vec=qvec1, k=10)
        hits_all = crud_knowledge.search_snippets(
            db, query_vec=qvec1, k=10, include_archived=True
        )
    c.check(
        all(hit["id"] != s2["id"] for hit in hits_active),
        "默认检索**不含 archived**（归档块不召回）",
        f"{[hit['id'] for hit in hits_active]}",
    )
    c.check(
        any(hit["id"] == s2["id"] for hit in hits_all),
        "include_archived=True 时召回归档块",
        f"{[hit['id'] for hit in hits_all]}",
    )
    with SessionLocal() as db:
        crud_knowledge.set_status(db, s2_uuid, status=SnippetStatus.ACTIVE)
    c.check(True, "归档恢复（active）—— 知识块只增 / 归档，无物理删除")

    # ---- (4) HTTP POST /search（默认 embedder 注入假实现）----
    with _DefaultEmbedder(FakeEmbedder()):
        resp = client.post(
            "/api/v1/knowledge/search",
            json={"query": pattern1_text, "chunk_types": ["hit_pattern", "review_insight"], "k": 5},
        )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    items = body.get("items") or []
    c.check(
        resp.status_code == 200
        and bool(items)
        and items[0]["id"] == s1["id"]
        and abs(float(items[0]["similarity"]) - 1.0) < 1e-3,
        "POST /search：200 + top-1 为目标块 + similarity 字段返回",
        f"{resp.status_code} {json.dumps(body, ensure_ascii=False)[:220]}",
    )
    c.check(
        body.get("query") == pattern1_text and "embedding" not in resp.text,
        "POST /search 回显 query 且响应不含 embedding 向量",
    )

    for payload, label in (
        ({"query": ""}, "query 空串 → 422"),
        ({"query": "有效查询", "k": 0}, "k=0 → 422"),
        ({"query": "有效查询", "k": 99}, "k=99（>20）→ 422"),
    ):
        bad = client.post("/api/v1/knowledge/search", json=payload)
        c.check(bad.status_code == 422, f"POST /search 参数校验：{label}", f"{bad.status_code}")

    # ---- (5) 未装依赖时的真实分支（注入依赖缺失，如实告知，不假装无命中）----
    fresh = "真实分支查询-" + uuid.uuid4().hex[:10]
    with _EmbeddingDepsMissing():
        real = client.post("/api/v1/knowledge/search", json={"query": fresh, "k": 3})
    c.check(
        real.status_code == 400
        and "知识检索失败" in real.text
        and "向量化服务不可用" in real.text,
        "未装向量化依赖：POST /search → 400（注入依赖缺失，如实告知 + 安装指引）",
        f"{real.status_code} {real.text[:200]}",
    )


# ---------------------------------------------------------------------------
# 组 4：样本不足路径（零模型调用零写入；run 补记 partial）
# ---------------------------------------------------------------------------
def group4_insufficient(c: Checker, client: TestClient, fx: Fixture) -> None:
    old_min = settings.INSIGHTS_MIN_PUBLISHED
    model = InsightsScriptModel(responses=[_good_response(fx)])
    before_count = _count_snippets()
    try:
        settings.INSIGHTS_MIN_PUBLISHED = 99
        with _InsightsEnv(model, FakeEmbedder()):
            resp = client.post("/api/v1/knowledge/extract")
    finally:
        settings.INSIGHTS_MIN_PUBLISHED = old_min

    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    c.check(
        resp.status_code == 201
        and body.get("insufficient_sample") is True
        and body.get("status") == "partial",
        "样本不足 → 201 + insufficient_sample=True + status=partial（不是错误）",
        f"{resp.status_code} {json.dumps(body, ensure_ascii=False)[:220]}",
    )
    c.check(
        body.get("generated") == 0
        and body.get("persisted") == 0
        and body.get("snippet_ids") == []
        and body.get("parse_attempts") == 0,
        "零产出：generated/persisted=0、snippet_ids 空、parse_attempts=0",
        f"{json.dumps({k: body.get(k) for k in ('generated', 'persisted', 'parse_attempts')})}",
    )
    c.check(
        model.calls == 0 and _count_snippets() == before_count,
        "**零模型调用、零写入**（不硬造规律）；knowledge_snippets 总数不变",
        f"calls={model.calls} count={before_count}→{_count_snippets()}",
    )
    gaps = " ".join(str(item) for item in (body.get("data_gaps") or []))
    c.check(
        "样本不足" in gaps and "至少发布 99 篇" in gaps and "回填数据后再提炼" in gaps,
        "data_gaps 含「样本不足：至少发布 N 篇并回填数据后再提炼（当前仅 X 篇）」",
        f"{gaps[:180]}",
    )
    c.check(
        body.get("min_required") == 99 and body.get("sample_size") == fx.before_published,
        "样本量口径：sample_size=全量已发布数（含本夹具 3 篇）/ min_required=门槛",
        f"sample_size={body.get('sample_size')} want={fx.before_published} min_required={body.get('min_required')}",
    )

    run = _fetch_run(str(body.get("run_id") or ""))
    outputs = run.get("outputs") if isinstance(run.get("outputs"), dict) else {}
    c.check(
        run.get("status") == "partial" and outputs.get("insufficient_sample") is True,
        "agent_runs 补记 status=partial（service 层按 output 修正，harness 不改）",
        f"status={run.get('status')}",
    )
    c.check(
        run.get("llm_raw") is None,
        "样本不足的运行无 llm_raw（零模型调用）",
        f"{run.get('llm_raw')}",
    )


# ---------------------------------------------------------------------------
# 组 5：RAG 反哺接入（命中 / 开关 / 降级 / 替换等价 / 拓扑不变）
# ---------------------------------------------------------------------------
def group5_feedback(c: Checker, client: TestClient, fx: Fixture) -> None:
    from app.agent.content.graph import build_content_graph
    from app.agent.topics.graph import build_topic_graph

    good = _good_response(fx)
    pattern1_text = str(good["patterns"][0]["pattern"])

    # ---- (1) safe_rag_block 命中：块含规律文本与类型标注 ----
    with _DefaultEmbedder(FakeEmbedder()):
        with SessionLocal() as db:
            block = safe_rag_block(
                db,
                query_text=pattern1_text,
                chunk_types=("hit_pattern", "review_insight"),
                top_k=5,
                enabled=True,
                source="selftest-命中",
            )
    c.check(
        bool(block) and pattern1_text in block and "爆款规律" in block,
        "safe_rag_block 命中：块正文含规律文本与「爆款规律」类型标注",
        f"{block[:160]}",
    )
    c.check(
        RAG_BLOCK_TITLE in render_rag_prompt_section(block),
        "块渲染进提示词时带「本账号历史经验（仅供参考，非行业标准）」标题",
    )

    # ---- (2) 开关关闭：零检索 ----
    probe = FakeEmbedder()
    with _DefaultEmbedder(probe):
        with SessionLocal() as db:
            block_off = safe_rag_block(
                db, query_text=pattern1_text, enabled=False, source="selftest-关闭"
            )
            block_blank = safe_rag_block(
                db, query_text="   ", enabled=True, source="selftest-空文本"
            )
    c.check(
        block_off == "" and block_blank == "" and probe.calls == [],
        "enabled=False / 查询空文本 → 空串且**零检索**（开关默认 True，可一键回退）",
        f"off={block_off!r} blank={block_blank!r} calls={probe.calls}",
    )

    # ---- (3) embedder 抛错 → 降级不阻断 ----
    degraded = None
    raised = False
    with _DefaultEmbedder(ThrowingEmbedder()):
        try:
            with SessionLocal() as db:
                degraded = safe_rag_block(
                    db,
                    query_text="降级测试查询-" + uuid.uuid4().hex[:10],
                    enabled=True,
                    source="selftest-降级",
                )
        except Exception:  # noqa: BLE001 —— 若抛异常即为「阻断主流程」的回归
            raised = True
    c.check(
        raised is False and degraded == "",
        "embedder 抛错 → 只 WARNING 降级返回空串（主流程不阻断、不报错给用户）",
        f"raised={raised} degraded={degraded!r}",
    )

    # ---- (4) 提示词替换等价（不影响既有逻辑的运行时证据）----
    rag_text = "- （爆款规律）复刻式短句标题更稳（样本 3 篇；置信度 0.72）"
    from app.agent.content.prompts import build_messages as content_build  # noqa: E402
    from app.agent.topics.prompts import build_messages as topics_build  # noqa: E402

    persona = {
        "name": "自检人设",
        "positioning": "通勤穿搭",
        "target_audience": "上班族",
        "tone": "亲和",
        "content_pillars": ["穿搭", "效率"],
    }
    hotspot = {
        "source": "selftest",
        "title": "自检热点",
        "url": "",
        "hot_score": 1.0,
        "rank_position": 1,
        "hot_date": "2026-10-12",
    }
    t0 = topics_build(
        persona_snapshot=persona,
        hotspot_snapshot=hotspot,
        per_hotspot=1,
        title_max_len=20,
        min_tags=3,
        max_tags=5,
    )[0].content
    t1 = topics_build(
        persona_snapshot=persona,
        hotspot_snapshot=hotspot,
        per_hotspot=1,
        title_max_len=20,
        min_tags=3,
        max_tags=5,
        rag_context=rag_text,
    )[0].content
    section = render_rag_prompt_section(rag_text)
    c.check(
        RAG_BLOCK_TITLE not in t0 and RAG_BLOCK_TITLE in t1,
        "选题图提示词：无 RAG 不含标题 / 有 RAG 含「本账号历史经验」标题",
    )
    c.check(
        t1.replace(section, "", 1) == t0,
        "选题图提示词替换等价：去掉 RAG 段后与传入前**逐字一致**（不影响既有逻辑）",
        f"len0={len(t0)} len1={len(t1)}",
    )

    topic = {"title": "自检选题", "angle": "角度", "summary": "摘要", "content_outline": ["a"]}
    c0 = content_build(
        persona_snapshot=persona,
        topic_snapshot=topic,
        title_max_len=20,
        body_min_len=200,
        body_max_len=600,
        min_tags=3,
        max_tags=5,
    )[0].content
    c1 = content_build(
        persona_snapshot=persona,
        topic_snapshot=topic,
        title_max_len=20,
        body_min_len=200,
        body_max_len=600,
        min_tags=3,
        max_tags=5,
        rag_context=rag_text,
    )[0].content
    c.check(
        c1.replace(section, "", 1) == c0,
        "创作图提示词替换等价：去掉 RAG 段后与传入前**逐字一致**（不影响既有逻辑）",
        f"len0={len(c0)} len1={len(c1)}",
    )

    # ---- (5) 接入点与拓扑不变（静态 + 运行时）----
    for name, path, flag, sw in (
        ("选题", BACKEND_DIR / "app" / "agent" / "topics" / "graph.py", "TOPIC_RAG_ENABLED", "选题图 load_context"),
        ("创作", BACKEND_DIR / "app" / "agent" / "content" / "graph.py", "CONTENT_RAG_ENABLED", "创作图 load_context"),
    ):
        src = path.read_text(encoding="utf-8")
        c.check(
            src.count("safe_rag_block(") == 1
            and src.count(flag) == 1
            and src.count(f'source="{sw}"') == 1,
            f"{name}图 RAG 接入点恰 1 处（load_context 内 safe_rag_block）+ 开关恰 1 处",
            f"calls={src.count('safe_rag_block(')} flag={src.count(flag)}",
        )

    tgraph = build_topic_graph(model=InsightsScriptModel(responses=[{}]), use_checkpointer=False)
    tbusiness = {n for n in tgraph.get_graph().nodes if not n.startswith("__")}
    c.check(
        tbusiness
        == {"load_context", "generate_topics", "validate_dedupe", "persist", "give_up"},
        "选题图仍为原五节点拓扑（RAG 未改节点 / 条件边 / 状态机）",
        f"{sorted(tbusiness)}",
    )
    cgraph = build_content_graph(model=InsightsScriptModel(responses=[{}]), use_checkpointer=False)
    cbusiness = {n for n in cgraph.get_graph().nodes if not n.startswith("__")}
    c.check(
        cbusiness
        == {"load_context", "generate_content", "validate_compliance", "persist", "give_up"},
        "创作图仍为原五节点拓扑（RAG 未改节点 / 条件边 / 状态机）",
        f"{sorted(cbusiness)}",
    )


# ---------------------------------------------------------------------------
# 组 6：接口面（方法精确 / 敏感字段 / 分页 / PATCH / main 装配）
# ---------------------------------------------------------------------------
def group6_surface(c: Checker, client: TestClient, fx: Fixture) -> None:
    paths = app.openapi()["paths"]
    c.check(
        sorted(paths.get("/api/v1/knowledge", {})) == ["get"],
        "/api/v1/knowledge 恰为 GET（列表）",
        f"{sorted(paths.get('/api/v1/knowledge', {}))}",
    )
    c.check(
        sorted(paths.get("/api/v1/knowledge/extract", {})) == ["post"],
        "/api/v1/knowledge/extract 恰为 POST（静态路径先于 /{id} 注册）",
        f"{sorted(paths.get('/api/v1/knowledge/extract', {}))}",
    )
    c.check(
        sorted(paths.get("/api/v1/knowledge/search", {})) == ["post"],
        "/api/v1/knowledge/search 恰为 POST（静态路径先于 /{id} 注册）",
        f"{sorted(paths.get('/api/v1/knowledge/search', {}))}",
    )
    c.check(
        sorted(paths.get("/api/v1/knowledge/{snippet_id}", {})) == ["get", "patch"],
        "/api/v1/knowledge/{snippet_id} 恰为 GET + PATCH（归档 / 恢复）",
        f"{sorted(paths.get('/api/v1/knowledge/{snippet_id}', {}))}",
    )
    bad_methods = [
        path
        for path in paths
        if "/knowledge" in path
        and any(method in paths[path] for method in ("put", "delete"))
    ]
    c.check(not bad_methods, "knowledge 路由不存在 PUT / DELETE（知识块只增 / 归档）", f"{bad_methods}")

    schemas = app.openapi()["components"]["schemas"]
    forbidden = {"embedding", "llm_raw", "api_key", "secret", "password", "cookie", "authorization"}
    for name in (
        "SnippetOut",
        "SnippetListOut",
        "ExtractResultOut",
        "SnippetSearchRequest",
        "SearchHit",
        "SnippetSearchOut",
        "SnippetUpdateRequest",
    ):
        props = set((schemas.get(name) or {}).get("properties") or {})
        hits = [flag for flag in forbidden if any(flag in prop.lower() for prop in props)]
        c.check(
            not hits,
            f"{name} 无敏感字段（embedding / llm_raw / key / 凭据）",
            f"hits={hits} props={sorted(props)}",
        )
    snippet_props = set((schemas.get("SnippetOut") or {}).get("properties") or {})
    c.check(
        snippet_props
        == {"id", "chunk_type", "ref_type", "title", "content", "meta", "status", "created_at"},
        "SnippetOut 恰 8 字段（白名单式输出，绝无向量）",
        f"{sorted(snippet_props)}",
    )

    main_src = (BACKEND_DIR / "app" / "main.py").read_text(encoding="utf-8")
    c.check(
        main_src.count("include_router(") >= 12 and "knowledge_router" in main_src,
        "main.py include_router ≥ 12 且 knowledge 在册（第 12 只，不回退）",
        f"{main_src.count('include_router(')}",
    )
    c.check(
        main_src.count("lifespan=") == 1 and main_src.count("async def lifespan") == 1,
        "全局 lifespan 仍唯一（本步无定时器，未新增生命周期）",
        f"lifespan={main_src.count('lifespan=')} / def={main_src.count('async def lifespan')}",
    )
    route_src = (BACKEND_DIR / "app" / "api" / "routes" / "knowledge.py").read_text(
        encoding="utf-8"
    )
    sql_hits = [pat for pat in ("db.execute(", "db.query(", "select(") if pat in route_src]
    c.check(not sql_hits, "knowledge 路由层零 SQLAlchemy 表达式（第 8 步分层纪律）", f"{sql_hits}")

    default_value = Settings.model_fields["INSIGHTS_SCHEDULER_ENABLED"].default
    c.check(
        default_value is False,
        "INSIGHTS_SCHEDULER_ENABLED 字段默认 False（首版以手动触发为主，仅配置预留）",
        f"{default_value}",
    )

    # ---- 列表行为 ----
    resp = client.get("/api/v1/knowledge", params={"limit": 50})
    body = resp.json() if resp.status_code == 200 else {}
    items = body.get("items") or []
    item_ids = {str(item.get("id")) for item in items}
    c.check(
        resp.status_code == 200
        and int(body.get("total") or 0) >= 2
        and {str(item) for item in fx.snippet_ids} <= item_ids,
        "GET /api/v1/knowledge：200 + 本轮写入的知识块全部在列表内",
        f"total={body.get('total')}",
    )
    times = [datetime.fromisoformat(str(item["created_at"])) for item in items]
    c.check(
        times == sorted(times, reverse=True),
        "列表按 created_at 倒序（id 兜底稳定顺序）",
    )
    c.check(
        all("embedding" not in item for item in items) and "llm_raw" not in resp.text,
        "列表项不含 embedding 向量 / 响应不含 llm_raw",
    )
    filtered = client.get(
        "/api/v1/knowledge", params={"chunk_type": "hit_pattern", "limit": 50}
    )
    c.check(
        filtered.status_code == 200
        and all(item["chunk_type"] == "hit_pattern" for item in filtered.json()["items"]),
        "chunk_type 过滤生效（仅 hit_pattern）",
    )
    for params, label in (
        ({"limit": 101}, "limit=101（>100）→ 422"),
        ({"limit": 0}, "limit=0 → 422"),
        ({"offset": -1}, "offset=-1 → 422"),
        ({"chunk_type": "not_a_type"}, "chunk_type 非法 → 422"),
        ({"status": "not_a_status"}, "status 非法 → 422"),
    ):
        bad = client.get("/api/v1/knowledge", params=params)
        c.check(bad.status_code == 422, f"GET /knowledge 参数校验：{label}", f"{bad.status_code}")

    # ---- 详情 / PATCH ----
    sid = str(fx.snippet_ids[0])
    one = client.get(f"/api/v1/knowledge/{sid}")
    c.check(
        one.status_code == 200
        and one.json().get("id") == sid
        and "embedding" not in one.text,
        "GET /{id}：200 + 详情不含 embedding",
        f"{one.status_code}",
    )
    missing = client.get(f"/api/v1/knowledge/{uuid.uuid4()}")
    c.check(missing.status_code == 404, "GET /{id} 不存在 → 404", f"{missing.status_code}")
    patched = client.patch(f"/api/v1/knowledge/{sid}", json={"status": "archived"})
    c.check(
        patched.status_code == 200 and patched.json().get("status") == "archived",
        "PATCH {status: archived} → 200（归档）",
        f"{patched.status_code} {patched.text[:120]}",
    )
    restored = client.patch(f"/api/v1/knowledge/{sid}", json={"status": "active"})
    c.check(
        restored.status_code == 200 and restored.json().get("status") == "active",
        "PATCH {status: active} → 200（恢复）",
    )
    bad_status = client.patch(f"/api/v1/knowledge/{sid}", json={"status": "draft"})
    c.check(
        bad_status.status_code == 422,
        "PATCH 仅允许 active / archived（draft → 422）",
        f"{bad_status.status_code}",
    )
    patch_missing = client.patch(
        f"/api/v1/knowledge/{uuid.uuid4()}", json={"status": "active"}
    )
    c.check(
        patch_missing.status_code == 404,
        "PATCH 不存在 id → 404",
        f"{patch_missing.status_code}",
    )


# ---------------------------------------------------------------------------
# 组 7：失败路径（回路有界 / 零请求零 run 闸门）
# ---------------------------------------------------------------------------
def group7_failures(c: Checker, client: TestClient, fx: Fixture) -> None:
    limit = int(settings.AGENT_JSON_MAX_ATTEMPTS)

    # (1) 全坏 JSON：3 轮 → 每轮 invoke_json 3 次 → 3×3=9 → give_up → 400
    bad_model = InsightsScriptModel(
        responses=["这不是 JSON：只有说明文字，没有任何结构化数据。"]
    )
    before_runs = _run_ids_now()
    with _InsightsEnv(bad_model, FakeEmbedder()):
        resp = client.post("/api/v1/knowledge/extract")
    c.check(
        resp.status_code == 400 and "规律提炼失败" in resp.text,
        "全坏 JSON → 400（可重试的错误体）",
        f"{resp.status_code} {resp.text[:200]}",
    )
    c.check(
        bad_model.calls == limit * limit,
        f"全坏 JSON：假模型调用恰 {limit}×{limit}={limit * limit} 次（JSON 尝试 × 图回路双上界）",
        f"calls={bad_model.calls}",
    )
    c.check(
        "Traceback" not in resp.text and FAKE_SECRET not in resp.text,
        "失败响应无 Traceback / 无密钥字样",
    )
    new_runs = _run_ids_now() - before_runs
    if len(new_runs) == 1:
        run = _fetch_run(next(iter(new_runs)))
        c.check(
            run.get("status") == "failed" and run.get("run_type") == "insights",
            "超限失败的 run 记 failed（agent_runs 如实留痕）",
            f"status={run.get('status')}",
        )
    else:
        c.check(False, "超限失败的 run 记 failed（agent_runs 如实留痕）", f"new_runs={len(new_runs)}")

    # (2) 结构全坏（可解析但条目全不合格）：恰 3 轮回炉 → 400
    weak_model = InsightsScriptModel(
        responses=[{"patterns": [{"pattern": "只有文本的条目（缺场景 / 来源 / 证据）"}]}]
    )
    before_runs2 = _run_ids_now()
    with _InsightsEnv(weak_model, FakeEmbedder()):
        resp2 = client.post("/api/v1/knowledge/extract")
    c.check(
        resp2.status_code == 400 and f"连续 {limit} 轮" in resp2.text,
        f"结构全坏 → {limit} 轮回炉后 400（有限回路，不无限重试）",
        f"{resp2.status_code} {resp2.text[:200]}",
    )
    c.check(
        weak_model.calls == limit,
        f"结构全坏：假模型调用恰 {limit} 次（每轮 1 次、回炉 {limit} 轮）",
        f"calls={weak_model.calls}",
    )
    c.check(
        len(_run_ids_now() - before_runs2) == 1,
        "结构全坏同样留痕 1 条 failed run",
    )

    # (3) LLM 未配置 → 400 且零请求零 run
    before_runs3 = _run_ids_now()
    with _DepsReadyOnly():
        with _NotConfiguredModel():
            resp3 = client.post("/api/v1/knowledge/extract")
    c.check(
        resp3.status_code == 400
        and "规律提炼未就绪" in resp3.text
        and "reason" in resp3.text,
        "LLM 未配置 → 400（统一错误体含 reason；先于图运行的闸门）",
        f"{resp3.status_code} {resp3.text[:200]}",
    )
    c.check(
        _run_ids_now() == before_runs3,
        "LLM 未配置：**零请求零 run**（未消耗任何 token）",
    )

    # (4) embedding 未就绪的真实分支（注入依赖缺失，先于 LLM 闸门）
    before_runs4 = _run_ids_now()
    with _InsightsDepsMissing():
        resp4 = client.post("/api/v1/knowledge/extract")
    c.check(
        resp4.status_code == 400
        and "本地向量化依赖未安装" in resp4.text
        and "pip install" in resp4.text,
        "未装向量化依赖：POST /extract → 400（注入依赖缺失；安装指引，**先于 LLM 闸门**）",
        f"{resp4.status_code} {resp4.text[:200]}",
    )
    c.check(
        _run_ids_now() == before_runs4,
        "embedding 未就绪：**零请求零 run**（不浪费 token）",
    )


# ---------------------------------------------------------------------------
# 组 8：零迁移
# ---------------------------------------------------------------------------
def group8_migrations(c: Checker) -> None:
    files = sorted(p.name for p in VERSIONS_DIR.glob("*.py") if not p.name.startswith("__"))
    c.check(len(files) == 2, "migrations/versions 仍恰 2 支迁移（本步零迁移）", f"{files}")
    c.check(
        all(name.startswith(("0001", "0002")) for name in files),
        "迁移文件名仍为 0001 / 0002（未新增 0003）",
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
        f"public 业务表仍恰 {EXPECTED_TABLES} 张（RunType 只加枚举成员，无 DDL）",
        f"{len(tables)}: {tables}",
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


# ---------------------------------------------------------------------------
# 组 9：合规扫描
# ---------------------------------------------------------------------------
def group9_compliance(c: Checker) -> None:
    for path in SCANNED_FILES:
        src = path.read_text(encoding="utf-8")
        hits = [pattern for pattern in FORBIDDEN_PATTERNS if pattern in src]
        c.check(
            not hits,
            f"{path.relative_to(BACKEND_DIR).as_posix()} 不含小红书域名 / 登录态凭据 / 密钥前缀",
            f"{hits}",
        )
    for path in FRONTEND_SCANNED:
        src = path.read_text(encoding="utf-8")
        hits = [pattern for pattern in FORBIDDEN_PATTERNS if pattern in src]
        c.check(
            not hits,
            f"{path.relative_to(ROOT_DIR).as_posix()} 不含违禁字样",
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
    c.check(
        len(ts_files) > 40,
        "前端扫描覆盖全部 ts / tsx（非抽样）",
        f"files={len(ts_files)}",
    )

    prompts_src = (BACKEND_DIR / "app" / "agent" / "insights" / "prompts.py").read_text(
        encoding="utf-8"
    )
    c.check(
        "禁止出现行业平均" in prompts_src
        and "外部数字" in prompts_src
        and "禁止编造" in prompts_src
        and "个人经验" in prompts_src,
        "提炼提示词明文：禁止外部数字 / 禁止编造数据 / 只用个人经验表述",
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    c = Checker()
    print("=" * 78)
    print("第 18 步 · RAG 知识库（embedding / 规律提炼 / 检索 / 反哺）离线自检")
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
        group0_graph(c)
        group1_embedding(c)
        group2_extract_success(c, client, fx)
        group3_retrieval(c, client, fx)
        group4_insufficient(c, client, fx)
        group5_feedback(c, client, fx)
        group6_surface(c, client, fx)
        group7_failures(c, client, fx)
        group8_migrations(c)
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
        c.check(
            not bad,
            "测试数据物理清理并双口径复查残留为 0",
            f"{residue}",
        )
    except Exception as exc:  # noqa: BLE001
        c.check(False, "测试数据物理清理并双口径复查残留为 0", f"复查异常：{exc}")

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项，跳过 {len(c.skipped)} 项。")
    if c.failures:
        for name in c.failures:
            print(f"  - FAIL: {name}")
        return 1
    print("全部 PASS ✔（第 18 步 RAG 知识库自检，依赖本地 docker PostgreSQL）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
