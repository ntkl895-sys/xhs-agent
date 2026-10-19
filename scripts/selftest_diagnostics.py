"""第 16 步 · 单篇诊断 Agent 后端自检
（**依赖本地 docker PostgreSQL；不联网、不需要真实 API Key**）。

覆盖任务书要求的全部断言，分 9 组：

0. 图结构：诊断图是真 LangGraph（五节点 + 条件边 + **有界回路**），
   ``load_context → generate → validate →（条件边）persist / 回炉 / give_up``；
1. 确定性纯函数：latest 取值（最后一个非空值 + 最新快照 id）、六率
   （**同快照配对** / views=0 缺失→None / 防跨快照错配 / 4 位小数）、环比与趋势
   （相邻增量 / 总增量 / 增长率含首期 0→None / 覆盖时长）、类型判定四情形
   （manual 优先 / 无快照→cold_start / 明显负增长→anomaly / 其余→review）、
   data_gaps 收集、输出归一与校验（score 越界 / suggestions 空 / 冷启动强制不评分）；
2. 成功路径（假模型）：``POST /generate`` 201 + ``diagnostics`` 逐列正确 +
   ``based_on_metric_id`` 指向最新快照 + ``benchmark`` 固定结构
   （``external_benchmark=null`` + 基准缺失声明）+ ``llm_meta`` 白名单
   + ``agent_runs`` 的 outputs / note_id / llm_raw 回填与脱敏（无 sk- / 无 base64）
   + 响应无 ``llm_raw`` + 假模型恰 1 次调用 + **只读**（notes / topics /
   note_metrics 不变）+ 手动类型（source=manual）；
3. 数据不足冷启动：无快照也能成功产出（score=null 不硬打分）、data_gaps 含
   「无运营数据快照」、suggestions 以回填数据 + 内容侧检查为主、不臆造数字；
4. 失败路径：全坏 JSON 超限 → 400 且**模型调用次数 = 回路上界**（3×3=9）、
   LLM 未配置 → 400 且不新建 agent_runs（零请求）、note 不存在 → 404、
   类型非法 → 422；响应均无 Traceback；
5. 接口面：``/generate`` 仅 POST、集合仅 GET、``/{id}`` 仅 GET，
   无 PUT / PATCH / DELETE；``DiagnosticOut`` / 请求模型无 ``llm_raw``；白名单恰五项；
   ``main.py`` include_router ≥ 9 且 lifespan 唯一；
6. 零迁移：``migrations/versions`` 仍恰 2 支、``alembic current`` = ``0002 (head)``、
   ``alembic check`` 退出码 0；
7. 合规扫描：本步新增 / 修改的后端、前端文件不含小红书域名 / 登录态凭据 / 密钥前缀；
   第 14 步既有的发布外链（note-studio-client.tsx）仍**恰 1 处**、本步未新增
   （该文件由 ``selftest_studio.py`` 背书，不在通用扫描清单内）；
8. 清理复查：测试数据（前缀 ``[SELFTEST]单篇诊断``）按 id 删 + 前缀兜底扫，
   双口径残留 0；检查点线程与 agent_runs 按**运行前后差集**安全清理。

HTTP 断言走**进程内** ``TestClient``（不需要 uvicorn）；数据复核走 SQLAlchemy 会话；
假模型经 monkeypatch 注入 ``app.agent.diagnosis.service.get_chat_model``（**不发真实请求**）。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\selftest_diagnostics.py

全部通过退出码 0；任一项 FAIL 退出码 1。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
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

import app.agent.diagnosis.service as diagnosis_service  # noqa: E402
from app.agent.checkpoint import close_checkpointer, init_checkpointer  # noqa: E402
from app.agent.diagnosis.contracts import (  # noqa: E402
    collect_data_gaps,
    compute_rates,
    compute_trends,
    decide_diagnosis_type,
    merge_data_gaps,
    metric_latest,
    normalize_diagnosis_payload,
)
from app.agent.diagnosis.graph import (  # noqa: E402
    NODE_GENERATE,
    NODE_GIVE_UP,
    NODE_LOAD_CONTEXT,
    NODE_PERSIST,
    NODE_VALIDATE,
    build_diagnosis_graph,
)
from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.models.enums import (  # noqa: E402
    AccountStatus,
    ComplianceStatus,
    DataSource,
    NoteStatus,
    PersonaStatus,
    TopicStatus,
)
from app.models.metric import NoteMetric  # noqa: E402
from app.models.note import Cover, Note  # noqa: E402
from app.models.persona import Persona  # noqa: E402
from app.models.topic import Topic  # noqa: E402

#: 本脚本全部测试数据统一前缀（清理与复查的双口径都靠它兜底）
PREFIX = "[SELFTEST]单篇诊断"

VERSIONS_DIR = BACKEND_DIR / "migrations" / "versions"

#: 本步新增 / 修改过的后端文件（合规扫描对象）
SCANNED_FILES = (
    BACKEND_DIR / "app" / "agent" / "diagnosis" / "__init__.py",
    BACKEND_DIR / "app" / "agent" / "diagnosis" / "contracts.py",
    BACKEND_DIR / "app" / "agent" / "diagnosis" / "state.py",
    BACKEND_DIR / "app" / "agent" / "diagnosis" / "prompts.py",
    BACKEND_DIR / "app" / "agent" / "diagnosis" / "graph.py",
    BACKEND_DIR / "app" / "agent" / "diagnosis" / "service.py",
    BACKEND_DIR / "app" / "schemas" / "diagnostic.py",
    BACKEND_DIR / "app" / "crud" / "diagnostic.py",
    BACKEND_DIR / "app" / "api" / "routes" / "diagnostics.py",
    BACKEND_DIR / "app" / "main.py",
    BACKEND_DIR / "app" / "core" / "config.py",
)

#: 本步新增 / 修改过的前端文件（合规扫描对象）。
#: 注意：`notes/[id]/note-studio-client.tsx` **不在此清单** —— 该文件含第 14 步既有
#: 的「前往发布页」外链常量（含平台域名），已由 ``selftest_studio.py`` 的专用断言
#: 背书；本步对其改动仅为并列新增「单篇诊断 →」跳转链接，不引入任何新的禁词
#: （组 7 另有「全前端域名恰 1 处」的守卫断言）。
FRONTEND_SCANNED = (
    FRONTEND_SRC / "app" / "diagnostics" / "page.tsx",
    FRONTEND_SRC / "app" / "api" / "v1" / "diagnostics" / "route.ts",
    FRONTEND_SRC / "app" / "api" / "v1" / "diagnostics" / "generate" / "route.ts",
    FRONTEND_SRC / "app" / "api" / "v1" / "diagnostics" / "[id]" / "route.ts",
    FRONTEND_SRC / "lib" / "types.ts",
    FRONTEND_SRC / "components" / "site-header.tsx",
    FRONTEND_SRC / "app" / "notes" / "page.tsx",
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

#: 六个指标 / 六条比率（测试侧硬编码，**不复用后端常量**以防「同源错误」）
METRIC_KEYS = ("views", "likes", "collects", "comments", "shares", "new_followers")
RATE_KEYS = (
    "like_rate",
    "collect_rate",
    "comment_rate",
    "share_rate",
    "follow_rate",
    "engagement_rate",
)

#: benchmark 的固定键集（结构由诊断图固定生成）
BENCHMARK_KEYS = {
    "basis",
    "latest_metrics",
    "rates",
    "trends",
    "thresholds",
    "external_benchmark",
    "benchmark_note",
}

#: llm_meta 白名单（与 schemas/diagnostic.py 一致）
META_KEYS = {
    "prompt_version",
    "json_parse_attempts",
    "diagnosis_type",
    "diagnosis_type_source",
    "data_gaps",
}

#: 基准缺失声明的**期望原文**（与任务书一致；刻意不复用后端常量）
EXPECTED_BENCHMARK_NOTE = (
    "本期无同类/历史爆款基准，仅基于该笔记自身数据；跨篇基准见第 18 步 RAG"
)

#: 时间基准（纯函数测试用）
T0 = datetime(2026, 10, 1, 0, 0, tzinfo=dt_timezone.utc)
H1 = timedelta(hours=1)
H2 = timedelta(hours=2)

#: 正常诊断载荷（假模型首轮响应；六率与趋势均在本地算好，模型只做评价）
GOOD_DIAG_JSON: dict[str, Any] = {
    "score": 82.5,
    "strengths": ["互动率表现良好", "标题切入具体"],
    "weaknesses": ["评论率偏低"],
    "reasons": ["互动率 0.2025，主要由点赞贡献（同快照配对计算）"],
    "suggestions": ["优化正文结尾的互动引导句", "把标签从 3 个扩充到 5 个相关词"],
    "data_gaps": ["建议补充发布后 24 小时快照"],
    "confidence": 0.8,
}

#: 手动类型（anomaly）的载荷
MANUAL_DIAG_JSON: dict[str, Any] = {
    "score": 70,
    "strengths": ["事件仍在传播窗口内"],
    "weaknesses": ["浏览量较前一次快照明显回落"],
    "reasons": ["views 从 2000 回落到 1200（降幅 40%）"],
    "suggestions": ["先核实快照是否采错时间点", "复盘发布时间与话题热度变化"],
    "data_gaps": [],
    "confidence": 0.6,
}

#: 冷启动载荷（模型给了 score，但本地必须强制为 None）
COLD_DIAG_JSON: dict[str, Any] = {
    "score": 77,
    "strengths": [],
    "weaknesses": ["还没有运营数据，无法判断表现"],
    "reasons": ["无任何数据快照，只能做内容侧初步检查"],
    "suggestions": [
        "尽快回填第一条数据快照（截图识别或手工录入）",
        "检查标题是否包含核心关键词与人群词",
    ],
    "data_gaps": [],
    "confidence": None,
}


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"[PASS] {label}")
        else:
            self.failures.append(label)
            print(f"[FAIL] {label}" + (f"  ->  {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 假模型（照 selftest_content_agent / selftest_metrics 的确定性脚本模型）
# ---------------------------------------------------------------------------
class DiagnosisScriptModel(BaseChatModel):
    """确定性假模型：按调用次序取响应；末项会被重复使用（模拟模型固执己见）。"""

    responses: list[Any] = Field(default_factory=list)
    calls: int = 0
    prompts: list[str] = Field(default_factory=list)
    usage_in: int = 21
    usage_out: int = 34

    @property
    def _llm_type(self) -> str:
        return "diagnosis-script"

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


class _ScriptedModel:
    """上下文管理器：把诊断 service 的模型工厂换成假模型，退出时恢复。"""

    def __init__(self, model: DiagnosisScriptModel) -> None:
        self.model = model
        self._original: Any = None

    def __enter__(self) -> DiagnosisScriptModel:
        self._original = diagnosis_service.get_chat_model
        diagnosis_service.get_chat_model = lambda **kwargs: self.model  # noqa: ARG005
        return self.model

    def __exit__(self, *exc_info: Any) -> None:
        diagnosis_service.get_chat_model = self._original


# ---------------------------------------------------------------------------
# 夹具与清理
# ---------------------------------------------------------------------------
class Fixture:
    def __init__(self) -> None:
        self.account_id: uuid.UUID | None = None
        self.persona_id: uuid.UUID | None = None
        self.topic_id: uuid.UUID | None = None
        self.note_id: uuid.UUID | None = None  # 有 2 条快照 + 选中封面的已发布笔记
        self.note2_id: uuid.UUID | None = None  # 无快照的已发布笔记（冷启动）
        self.cover_id: uuid.UUID | None = None
        self.snapshot_ids: list[uuid.UUID] = []
        self.before_threads: set[str] = set()
        self.before_run_ids: set[str] = set()
        self.residue: dict[str, int] = {}

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

    # ------------------------------------------------------------- 建立
    def setup(self) -> None:
        self.before_threads = self._threads_now()
        self.before_run_ids = _run_ids_now()
        now = datetime.now(dt_timezone.utc)
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
            note = Note(
                account_id=account.id,
                persona_id=persona.id,
                topic_id=topic.id,
                title=f"{PREFIX}已发布笔记",
                body="已发布正文（用于内容侧诊断的事实材料）。",
                tags=["自检", "诊断"],
                status=NoteStatus.PUBLISHED,
                compliance_status=ComplianceStatus.PASSED,
                published_at=now - timedelta(hours=3),
            )
            db.add(note)
            db.flush()
            note2 = Note(
                account_id=account.id,
                persona_id=persona.id,
                title=f"{PREFIX}冷启动笔记",
                body="没有数据快照的已发布笔记。",
                tags=["自检"],
                status=NoteStatus.PUBLISHED,
                compliance_status=ComplianceStatus.PASSED,
                published_at=now - timedelta(hours=1),
            )
            db.add(note2)
            db.flush()
            cover = Cover(
                note_id=note.id,
                template_key="gradient_hero",
                color_scheme="coral",
                title_text="自检封面",
                is_selected=True,
            )
            db.add(cover)
            db.flush()
            snap1 = NoteMetric(
                note_id=note.id,
                captured_at=now - timedelta(hours=2),
                data_source=DataSource.MANUAL,
                views=1000,
                likes=100,
                collects=50,
                comments=10,
                shares=5,
                new_followers=20,
            )
            snap2 = NoteMetric(
                note_id=note.id,
                captured_at=now,
                data_source=DataSource.SCREENSHOT,
                views=2000,
                likes=240,
                collects=120,
                comments=30,
                shares=15,
                new_followers=45,
            )
            db.add(snap1)
            db.add(snap2)
            db.flush()
            self.account_id = account.id
            self.persona_id = persona.id
            self.topic_id = topic.id
            self.note_id = note.id
            self.note2_id = note2.id
            self.cover_id = cover.id
            self.snapshot_ids = [snap1.id, snap2.id]
            db.commit()

    @property
    def latest_snapshot_id(self) -> uuid.UUID:
        """captured_at 最晚的那条快照（= based_on_metric_id 的期望值）。"""
        return self.snapshot_ids[-1]

    # ------------------------------------------------------------- 清理
    def cleanup(self) -> None:
        note_ids = [str(item) for item in (self.note_id, self.note2_id) if item is not None]
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
        # 3) 业务数据：diagnostics / covers / note_metrics → notes → topics → personas → accounts
        like = f"%{PREFIX}%"
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
            if self.note_id is not None:
                db.execute(text("DELETE FROM notes WHERE id = :n"), {"n": str(self.note_id)})
            if self.note2_id is not None:
                db.execute(text("DELETE FROM notes WHERE id = :n"), {"n": str(self.note2_id)})
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
        note_ids = [str(item) for item in (self.note_id, self.note2_id) if item is not None]
        with SessionLocal() as db:
            return {
                "diagnostics(by_note_id)": db.execute(
                    text("SELECT count(*) FROM diagnostics WHERE note_id = ANY(:nids)"),
                    {"nids": note_ids},
                ).scalar_one(),
                "diagnostics(prefix)": db.execute(
                    text(
                        "SELECT count(*) FROM diagnostics WHERE note_id IN "
                        "(SELECT id FROM notes WHERE title LIKE :t)"
                    ),
                    {"t": like},
                ).scalar_one(),
                "note_metrics(by_note_id)": db.execute(
                    text("SELECT count(*) FROM note_metrics WHERE note_id = ANY(:nids)"),
                    {"nids": note_ids},
                ).scalar_one(),
                "note_metrics(prefix)": db.execute(
                    text(
                        "SELECT count(*) FROM note_metrics WHERE note_id IN "
                        "(SELECT id FROM notes WHERE title LIKE :t)"
                    ),
                    {"t": like},
                ).scalar_one(),
                "covers(by_note_id)": db.execute(
                    text("SELECT count(*) FROM covers WHERE note_id = ANY(:nids)"),
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
        return {str(row[0]) for row in db.execute(text("SELECT id FROM agent_runs")).all()}


def _count_runs() -> int:
    with SessionLocal() as db:
        return int(db.execute(text("SELECT count(*) FROM agent_runs")).scalar_one())


def _count_metrics(note_id: uuid.UUID) -> int:
    with SessionLocal() as db:
        return int(
            db.execute(
                text("SELECT count(*) FROM note_metrics WHERE note_id = :n"),
                {"n": str(note_id)},
            ).scalar_one()
        )


def _count_diagnostics(note_id: uuid.UUID) -> int:
    with SessionLocal() as db:
        return int(
            db.execute(
                text("SELECT count(*) FROM diagnostics WHERE note_id = :n"),
                {"n": str(note_id)},
            ).scalar_one()
        )


def _fetch_diagnostic(diagnostic_id: str) -> dict[str, Any]:
    cols = (
        "id, note_id, based_on_metric_id, diagnosis_type, score, strengths, weaknesses, "
        "reasons, suggestions, benchmark, llm_model, llm_meta"
    )
    with SessionLocal() as db:
        row = db.execute(
            text(f"SELECT {cols} FROM diagnostics WHERE id = :d"),  # noqa: S608
            {"d": str(diagnostic_id)},
        ).one_or_none()
    if row is None:
        return {}
    keys = [item.strip() for item in cols.split(",")]
    return dict(zip(keys, row, strict=True))


def _fetch_run(run_id: str) -> dict[str, Any]:
    cols = (
        "id, run_type, trigger, status, outputs, llm_raw, error_message, note_id, "
        "json_parse_attempts, prompt_tokens, completion_tokens, total_tokens"
    )
    with SessionLocal() as db:
        row = db.execute(
            text(f"SELECT {cols} FROM agent_runs WHERE id = :r"),  # noqa: S608
            {"r": str(run_id)},
        ).one_or_none()
    if row is None:
        return {}
    keys = [item.strip() for item in cols.split(",")]
    return dict(zip(keys, row, strict=True))


def _fetch_note(note_id: uuid.UUID) -> dict[str, Any]:
    with SessionLocal() as db:
        row = db.execute(
            text("SELECT title, body, status FROM notes WHERE id = :n"),
            {"n": str(note_id)},
        ).one_or_none()
    if row is None:
        return {}
    return {"title": row[0], "body": row[1], "status": row[2]}


def _fetch_topic_status(topic_id: uuid.UUID) -> str | None:
    with SessionLocal() as db:
        row = db.execute(
            text("SELECT status FROM topics WHERE id = :t"), {"t": str(topic_id)}
        ).one_or_none()
    return None if row is None else str(row[0])


def _post_generate(client: TestClient, note_id: uuid.UUID | str, diagnosis_type: Any = None):
    payload: dict[str, Any] = {"note_id": str(note_id)}
    if diagnosis_type is not None:
        payload["diagnosis_type"] = diagnosis_type
    return client.post("/api/v1/diagnostics/generate", json=payload)


# ---------------------------------------------------------------------------
# 纯函数测试用的快照构造
# ---------------------------------------------------------------------------
def _snap(index: int, captured_at: datetime, **values: Any) -> dict[str, Any]:
    """构造一条普通 dict 快照（缺省指标全部为 None；captured_at 用 ISO 串）。"""
    row: dict[str, Any] = {
        "id": f"00000000-0000-0000-0000-0000000000{index:02d}",
        "captured_at": captured_at.isoformat(),
        "views": None,
        "likes": None,
        "collects": None,
        "comments": None,
        "shares": None,
        "new_followers": None,
        "data_source": "manual",
    }
    row.update(values)
    return row


# ---------------------------------------------------------------------------
# 组 0：图结构（真 LangGraph + 有界回路）
# ---------------------------------------------------------------------------
def group0_graph(c: Checker) -> None:
    graph = build_diagnosis_graph(
        model=DiagnosisScriptModel(responses=[GOOD_DIAG_JSON]),
        use_checkpointer=False,
    )
    c.check(
        "Compiled" in type(graph).__name__,
        "build_diagnosis_graph 返回已编译的 LangGraph 图",
        f"{type(graph).__name__}",
    )
    view = graph.get_graph()
    nodes = set(view.nodes.keys())
    business = {node for node in nodes if not node.startswith("__")}
    c.check(
        business == {NODE_LOAD_CONTEXT, NODE_GENERATE, NODE_VALIDATE, NODE_PERSIST, NODE_GIVE_UP},
        "图含 load_context / generate_diagnosis / validate_diagnosis / persist / give_up 五节点",
        f"实际={sorted(nodes)}",
    )
    edges = {(edge.source, edge.target, bool(edge.conditional)) for edge in view.edges}
    c.check(
        (NODE_LOAD_CONTEXT, NODE_GENERATE, False) in edges,
        "load_context → generate_diagnosis 为直连边",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_GENERATE, NODE_VALIDATE, False) in edges,
        "generate_diagnosis → validate_diagnosis 为直连边",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_VALIDATE, NODE_GENERATE, True) in edges,
        "validate_diagnosis → generate_diagnosis 为条件边（回炉回路）",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_VALIDATE, NODE_PERSIST, True) in edges,
        "validate 通过 → persist 为条件边",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_VALIDATE, NODE_GIVE_UP, True) in edges,
        "validate 超限 → give_up 为条件边",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_PERSIST, "__end__", False) in edges and (NODE_GIVE_UP, "__end__", False) in edges,
        "persist / give_up → END",
        f"edges={sorted(edges)}",
    )
    # 回路有界：路由函数必须绑定 AGENT_JSON_MAX_ATTEMPTS（行为面在组 4 验证 3×3=9）
    graph_src = (BACKEND_DIR / "app" / "agent" / "diagnosis" / "graph.py").read_text(
        encoding="utf-8"
    )
    c.check(
        "AGENT_JSON_MAX_ATTEMPTS" in graph_src,
        "回路上界绑定 AGENT_JSON_MAX_ATTEMPTS（有界回路，不无限重试）",
    )


# ---------------------------------------------------------------------------
# 组 1：确定性纯函数
# ---------------------------------------------------------------------------
def group1_pure_functions(c: Checker) -> None:
    # --- 1.1 latest：每个指标的最后一个非空值 + 最新快照 id ---
    snaps = [
        _snap(1, T0, views=1000, likes=100),
        _snap(2, T0 + H1, views=None, likes=260),
    ]
    latest, latest_id = metric_latest(snaps)
    c.check(
        latest["views"] == 1000 and latest["likes"] == 260,
        "metric_latest 取每个指标的**最后一个非空值**（后一条为空时向前取）",
        f"{latest}",
    )
    c.check(
        latest["collects"] is None and latest["new_followers"] is None,
        "metric_latest 全程缺失的指标恒为 None（不臆造）",
        f"{latest}",
    )
    c.check(
        latest_id == snaps[-1]["id"],
        "latest_snapshot_id = 时间线中**最新一条**快照 id（→ based_on_metric_id）",
        f"{latest_id}",
    )
    empty_latest, empty_id = metric_latest([])
    c.check(
        all(value is None for value in empty_latest.values()) and empty_id is None,
        "无快照时 metric_latest 全部 None 且 id 为 None",
    )

    # --- 1.2 六率：同快照配对 / views=0 / 防跨快照错配 / 4 位小数 ---
    paired = [
        _snap(1, T0, views=1000, likes=100, collects=50, comments=10, shares=5, new_followers=10),
        _snap(2, T0 + H1, views=2000),  # 最新快照只有 views：不得拿它配旧分子
    ]
    rates = compute_rates(paired)
    c.check(
        rates["like_rate"] == 0.1 and rates["collect_rate"] == 0.05,
        "比率基于**同一条快照**配对（最新的分子缺失时回退更早的完整快照，防跨快照错配）",
        f"{rates}",
    )
    c.check(
        rates["engagement_rate"] == round(165 / 1000, 4),
        "engagement_rate = 四互动之和 / **同快照** views",
        f"{rates['engagement_rate']}",
    )
    zero = compute_rates([_snap(1, T0, views=0, likes=5, collects=1, comments=1, shares=1)])
    c.check(
        all(zero[key] is None for key in RATE_KEYS),
        "views=0 时全部比率为 None（防除零，不臆造）",
        f"{zero}",
    )
    normal = compute_rates(
        [
            _snap(
                1,
                T0,
                views=2000,
                likes=240,
                collects=120,
                comments=30,
                shares=15,
                new_followers=45,
            )
        ]
    )
    c.check(
        normal["like_rate"] == 0.12
        and normal["collect_rate"] == 0.06
        and normal["comment_rate"] == 0.015
        and normal["share_rate"] == 0.0075
        and normal["follow_rate"] == 0.0225
        and normal["engagement_rate"] == 0.2025,
        "六条比率口径正确且精确到 4 位小数",
        f"{normal}",
    )

    # --- 1.3 环比 / 趋势 / 覆盖时长 ---
    trends = compute_trends(
        [
            _snap(1, T0, views=1000, likes=100),
            _snap(2, T0 + H2, views=3000, likes=260),
        ],
        T0 - H2,
    )
    c.check(
        trends["total_deltas"]["views"] == 2000 and trends["total_deltas"]["likes"] == 160,
        "总增量 = 首期（第一个非空值）→ 最新（最后一个非空值）",
        f"{trends['total_deltas']}",
    )
    c.check(
        trends["growth_rates"]["views"] == 2.0,
        "增长率 = 总增量 / 首期值（4 位小数）",
        f"{trends['growth_rates']}",
    )
    c.check(
        trends["coverage_hours"] == 4,
        "覆盖时长 = published_at → 最新 captured_at（小时整数）",
        f"{trends['coverage_hours']}",
    )
    segment = trends["segments"][0]
    c.check(
        segment["deltas"]["views"] == 2000 and segment["from"] == T0.isoformat(),
        "相邻快照逐段记录增量（from / to / deltas）",
        f"{segment}",
    )
    first_zero = compute_trends(
        [_snap(1, T0, views=0), _snap(2, T0 + H1, views=500)],
        None,
    )
    c.check(
        first_zero["total_deltas"]["views"] == 500
        and first_zero["growth_rates"]["views"] is None,
        "首期为 0 时增长率为 None（不产生除零）",
        f"{first_zero['growth_rates']}",
    )
    c.check(
        first_zero["coverage_hours"] == 1,
        "无 published_at 时覆盖时长按**最早快照**估算",
        f"{first_zero['coverage_hours']}",
    )
    c.check(
        first_zero["total_deltas"]["likes"] is None,
        "某一端缺失的指标总增量为 None",
        f"{first_zero['total_deltas']}",
    )

    # --- 1.4 类型判定四情形 ---
    manual = decide_diagnosis_type([], "anomaly")
    c.check(
        manual["type"] == "anomaly" and manual["source"] == "manual",
        "用户显式指定类型优先（source=manual，不走自动判定）",
        f"{manual}",
    )
    no_snap = decide_diagnosis_type([])
    c.check(
        no_snap["type"] == "cold_start" and no_snap["source"] == "auto",
        "无任何快照 → cold_start（自动判定）",
        f"{no_snap}",
    )
    views_missing = decide_diagnosis_type([_snap(1, T0, likes=10)])
    c.check(
        views_missing["type"] == "cold_start",
        "最新 views 缺失 → cold_start（无法建立漏斗口径）",
        f"{views_missing}",
    )
    drop = decide_diagnosis_type(
        [
            _snap(1, T0, views=1000, likes=100),
            _snap(2, T0 + H1, views=600, likes=90),
        ]
    )
    c.check(
        drop["type"] == "anomaly" and "views" in drop["reason"],
        "views 降幅 40% ≥ 阈值（30%）→ anomaly，且 reason 指向依据",
        f"{drop}",
    )
    likes_drop = decide_diagnosis_type(
        [
            _snap(1, T0, views=1000, likes=100),
            _snap(2, T0 + H1, views=900, likes=50),
        ]
    )
    c.check(
        likes_drop["type"] == "anomaly" and "likes" in likes_drop["reason"],
        "likes 降幅 ≥ 阈值同样判 anomaly",
        f"{likes_drop}",
    )
    mild = decide_diagnosis_type(
        [
            _snap(1, T0, views=1000, likes=100),
            _snap(2, T0 + H1, views=800, likes=90),
        ]
    )
    c.check(mild["type"] == "review", "降幅低于阈值 → review（常规复盘）", f"{mild}")
    custom = decide_diagnosis_type(
        [
            _snap(1, T0, views=1000, likes=100),
            _snap(2, T0 + H1, views=800, likes=90),
        ],
        None,
        anomaly_drop_ratio=0.1,
    )
    c.check(
        custom["type"] == "anomaly",
        "异常阈值可由配置调整（DIAG_ANOMALY_DROP_RATIO 生效）",
        f"{custom}",
    )

    # --- 1.5 data_gaps 收集 ---
    gaps = collect_data_gaps(
        snapshots=[],
        latest_values={key: None for key in METRIC_KEYS},
        rates={key: None for key in RATE_KEYS},
        has_cover=False,
        published_at=None,
        body_text="",
    )
    c.check(
        any("无运营数据快照" in item for item in gaps),
        "无快照 → data_gaps 声明「无运营数据快照」",
        f"{gaps}",
    )
    c.check(
        any("封面" in item for item in gaps)
        and any("发布时间" in item for item in gaps)
        and any("正文" in item for item in gaps),
        "缺封面 / 发布时间 / 正文均逐项声明",
        f"{gaps}",
    )
    one_snap = [
        _snap(1, T0, views=100),
    ]
    one_gaps = collect_data_gaps(
        snapshots=one_snap,
        latest_values={"views": 100, **{key: None for key in METRIC_KEYS if key != "views"}},
        rates=compute_rates(one_snap),
        has_cover=True,
        published_at=T0,
        body_text="正文",
    )
    c.check(
        any("只有 1 条快照" in item for item in one_gaps),
        "仅 1 条快照 → 声明无法计算环比与趋势",
        f"{one_gaps}",
    )
    c.check(
        any("缺少指标" in item for item in one_gaps),
        "缺失指标逐项声明",
        f"{one_gaps}",
    )
    c.check(
        any("无法计算" in item for item in one_gaps),
        "无法计算的比率逐项声明（同快照内分子缺失）",
        f"{one_gaps}",
    )

    # --- 1.6 输出归一与校验 ---
    normalized, errors, warnings = normalize_diagnosis_payload(
        GOOD_DIAG_JSON,
        cold_start=False,
        max_items=8,
        text_max_len=300,
        score_min=0,
        score_max=100,
    )
    c.check(
        not errors and normalized["score"] == 82.5 and len(normalized["suggestions"]) == 2,
        "合法载荷无硬伤（score / 四数组归一正确）",
        f"errors={errors}",
    )
    c.check(
        not warnings,
        "合法载荷不产生宽容修正 warning",
        f"{warnings}",
    )
    _, bad_score_errors, _ = normalize_diagnosis_payload(
        {"score": 120, "suggestions": ["ok"]},
        cold_start=False,
        max_items=8,
        text_max_len=300,
        score_min=0,
        score_max=100,
    )
    c.check(
        any("越界" in item for item in bad_score_errors),
        "score 越界（>100）→ 硬伤（回炉重写）",
        f"{bad_score_errors}",
    )
    _, empty_sugg_errors, _ = normalize_diagnosis_payload(
        {"score": 80, "suggestions": []},
        cold_start=False,
        max_items=8,
        text_max_len=300,
        score_min=0,
        score_max=100,
    )
    c.check(
        any("suggestions" in item for item in empty_sugg_errors),
        "suggestions 为空 → 硬伤（必须给出可执行建议）",
        f"{empty_sugg_errors}",
    )
    cold_normalized, cold_errors, cold_warnings = normalize_diagnosis_payload(
        {"score": 88, "suggestions": ["a"]},
        cold_start=True,
        max_items=8,
        text_max_len=300,
        score_min=0,
        score_max=100,
    )
    c.check(
        cold_normalized["score"] is None
        and not cold_errors
        and any("不硬打分" in item for item in cold_warnings),
        "冷启动**强制 score=None**（模型给分也不采纳、不判硬伤）",
        f"score={cold_normalized['score']} warnings={cold_warnings}",
    )
    big, big_errors, big_warnings = normalize_diagnosis_payload(
        {
            "score": 80,
            "suggestions": [f"建议 {index}" for index in range(12)],
            "strengths": ["x" * 400],
        },
        cold_start=False,
        max_items=8,
        text_max_len=300,
        score_min=0,
        score_max=100,
    )
    c.check(
        len(big["suggestions"]) == 8
        and len(big["strengths"][0]) == 300
        and len(big_warnings) >= 2
        and not big_errors,
        "超条数 / 超长被**本地截断**并记录 warning（不进回炉）",
        f"warnings={big_warnings}",
    )
    merged = merge_data_gaps(["a", "b"], ["b", "c"], limit=2)
    c.check(
        merged == ["a", "b"],
        "merge_data_gaps 去重 + 保序 + 限长",
        f"{merged}",
    )


# ---------------------------------------------------------------------------
# 组 2：成功路径（假模型，经 HTTP 全链路）
# ---------------------------------------------------------------------------
def group2_success(c: Checker, client: TestClient, fx: Fixture) -> None:
    assert fx.note_id is not None
    before_runs = _run_ids_now()
    before_threads = fx._threads_now()
    before_metrics = _count_metrics(fx.note_id)
    before_note = _fetch_note(fx.note_id)
    before_topic = _fetch_topic_status(fx.topic_id)  # type: ignore[arg-type]

    model = DiagnosisScriptModel(responses=[GOOD_DIAG_JSON, MANUAL_DIAG_JSON])
    with _ScriptedModel(model):
        r = _post_generate(client, fx.note_id)
    c.check(
        r.status_code == 201,
        "POST /api/v1/diagnostics/generate 成功 → 201",
        f"{r.status_code} {r.text[:200]}",
    )
    data: dict[str, Any] = r.json() if r.status_code == 201 else {}

    c.check(
        data.get("note_id") == str(fx.note_id),
        "响应 note_id 正确",
        f"{data.get('note_id')}",
    )
    c.check(
        data.get("diagnosis_type") == "review",
        "自动判定为 review（有数据、无显著负增长）",
        f"{data.get('diagnosis_type')}",
    )
    c.check(
        data.get("score") == 82.5,
        "score 逐列落库并返回（0~100）",
        f"{data.get('score')}",
    )
    c.check(
        data.get("strengths") == GOOD_DIAG_JSON["strengths"],
        "strengths 数组原样返回",
        f"{data.get('strengths')}",
    )
    c.check(
        data.get("suggestions") == GOOD_DIAG_JSON["suggestions"],
        "suggestions 数组原样返回（可执行建议）",
        f"{data.get('suggestions')}",
    )
    c.check(
        data.get("based_on_metric_id") == str(fx.latest_snapshot_id),
        "based_on_metric_id = 服务端算出的最新快照 id",
        f"{data.get('based_on_metric_id')}",
    )

    # --- llm_meta 白名单 ---
    meta = data.get("llm_meta") or {}
    c.check(
        set(meta) == META_KEYS,
        "llm_meta 恰为白名单五项（不多不少）",
        f"{sorted(meta)}",
    )
    c.check(
        meta.get("prompt_version") == settings.DIAG_PROMPT_VERSION,
        "llm_meta.prompt_version 取配置值",
        f"{meta.get('prompt_version')}",
    )
    c.check(
        meta.get("json_parse_attempts") == 1,
        "llm_meta.json_parse_attempts = 1（一次命中）",
        f"{meta.get('json_parse_attempts')}",
    )
    c.check(
        meta.get("diagnosis_type_source") == "auto",
        "llm_meta.diagnosis_type_source = auto（自动判定）",
        f"{meta.get('diagnosis_type_source')}",
    )
    gaps = meta.get("data_gaps") or []
    c.check(
        any("24 小时快照" in item for item in gaps),
        "模型补充的 data_gaps 与本地缺口合并保留",
        f"{gaps}",
    )
    c.check(
        not any("未选择封面" in item for item in gaps),
        "已选封面 → data_gaps **不含**封面缺口（事实不误报）",
        f"{gaps}",
    )

    # --- benchmark 固定结构（绝不杜撰外部数字）---
    bench = data.get("benchmark") or {}
    c.check(
        set(bench) == BENCHMARK_KEYS,
        "benchmark 恰为固定七键（结构稳定）",
        f"{sorted(bench)}",
    )
    c.check(
        bench.get("basis") == "self_only",
        "benchmark.basis = self_only（仅基于该笔记自身）",
        f"{bench.get('basis')}",
    )
    c.check(
        bench.get("external_benchmark") is None,
        "benchmark.external_benchmark = null（**绝不杜撰**行业均值 / 爆款阈值）",
        f"{bench.get('external_benchmark')}",
    )
    c.check(
        bench.get("benchmark_note") == EXPECTED_BENCHMARK_NOTE,
        "benchmark.benchmark_note 为固定基准缺失声明（指向第 18 步 RAG）",
        f"{bench.get('benchmark_note')!r}",
    )
    latest_metrics = bench.get("latest_metrics") or {}
    c.check(
        latest_metrics.get("views") == 2000 and latest_metrics.get("likes") == 240,
        "benchmark.latest_metrics = 本地算好的最新六指标",
        f"{latest_metrics}",
    )
    rates = bench.get("rates") or {}
    c.check(
        rates.get("like_rate") == 0.12 and rates.get("engagement_rate") == 0.2025,
        "benchmark.rates = 本地算好的六条互动率（4 位小数）",
        f"{rates}",
    )
    c.check(
        bench.get("thresholds", {}).get("anomaly_drop_ratio") == settings.DIAG_ANOMALY_DROP_RATIO,
        "benchmark.thresholds 记录本地判定阈值（可追溯）",
        f"{bench.get('thresholds')}",
    )
    c.check(
        bench.get("trends", {}).get("coverage_hours") == 3,
        "benchmark.trends 含覆盖时长（published_at → 最新快照）",
        f"{bench.get('trends', {}).get('coverage_hours')}",
    )

    # --- 响应面（无 llm_raw / 无堆栈）---
    c.check(
        "llm_raw" not in r.text,
        "响应**不含** llm_raw（它只进 agent_runs）",
    )
    c.check(
        "Traceback" not in r.text and 'File "' not in r.text,
        "响应不含原始堆栈",
    )
    c.check(
        "sk-" not in r.text and "base64" not in r.text,
        "响应不含密钥前缀 / base64",
    )

    # --- DB 落库（逐列）---
    diagnostic_id = str(data.get("id") or "")
    row = _fetch_diagnostic(diagnostic_id)
    c.check(bool(row), "diagnostics 新增一条（id 可回查）", f"{diagnostic_id}")
    c.check(
        row.get("note_id") is not None
        and str(row.get("note_id")) == str(fx.note_id)
        and str(row.get("based_on_metric_id")) == str(fx.latest_snapshot_id),
        "落库行 note_id / based_on_metric_id 正确",
        f"{row.get('note_id')} / {row.get('based_on_metric_id')}",
    )
    c.check(
        str(row.get("diagnosis_type")) == "review" and float(row.get("score")) == 82.5,
        "落库行 diagnosis_type / score 正确",
        f"{row.get('diagnosis_type')} / {row.get('score')}",
    )
    c.check(
        list(row.get("strengths") or []) == GOOD_DIAG_JSON["strengths"]
        and list(row.get("suggestions") or []) == GOOD_DIAG_JSON["suggestions"],
        "落库行 strengths / suggestions 为 JSONB 字符串数组",
        f"{row.get('strengths')}",
    )
    c.check(
        set(row.get("llm_meta") or {}) == META_KEYS,
        "落库行 llm_meta 亦为白名单五项",
        f"{sorted(row.get('llm_meta') or [])}",
    )
    c.check(
        row.get("llm_model") == settings.LLM_MODEL and "sk-" not in str(row.get("llm_model")),
        "落库行 llm_model = 配置模型名（不含 key）",
        f"{row.get('llm_model')}",
    )
    c.check(
        _count_diagnostics(fx.note_id) == 1,
        "本次诊断恰新增 1 行 diagnostics",
        f"{_count_diagnostics(fx.note_id)}",
    )

    # --- agent_runs 审计回填 ---
    new_runs = _run_ids_now() - before_runs
    run_rows = [_fetch_run(rid) for rid in sorted(new_runs)]
    c.check(len(new_runs) == 1, "本次诊断恰新增 1 条 agent_runs", f"{sorted(new_runs)}")
    if run_rows:
        run = run_rows[0]
        c.check(
            str(run.get("status")) == "success" and str(run.get("run_type")) == "diagnosis",
            "run_type=diagnosis 且 status=success",
            f"{run.get('status')} / {run.get('run_type')}",
        )
        c.check(
            str(run.get("note_id")) == str(fx.note_id),
            "agent_runs.note_id 由 service 按 run_id 回填（harness 不转发 note_id）",
            f"{run.get('note_id')}",
        )
        outputs = run.get("outputs") or {}
        c.check(
            isinstance(outputs, dict)
            and outputs.get("diagnostic_id") == diagnostic_id
            and outputs.get("diagnosis_type") == "review"
            and outputs.get("score") == 82.5
            and outputs.get("persisted") is True,
            "agent_runs.outputs 回填 {diagnostic_id, diagnosis_type, score, persisted:true}",
            f"{outputs}",
        )
        raw = str(run.get("llm_raw") or "")
        c.check(
            bool(raw) and "strengths" in raw,
            "agent_runs.llm_raw 已回填模型原始输出",
            f"{raw[:80]}",
        )
        c.check(
            "sk-" not in raw and "base64" not in raw,
            "llm_raw 已脱敏（无密钥前缀 / 无图片数据）",
        )
        c.check(
            run.get("json_parse_attempts") == 1,
            "agent_runs.json_parse_attempts = 1",
            f"{run.get('json_parse_attempts')}",
        )
        c.check(
            run.get("total_tokens") == 55
            and run.get("prompt_tokens") == 21
            and run.get("completion_tokens") == 34,
            "agent_runs 记录真实 token 用量（只记 token、不记价格）",
            f"{run.get('prompt_tokens')}/{run.get('completion_tokens')}/{run.get('total_tokens')}",
        )

    # --- 假模型调用次数与提示词内容 ---
    c.check(
        model.calls == 1,
        "假模型恰调用 1 次（一次命中，无回炉）",
        f"{model.calls}",
    )
    prompt = model.prompts[0] if model.prompts else ""
    c.check(
        "最新指标" in prompt and "互动率" in prompt and "0.12" in prompt,
        "提示词携带**本地算好的事实**（最新指标 / 互动率 JSON），模型不做算术",
        f"{prompt[:120]}...",
    )
    c.check(
        "数据缺口" in prompt,
        "提示词携带数据缺口声明",
    )

    # --- 只读红线：notes / topics / note_metrics 全不变 ---
    c.check(
        _count_metrics(fx.note_id) == before_metrics == 2,
        "诊断**不改** note_metrics（行数不变）",
        f"{_count_metrics(fx.note_id)} vs {before_metrics}",
    )
    c.check(
        _fetch_note(fx.note_id) == before_note,
        "诊断**不改** notes（标题 / 正文 / 状态不变）",
        f"{_fetch_note(fx.note_id)} vs {before_note}",
    )
    c.check(
        _fetch_topic_status(fx.topic_id) == before_topic == "writing",  # type: ignore[arg-type]
        "诊断**不改** topics（状态不变）",
        f"{_fetch_topic_status(fx.topic_id)}",  # type: ignore[arg-type]
    )

    # --- 手动类型：source=manual ---
    with _ScriptedModel(model):
        r2 = _post_generate(client, fx.note_id, diagnosis_type="anomaly")
    data2: dict[str, Any] = r2.json() if r2.status_code == 201 else {}
    c.check(
        r2.status_code == 201 and data2.get("diagnosis_type") == "anomaly",
        "手动指定 diagnosis_type=anomaly → 201 且落库类型一致",
        f"{r2.status_code} {data2.get('diagnosis_type')}",
    )
    c.check(
        (data2.get("llm_meta") or {}).get("diagnosis_type_source") == "manual",
        "手动指定时 llm_meta.diagnosis_type_source = manual",
        f"{(data2.get('llm_meta') or {}).get('diagnosis_type_source')}",
    )
    c.check(
        _count_diagnostics(fx.note_id) == 2,
        "诊断历史累计 2 条（只增不改）",
        f"{_count_diagnostics(fx.note_id)}",
    )

    # --- 检查点确实绑定（真 LangGraph 运行证据）---
    c.check(
        len(fx._threads_now() - before_threads) >= 1,
        "运行产生新的检查点线程（compile_graph 绑定检查点生效）",
        f"{len(fx._threads_now() - before_threads)}",
    )

    # --- GET 列表 / 详情 ---
    rlist = client.get(f"/api/v1/diagnostics?note_id={fx.note_id}")
    list_data: dict[str, Any] = rlist.json() if rlist.status_code == 200 else {}
    items = list_data.get("items") or []
    c.check(
        rlist.status_code == 200 and list_data.get("note_id") == str(fx.note_id) and len(items) == 2,
        "GET /api/v1/diagnostics?note_id= 返回该笔记的诊断历史（2 条）",
        f"{rlist.status_code} {len(items)}",
    )
    if len(items) == 2:
        c.check(
            items[0].get("created_at", "") >= items[1].get("created_at", ""),
            "诊断历史按 created_at 倒序",
            f"{items[0].get('created_at')} >= {items[1].get('created_at')}",
        )
    rdetail = client.get(f"/api/v1/diagnostics/{diagnostic_id}")
    c.check(
        rdetail.status_code == 200 and rdetail.json().get("id") == diagnostic_id,
        "GET /api/v1/diagnostics/{id} 返回单条详情",
        f"{rdetail.status_code}",
    )
    c.check(
        "llm_raw" not in rlist.text and "llm_raw" not in rdetail.text,
        "列表 / 详情响应均不含 llm_raw",
    )


# ---------------------------------------------------------------------------
# 组 3：数据不足冷启动（无快照也能诊断；不硬打分、不臆造）
# ---------------------------------------------------------------------------
def group3_cold_start(c: Checker, client: TestClient, fx: Fixture) -> None:
    assert fx.note2_id is not None
    model = DiagnosisScriptModel(responses=[COLD_DIAG_JSON])
    with _ScriptedModel(model):
        r = _post_generate(client, fx.note2_id)
    c.check(
        r.status_code == 201,
        "无快照的笔记也能诊断成功（数据不足不是错误）→ 201",
        f"{r.status_code} {r.text[:200]}",
    )
    data: dict[str, Any] = r.json() if r.status_code == 201 else {}
    c.check(
        data.get("score") is None,
        "冷启动**强制 score=null**（模型给了 77 也不采纳，不硬打分）",
        f"{data.get('score')}",
    )
    c.check(
        data.get("diagnosis_type") == "cold_start",
        "自动判定为 cold_start",
        f"{data.get('diagnosis_type')}",
    )
    c.check(
        data.get("based_on_metric_id") is None,
        "无快照 → based_on_metric_id = null",
        f"{data.get('based_on_metric_id')}",
    )
    gaps = (data.get("llm_meta") or {}).get("data_gaps") or []
    c.check(
        any("无运营数据快照" in item for item in gaps),
        "data_gaps 含「无运营数据快照」声明",
        f"{gaps}",
    )
    suggestions = data.get("suggestions") or []
    c.check(
        any("回填" in item for item in suggestions),
        "suggestions 以「尽快回填数据」为先（可操作）",
        f"{suggestions}",
    )
    c.check(
        any(
            word in item
            for item in suggestions
            for word in ("标题", "封面", "标签", "正文")
        ),
        "suggestions 含内容侧可检查项（明确是内容侧初步检查）",
        f"{suggestions}",
    )
    bench = data.get("benchmark") or {}
    c.check(
        bench.get("external_benchmark") is None,
        "冷启动同样不引入外部基准（external_benchmark = null）",
        f"{bench.get('external_benchmark')}",
    )
    latest_metrics = bench.get("latest_metrics") or {}
    c.check(
        latest_metrics and all(value is None for value in latest_metrics.values()),
        "latest_metrics 全为 null（不臆造任何数字）",
        f"{latest_metrics}",
    )
    prompt = model.prompts[0] if model.prompts else ""
    c.check(
        "无运营数据快照" in prompt and "cold_start" in prompt,
        "提示词把数据缺口与诊断类型如实喂给模型",
        f"{prompt[:120]}...",
    )
    c.check(
        _count_metrics(fx.note2_id) == 0,
        "冷启动笔记仍无快照（诊断不写 note_metrics）",
        f"{_count_metrics(fx.note2_id)}",
    )


# ---------------------------------------------------------------------------
# 组 4：失败路径
# ---------------------------------------------------------------------------
def group4_failures(c: Checker, client: TestClient, fx: Fixture) -> None:
    assert fx.note_id is not None and fx.note2_id is not None
    limit = max(1, settings.AGENT_JSON_MAX_ATTEMPTS)

    # 4.1 全坏 JSON → 回炉超限 → 400；模型调用次数 = 回路上界
    before_runs = _run_ids_now()
    model = DiagnosisScriptModel(responses=["这不是 JSON，只是普通文本。"])
    with _ScriptedModel(model):
        r = _post_generate(client, fx.note_id)
    c.check(
        r.status_code == 400 and r.json().get("code") == "bad_request",
        "结构校验连续超限 → 400（可重试）",
        f"{r.status_code} {r.text[:200]}",
    )
    c.check(
        "诊断失败" in r.json().get("message", ""),
        "错误消息为友好中文指引（含轮数说明）",
        f"{r.json().get('message')!r}",
    )
    c.check(
        "Traceback" not in r.text and 'File "' not in r.text,
        "失败响应不含原始堆栈",
    )
    c.check(
        model.calls == limit * limit,
        f"模型调用次数 = 图回炉 {limit} 轮 × 单轮 {limit} 次 = {limit * limit}（回路上界）",
        f"{model.calls}",
    )
    new_runs = _run_ids_now() - before_runs
    failed_rows = [_fetch_run(rid) for rid in sorted(new_runs)]
    c.check(
        len(new_runs) == 1 and str(failed_rows[0].get("status")) == "failed",
        "失败运行落 agent_runs=status:failed（审计不丢）",
        f"{[(row.get('status'), row.get('error_message')) for row in failed_rows]}",
    )
    if failed_rows:
        err_msg = str(failed_rows[0].get("error_message") or "")
        c.check(
            "诊断失败" in err_msg and "sk-" not in err_msg,
            "error_message 为可读摘要且无密钥前缀",
            f"{err_msg[:120]}",
        )

    # 4.2 LLM 未配置 → 400（构造阶段拒绝：零请求、不建 run）
    before_count = _count_runs()
    original_enabled = settings.AGENT_LLM_ENABLED
    settings.AGENT_LLM_ENABLED = False
    try:
        r = _post_generate(client, fx.note_id)  # 不注入假模型：走真实工厂闸门
    finally:
        settings.AGENT_LLM_ENABLED = original_enabled
    c.check(
        r.status_code == 400 and r.json().get("code") == "bad_request",
        "LLM 未配置（AGENT_LLM_ENABLED=false）→ 400",
        f"{r.status_code} {r.text[:200]}",
    )
    c.check(
        "未就绪" in r.json().get("message", "")
        and "AGENT_LLM_ENABLED" in json.dumps(r.json(), ensure_ascii=False),
        "未配置错误信息指向开关 / key（可操作指引）",
    )
    c.check(
        _count_runs() == before_count,
        "未配置时**不创建** agent_runs（零请求）",
        f"{_count_runs()} vs {before_count}",
    )

    # 4.3 note 不存在 → 404（前置校验，不消耗模型）
    before_runs = _run_ids_now()
    r = _post_generate(client, uuid.uuid4())
    c.check(
        r.status_code == 404 and r.json().get("code") == "not_found",
        "note 不存在 → 404",
        f"{r.status_code} {r.text[:200]}",
    )
    c.check(
        not (_run_ids_now() - before_runs),
        "note 不存在时**不创建** agent_runs（前置校验先于跑图）",
    )

    # 4.4 diagnosis_type 非法 → 422（schema 拦截）
    r = _post_generate(client, fx.note_id, diagnosis_type="weird")
    c.check(
        r.status_code == 422 and r.json().get("code") == "validation",
        "diagnosis_type 非法 → 422（schema 拦截）",
        f"{r.status_code} {r.text[:200]}",
    )

    # 4.5 GET 列表缺 note_id → 400；note 不存在 → 404；诊断不存在 → 404
    r = client.get("/api/v1/diagnostics")
    c.check(
        r.status_code == 400 and r.json().get("code") == "bad_request",
        "GET /diagnostics 缺 note_id → 400",
        f"{r.status_code}",
    )
    r = client.get(f"/api/v1/diagnostics?note_id={uuid.uuid4()}")
    c.check(
        r.status_code == 404 and r.json().get("code") == "not_found",
        "GET /diagnostics 的 note 不存在 → 404",
        f"{r.status_code}",
    )
    r = client.get(f"/api/v1/diagnostics/{uuid.uuid4()}")
    c.check(
        r.status_code == 404 and r.json().get("code") == "not_found",
        "GET /diagnostics/{id} 不存在 → 404",
        f"{r.status_code}",
    )


# ---------------------------------------------------------------------------
# 组 5：接口面与 schema
# ---------------------------------------------------------------------------
def group5_surface(c: Checker) -> None:
    paths = app.openapi()["paths"]
    c.check(
        sorted(paths.get("/api/v1/diagnostics", {})) == ["get"],
        "/api/v1/diagnostics 恰为 GET（诊断历史）",
        f"{sorted(paths.get('/api/v1/diagnostics', {}))}",
    )
    c.check(
        sorted(paths.get("/api/v1/diagnostics/generate", {})) == ["post"],
        "/api/v1/diagnostics/generate 恰为 POST（静态路径先于动态段注册）",
        f"{sorted(paths.get('/api/v1/diagnostics/generate', {}))}",
    )
    c.check(
        sorted(paths.get("/api/v1/diagnostics/{diagnostic_id}", {})) == ["get"],
        "/api/v1/diagnostics/{id} 恰为 GET（详情）",
        f"{sorted(paths.get('/api/v1/diagnostics/{diagnostic_id}', {}))}",
    )
    bad = [
        path
        for path in paths
        if "/diagnostics" in path
        and any(method in paths[path] for method in ("put", "patch", "delete"))
    ]
    c.check(not bad, "诊断路由不存在 PUT / PATCH / DELETE（只增不改）", f"{bad}")

    schemas = app.openapi()["components"]["schemas"]
    out_props = set(schemas["DiagnosticOut"]["properties"])
    c.check("llm_raw" not in out_props, "DiagnosticOut **不含** llm_raw", f"{sorted(out_props)}")
    sensitive = {"token", "cookie", "secret", "password", "llm_raw", "api_key", "screenshot_path"}
    c.check(
        not any(flag in name.lower() for flag in sensitive for name in out_props),
        "DiagnosticOut 无任何敏感字段",
        f"{sorted(out_props)}",
    )
    request_props = set(schemas["DiagnosticGenerateRequest"]["properties"])
    c.check(
        request_props == {"note_id", "diagnosis_type"},
        "DiagnosticGenerateRequest 属性恰为 note_id + diagnosis_type",
        f"{sorted(request_props)}",
    )
    meta_props = set(schemas["DiagnosticMeta"]["properties"])
    c.check(
        meta_props == META_KEYS,
        "DiagnosticMeta 白名单恰为五项",
        f"{sorted(meta_props)}",
    )

    main_src = (BACKEND_DIR / "app" / "main.py").read_text(encoding="utf-8")
    c.check(
        main_src.count("include_router(") >= 9 and "diagnostics_router" in main_src,
        "main.py include_router ≥ 9 且 diagnostics_router 在册（第 9 只，不回退）",
        f"{main_src.count('include_router(')}",
    )
    c.check(
        main_src.count("lifespan=") == 1 and main_src.count("async def lifespan") == 1,
        "全局 lifespan 仍唯一（第 16 步未新增）",
        f"lifespan={main_src.count('lifespan=')} / def={main_src.count('async def lifespan')}",
    )


# ---------------------------------------------------------------------------
# 组 6：零迁移
# ---------------------------------------------------------------------------
def group6_migrations(c: Checker) -> None:
    files = sorted(p.name for p in VERSIONS_DIR.glob("*.py") if not p.name.startswith("__"))
    c.check(len(files) == 2, "migrations/versions 仍恰 2 支迁移（本步零迁移）", f"{files}")
    c.check(
        all(name.startswith(("0001", "0002")) for name in files),
        "迁移文件名仍为 0001 / 0002（未新增 0003）",
        f"{files}",
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
# 组 7：合规扫描
# ---------------------------------------------------------------------------
def group7_compliance(c: Checker) -> None:
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
    # 第 14 步既有的发布外链仍恰 1 处（本步只在 published 状态并列新增跳转链接）
    studio_src = (
        FRONTEND_SRC / "app" / "notes" / "[id]" / "note-studio-client.tsx"
    ).read_text(encoding="utf-8")
    c.check(
        studio_src.count("xiaohongshu") == 1,
        "全前端平台域名仍恰 1 处（第 14 步发布外链；本步未新增，该文件由 selftest_studio 背书）",
        f"{studio_src.count('xiaohongshu')}",
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    c = Checker()
    print("=" * 78)
    print("第 16 步 · 单篇诊断 Agent 离线自检（不联网、不需要 API Key）")
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
        group1_pure_functions(c)
        group2_success(c, client, fx)
        group3_cold_start(c, client, fx)
        group4_failures(c, client, fx)
        group5_surface(c)
        group6_migrations(c)
        group7_compliance(c)
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
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for name in c.failures:
            print(f"  - FAIL: {name}")
        return 1
    print("全部 PASS ✔（第 16 步单篇诊断自检，依赖本地 docker PostgreSQL）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
