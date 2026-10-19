"""第 17 步 · 运营看板 + 周报 Agent 后端自检
（**依赖本地 docker PostgreSQL；不联网、不需要真实 API Key**）。

覆盖任务书要求的全部断言，分 9 组：

0. 图结构：周报图是真 LangGraph（五节点 + 条件边 + **有界回路**），
   ``load_context → generate_report → validate_report →（条件边）persist / 回炉 / give_up``；
1. 确定性纯函数：看板聚合（latest 每指标最后一个非空值、合计六率 4 位小数 /
   views<=0 → 全 None、覆盖率可识别、步长推导与 auto 收紧、趋势空桶 None
   不插值 / 桶内每笔记取最新再求和、排行降序缺失最后）+ 周报契约
   （周期解析与校验、单笔记周期增量主 / 退化口径、报告归一：解包 / 结构硬伤 /
   置信度越界宽容 / 数组限条）；
2. 看板接口：``GET /summary`` 计数七键 + 漏斗自洽重算（4 位小数）、
   ``GET /trend`` 三点精确值 / 无数据全 None / 不存在 404 / 参数 422、
   ``GET /ranking`` 降序缺失最后 / limit 生效 / 不含 draft / 参数 422；
3. 成功路径（假模型）：``POST /reports/weekly/generate`` 201 + 周期回显 +
   ``parse_attempts=1`` + 假模型恰 1 次调用 + 提示词含周期事实且首轮无回炉反馈
   + ``agent_runs`` 的 run_type/trigger/outputs/llm_raw 回填（脱敏）+
   响应无 llm_raw + ``GET /weekly`` 列表 / ``GET /weekly/{id}`` 详情 +
   看板与周报**全程只读**（八张业务表计数不变）；
4. 失败路径：全坏 JSON 超限 → 400 且模型调用次数 = 回路上界
   （``AGENT_JSON_MAX_ATTEMPTS × (1 + AGENT_NODE_MAX_RETRIES)`` = 9）+
   失败 run 记 failed；LLM 未配置 → 400 且**零请求零 run**；
   周期非法（越界 / 互斥 / 起点晚于终点 / 非法日期）→ 422 且零 run；
   响应均无 Traceback / 无密钥字样；
5. 接口面：六个端点方法精确（仅 GET / 仅 POST），无 PUT / PATCH / DELETE；
   响应 schema 无敏感字段；``main.py`` include_router ≥ 11 且 lifespan 唯一；
   周报定时**默认关闭**（字段默认值 + 实测 start 返回 None）；
6. 零迁移：``migrations/versions`` 仍恰 2 支、``alembic current`` = ``0002 (head)``、
   ``alembic check`` 退出码 0、public 业务表仍恰 10 张；
7. 合规扫描：本步新增 / 修改的后端、前端文件不含小红书域名 / 登录态凭据 /
   密钥前缀；全前端域名仍**恰 1 处**（第 14 步发布外链，本步未新增）；
   提示词含「严禁外部数字」硬约束；
8. 清理复查：测试数据（前缀 ``[SELFTEST]运营看板``）按 id 删 + 前缀兜底扫，
   双口径残留 0；检查点线程与 agent_runs 按**运行前后差集**安全清理。

HTTP 断言走**进程内** ``TestClient``（不需要 uvicorn）；数据复核走 SQLAlchemy 会话；
假模型经 monkeypatch 注入 ``app.agent.weekly.service.get_chat_model``
（**不发真实请求**）。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\selftest_dashboard.py

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

import app.agent.weekly.service as weekly_service  # noqa: E402
from app.agent.checkpoint import close_checkpointer, init_checkpointer  # noqa: E402
from app.agent.errors import AgentConfigError  # noqa: E402
from app.agent.weekly.contracts import (  # noqa: E402
    WeeklyContextError,
    build_period_facts,
    normalize_report_payload,
    period_delta,
    resolve_period,
)
from app.agent.weekly.graph import (  # noqa: E402
    NODE_GENERATE,
    NODE_GIVE_UP,
    NODE_LOAD_CONTEXT,
    NODE_PERSIST,
    NODE_VALIDATE,
    build_weekly_report_graph,
)
from app.core.config import Settings, settings  # noqa: E402
from app.core.timeutil import now_local  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.models.diagnostic import Diagnostic  # noqa: E402
from app.models.enums import (  # noqa: E402
    AccountStatus,
    ComplianceStatus,
    DataSource,
    DiagnosisType,
    NoteStatus,
    PersonaStatus,
    TopicStatus,
)  # noqa: E402
from app.models.hotspot import Hotspot  # noqa: E402
from app.models.metric import NoteMetric  # noqa: E402
from app.models.note import Note  # noqa: E402
from app.models.persona import Persona  # noqa: E402
from app.models.topic import Topic  # noqa: E402
from app.services.report.scheduler import (  # noqa: E402
    get_weekly_scheduler,
    start_weekly_scheduler,
)

#: 本脚本全部测试数据统一前缀（清理与复查的双口径都靠它兜底）
PREFIX = "[SELFTEST]运营看板"

VERSIONS_DIR = BACKEND_DIR / "migrations" / "versions"

#: 本步新增 / 修改过的后端文件（合规扫描对象）
SCANNED_FILES = (
    BACKEND_DIR / "app" / "agent" / "weekly" / "__init__.py",
    BACKEND_DIR / "app" / "agent" / "weekly" / "state.py",
    BACKEND_DIR / "app" / "agent" / "weekly" / "contracts.py",
    BACKEND_DIR / "app" / "agent" / "weekly" / "prompts.py",
    BACKEND_DIR / "app" / "agent" / "weekly" / "graph.py",
    BACKEND_DIR / "app" / "agent" / "weekly" / "service.py",
    BACKEND_DIR / "app" / "services" / "dashboard" / "__init__.py",
    BACKEND_DIR / "app" / "services" / "dashboard" / "aggregator.py",
    BACKEND_DIR / "app" / "services" / "dashboard" / "service.py",
    BACKEND_DIR / "app" / "services" / "report" / "__init__.py",
    BACKEND_DIR / "app" / "services" / "report" / "scheduler.py",
    BACKEND_DIR / "app" / "schemas" / "dashboard.py",
    BACKEND_DIR / "app" / "schemas" / "report.py",
    BACKEND_DIR / "app" / "api" / "routes" / "dashboard.py",
    BACKEND_DIR / "app" / "api" / "routes" / "reports.py",
    BACKEND_DIR / "app" / "models" / "enums.py",
    BACKEND_DIR / "app" / "core" / "config.py",
    BACKEND_DIR / "app" / "main.py",
)

#: 本步新增 / 修改过的前端文件（合规扫描对象）。
#: 注意：`notes/[id]/note-studio-client.tsx` **不在此清单** —— 该文件含第 14 步既有
#: 的「前往发布页」外链常量（含平台域名），已由 ``selftest_studio.py`` 的专用断言
#: 背书；组 7 另有「全前端域名恰 1 处」的守卫断言。
FRONTEND_SCANNED = (
    FRONTEND_SRC / "app" / "dashboard" / "page.tsx",
    FRONTEND_SRC / "app" / "api" / "v1" / "dashboard" / "summary" / "route.ts",
    FRONTEND_SRC / "app" / "api" / "v1" / "dashboard" / "trend" / "route.ts",
    FRONTEND_SRC / "app" / "api" / "v1" / "dashboard" / "ranking" / "route.ts",
    FRONTEND_SRC / "app" / "api" / "v1" / "reports" / "weekly" / "route.ts",
    FRONTEND_SRC / "app" / "api" / "v1" / "reports" / "weekly" / "generate" / "route.ts",
    FRONTEND_SRC / "app" / "api" / "v1" / "reports" / "weekly" / "[run_id]" / "route.ts",
    FRONTEND_SRC / "lib" / "charts" / "LineChart.tsx",
    FRONTEND_SRC / "lib" / "charts" / "HBars.tsx",
    FRONTEND_SRC / "lib" / "charts" / "FunnelBars.tsx",
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

#: summary.counts 的七键（与 schemas/dashboard.py 一致，测试侧硬编码）
COUNT_KEYS = {
    "accounts",
    "notes_total",
    "notes_published",
    "notes_ready",
    "notes_draft",
    "topics_writing",
    "notes_published_in_range",
}

#: 周报 persist 输出（agent_runs.outputs）的固定键（测试侧硬编码）
WEEKLY_OUTPUT_KEYS = {
    "report",
    "period_start",
    "period_end",
    "parse_attempts",
    "prompt_version",
    "persisted",
}

#: 业务表（public schema，不含 alembic_version）期望张数
EXPECTED_TABLES = 10

#: 失败路径的坏输出（非 JSON 纯文本；三个兜底层都会拿到它）
BAD_JSON_TEXT = "抱歉，我无法输出 JSON 格式，以下是说明文字。"

#: 正常周报载荷（假模型首轮响应；结构合法、建议非空）
GOOD_WEEKLY_JSON: dict[str, Any] = {
    "summary": (
        "本周期账号完成了新笔记发布并积累到首批数据。最新快照显示浏览量合计已在本地算好，"
        "互动结构以点赞与收藏为主；样本仍然偏少，建议先按「补齐快照 → 复盘互动率 → "
        "微调标题与发布节奏」的顺序推进。"
    ),
    "highlights": ["新发布笔记已积累浏览与点赞数据", "收藏数高于评论数，内容具备收藏价值"],
    "issues": ["仅少量笔记有完整指标数据，结论的稳健性有限"],
    "data_evidence": ["最新快照：views 合计与 likes 合计均来自周期事实中的本地计算值"],
    "next_week_suggestions": [
        "为存量笔记补齐数据快照：每篇至少回填发布后 24 小时与 7 天两个时间点",
        "在正文结尾增加互动引导句，观察评论率是否抬升",
        "按「周期事实」中增量最高的笔记方向复刻一篇选题",
    ],
    "data_gaps": ["建议补充发布后 24 小时快照"],
    "confidence": 0.7,
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
# 假模型（照 selftest_content_agent / selftest_diagnostics 的确定性脚本模型）
# ---------------------------------------------------------------------------
class WeeklyScriptModel(BaseChatModel):
    """确定性假模型：按调用次序取响应；末项会被重复使用（模拟模型固执己见）。"""

    responses: list[Any] = Field(default_factory=list)
    calls: int = 0
    prompts: list[str] = Field(default_factory=list)
    usage_in: int = 21
    usage_out: int = 34

    @property
    def _llm_type(self) -> str:
        return "weekly-script"

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
    """上下文管理器：把周报 service 的模型工厂换成假模型，退出时恢复。"""

    def __init__(self, model: WeeklyScriptModel) -> None:
        self.model = model
        self._original: Any = None

    def __enter__(self) -> WeeklyScriptModel:
        self._original = weekly_service.get_chat_model
        weekly_service.get_chat_model = lambda **kwargs: self.model  # noqa: ARG005
        return self.model

    def __exit__(self, *exc_info: Any) -> None:
        weekly_service.get_chat_model = self._original


class _UnconfiguredModel:
    """上下文管理器：模拟「LLM 未配置」（工厂抛 AgentConfigError）。

    用于断言 400 且**零请求零 agent_runs**——本机 .env 已配真实 key，
    必须在此处拦掉工厂，确保绝无真实网络调用。
    """

    def __enter__(self) -> None:
        self._original = weekly_service.get_chat_model

        def _raise(**kwargs: Any) -> Any:  # noqa: ARG001
            raise AgentConfigError("测试注入：LLM 未配置（开关关闭 / key 缺失 / 模型名占位）")

        weekly_service.get_chat_model = _raise

    def __exit__(self, *exc_info: Any) -> None:
        weekly_service.get_chat_model = self._original


# ---------------------------------------------------------------------------
# 夹具与清理
# ---------------------------------------------------------------------------
class Fixture:
    def __init__(self) -> None:
        self.account_id: uuid.UUID | None = None
        self.persona_id: uuid.UUID | None = None
        self.topic_id: uuid.UUID | None = None
        self.note_a_id: uuid.UUID | None = None   # 已发布 + 3 条快照（D-2 / D-1 / 今天各一）
        self.note_b_id: uuid.UUID | None = None   # 已发布 + 无快照（周期外发布）
        self.note_c_id: uuid.UUID | None = None   # 草稿
        self.note_d_id: uuid.UUID | None = None   # 待发布（ready）
        self.snapshot_ids: list[uuid.UUID] = []
        self.hotspot_id: uuid.UUID | None = None
        self.diagnostic_id: uuid.UUID | None = None
        self.before_threads: set[str] = set()
        self.before_run_ids: set[str] = set()

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
                title=f"{PREFIX}笔记A（周期内发布，3 快照）",
                body="用于看板聚合与周报事实的自检正文。",
                tags=["自检", "看板"],
                status=NoteStatus.PUBLISHED,
                compliance_status=ComplianceStatus.PASSED,
                published_at=now - timedelta(hours=2),
            )
            note_b = Note(
                account_id=account.id,
                persona_id=persona.id,
                title=f"{PREFIX}笔记B（周期外发布，无快照）",
                body="没有数据快照的已发布笔记（排行缺失值垫底 / 趋势全 None）。",
                tags=["自检"],
                status=NoteStatus.PUBLISHED,
                compliance_status=ComplianceStatus.PASSED,
                published_at=now - timedelta(days=10),
            )
            note_c = Note(
                account_id=account.id,
                persona_id=persona.id,
                title=f"{PREFIX}草稿C",
                body="草稿笔记（计数用；不得出现在排行里）。",
                tags=["自检"],
                status=NoteStatus.DRAFT,
                compliance_status=ComplianceStatus.PENDING,
            )
            note_d = Note(
                account_id=account.id,
                persona_id=persona.id,
                title=f"{PREFIX}待发布D",
                body="已定稿待发布（计数用）。",
                tags=["自检"],
                status=NoteStatus.READY,
                compliance_status=ComplianceStatus.PASSED,
            )
            for note in (note_a, note_b, note_c, note_d):
                db.add(note)
            db.flush()

            # 三条快照：D-2 / D-1 / 今天（同刻），views 100 → 180 → 260
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
            for snap in (snap_d2, snap_d1, snap_d0):
                db.add(snap)
            db.flush()

            hotspot = Hotspot(
                source="selftest",
                title=f"{PREFIX}热点",
                hot_date=now.date(),
                fetched_at=now,
                content_hash=(uuid.uuid4().hex + uuid.uuid4().hex),
            )
            db.add(hotspot)
            db.flush()

            diagnostic = Diagnostic(
                note_id=note_a.id,
                based_on_metric_id=snap_d0.id,
                diagnosis_type=DiagnosisType.REVIEW,
                score=80.0,
                strengths=["[SELFTEST]互动结构以点赞为主"],
                weaknesses=["[SELFTEST]样本偏少"],
                reasons=["[SELFTEST]基于最新快照的本地计算"],
                suggestions=["[SELFTEST]补充发布后 24 小时快照", "[SELFTEST]优化标题关键词"],
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
            self.note_d_id = note_d.id
            self.snapshot_ids = [snap_d2.id, snap_d1.id, snap_d0.id]
            self.hotspot_id = hotspot.id
            self.diagnostic_id = diagnostic.id
            db.commit()

    # ------------------------------------------------------------- 清理
    def cleanup(self) -> None:
        note_ids = [
            str(item)
            for item in (self.note_a_id, self.note_b_id, self.note_c_id, self.note_d_id)
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
        # 3) 业务数据：diagnostics / covers / note_metrics / hotspots → notes
        #    → topics → personas → accounts（按 id + 前缀双口径）
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
            if self.diagnostic_id is not None:
                db.execute(
                    text("DELETE FROM diagnostics WHERE id = :d"),
                    {"d": str(self.diagnostic_id)},
                )
            db.execute(text("DELETE FROM hotspots WHERE title LIKE :t"), {"t": like})
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
            for item in (self.note_a_id, self.note_b_id, self.note_c_id, self.note_d_id)
            if item is not None
        ]
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
                "hotspots(prefix)": db.execute(
                    text("SELECT count(*) FROM hotspots WHERE title LIKE :t"), {"t": like}
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


def _fetch_run(run_id: str) -> dict[str, Any]:
    cols = (
        "id, run_type, trigger, status, outputs, llm_raw, error_message, "
        "json_parse_attempts, prompt_tokens, completion_tokens, total_tokens, llm_model"
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


def _table_counts() -> dict[str, int]:
    """八张业务表的行数快照（只读断言：看板与周报全程不得改动业务数据）。"""
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
            table: int(db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608
            for table in tables
        }


def _post_generate(client: TestClient, payload: dict[str, Any] | None = None):
    return client.post("/api/v1/reports/weekly/generate", json=payload or {})


def _snap(index: int, captured_at: datetime, *, note_id: str, **values: Any) -> dict[str, Any]:
    """构造一条普通 dict 快照（缺省指标全部为 None；captured_at 用 ISO 串）。"""
    row: dict[str, Any] = {
        "id": f"00000000-0000-0000-0000-0000000000{index:02d}",
        "note_id": note_id,
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
    graph = build_weekly_report_graph(
        model=WeeklyScriptModel(responses=[GOOD_WEEKLY_JSON]),
        use_checkpointer=False,
    )
    c.check(
        "Compiled" in type(graph).__name__,
        "build_weekly_report_graph 返回已编译的 LangGraph 图",
        f"{type(graph).__name__}",
    )
    view = graph.get_graph()
    nodes = set(view.nodes.keys())
    business = {node for node in nodes if not node.startswith("__")}
    c.check(
        business
        == {NODE_LOAD_CONTEXT, NODE_GENERATE, NODE_VALIDATE, NODE_PERSIST, NODE_GIVE_UP},
        "图含 load_context / generate_report / validate_report / persist_run / give_up 五节点",
        f"实际={sorted(nodes)}",
    )
    edges = {(edge.source, edge.target, bool(edge.conditional)) for edge in view.edges}
    c.check(
        (NODE_LOAD_CONTEXT, NODE_GENERATE, False) in edges,
        "load_context → generate_report 为直连边",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_GENERATE, NODE_VALIDATE, False) in edges,
        "generate_report → validate_report 为直连边",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_VALIDATE, NODE_GENERATE, True) in edges,
        "validate_report → generate_report 为条件边（回炉回路）",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_VALIDATE, NODE_PERSIST, True) in edges,
        "validate 通过 → persist_run 为条件边",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_VALIDATE, NODE_GIVE_UP, True) in edges,
        "validate 超限 → give_up 为条件边",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_PERSIST, "__end__", False) in edges and (NODE_GIVE_UP, "__end__", False) in edges,
        "persist_run / give_up → END",
        f"edges={sorted(edges)}",
    )
    # 回路有界：路由函数必须绑定 AGENT_JSON_MAX_ATTEMPTS（行为面在组 4 验证 3×3=9）
    graph_src = (BACKEND_DIR / "app" / "agent" / "weekly" / "graph.py").read_text(
        encoding="utf-8"
    )
    c.check(
        "AGENT_JSON_MAX_ATTEMPTS" in graph_src,
        "回路上界绑定 AGENT_JSON_MAX_ATTEMPTS（有界回路，不无限重试）",
    )
    c.check(
        'new_state_graph("weekly-report", WeeklyState)' in graph_src,
        "图用 new_state_graph 装配且状态为 WeeklyState（复用 harness 基座）",
    )
    service_src = (BACKEND_DIR / "app" / "agent" / "weekly" / "service.py").read_text(
        encoding="utf-8"
    )
    c.check(
        "run_agent(" in service_src and 'kind="weekly_report"' in service_src,
        "周报经 app.agent.runner.run_agent 运行（第五个业务图，禁止裸 HTTP 自编排）",
    )


# ---------------------------------------------------------------------------
# 组 1：确定性纯函数（看板聚合 + 周报契约）
# ---------------------------------------------------------------------------
def group1_pure_functions(c: Checker) -> None:
    from app.services.dashboard.aggregator import (
        aggregate_trend,
        as_datetime,
        latest_values_per_note,
        range_start_local,
        rank_items,
        rates_from_sums,
        resolve_step_days,
        sum_funnel,
    )

    tz = dt_timezone(timedelta(hours=8))
    t0 = datetime(2026, 10, 1, 0, 0, tzinfo=tz)
    h1 = timedelta(hours=1)

    # --- 1.1 as_datetime：宽容解析 ---
    c.check(
        as_datetime(t0) == t0 and as_datetime(t0.isoformat()) == t0,
        "as_datetime 支持 datetime 与 ISO 字符串（原样往返）",
        f"{as_datetime(t0)}, {as_datetime(t0.isoformat())}",
    )
    c.check(
        as_datetime("not-a-time") is None and as_datetime(None) is None,
        "as_datetime 非法输入 → None（不抛异常，调用侧可识别）",
    )

    # --- 1.2 latest_values_per_note：每指标最后一个非空值（残缺快照不覆盖）---
    snaps = [
        _snap(
            1,
            t0,
            note_id="nA",
            views=1000,
            likes=100,
            collects=50,
            comments=10,
            shares=5,
            new_followers=20,
        ),
        _snap(2, t0 + h1, note_id="nA", views=None, likes=260),
        _snap(3, t0 + h1, note_id="nB", views=7),
    ]
    latest = latest_values_per_note(snaps)
    c.check(
        latest["nA"]
        == {
            "views": 1000,
            "likes": 260,
            "collects": 50,
            "comments": 10,
            "shares": 5,
            "new_followers": 20,
        },
        "latest_values_per_note 取每个指标的最后一个非空值（后一条为空时向前取）",
        f"{latest.get('nA')}",
    )
    c.check(
        latest["nB"]["views"] == 7 and latest["nB"]["likes"] is None,
        "latest_values_per_note 全程缺失的指标恒为 None（不臆造）",
        f"{latest.get('nB')}",
    )

    # --- 1.3 rates_from_sums：合计口径六率（4 位小数；views<=0 → 全 None）---
    sums = {
        "views": 1000,
        "likes": 100,
        "collects": 50,
        "comments": 10,
        "shares": 5,
        "new_followers": 20,
    }
    rates = rates_from_sums(sums)
    c.check(
        rates["like_rate"] == 0.1
        and rates["collect_rate"] == 0.05
        and rates["comment_rate"] == 0.01
        and rates["share_rate"] == 0.005
        and rates["follow_rate"] == 0.02
        and rates["engagement_rate"] == 0.165,
        "rates_from_sums 六率按合计口径、4 位小数（engagement=(赞+藏+评+转)/浏览）",
        f"{rates}",
    )
    zero_rates = rates_from_sums({**sums, "views": 0})
    none_rates = rates_from_sums({**sums, "views": None})
    c.check(
        all(value is None for value in zero_rates.values())
        and all(value is None for value in none_rates.values()),
        "views 合计为 0 / 缺失 → 六率全 None（绝不臆造）",
        f"{zero_rates}",
    )

    # --- 1.4 sum_funnel：多笔记求和 + 覆盖率可识别 ---
    funnel = sum_funnel(
        {
            "nA": {field: 10 for field in METRIC_KEYS},
            "nB": {"views": 5, "likes": None, "collects": None, "comments": None,
                   "shares": None, "new_followers": None},
            "nC": {field: None for field in METRIC_KEYS},
        }
    )
    c.check(
        funnel["latest_totals"]["views"] == 15
        and funnel["latest_totals"]["likes"] == 10
        and funnel["metric_coverage"]["views"] == 2
        and funnel["metric_coverage"]["likes"] == 1,
        "sum_funnel 缺失按 0 求和、metric_coverage 逐指标可识别",
        f"{funnel}",
    )
    c.check(
        funnel["covered_notes"] == 2,
        "covered_notes 只统计「至少一个非空值」的笔记（nC 不计）",
        f"{funnel['covered_notes']}",
    )
    c.check(
        funnel["rates"]["like_rate"] == round(10 / 15, 4),
        "sum_funnel 的 rates 与 latest_totals 同源（合计口径自洽）",
        f"{funnel['rates']}",
    )

    # --- 1.5 resolve_step_days：步长推导与 auto 收紧 ---
    c.check(
        resolve_step_days(3, "day", 30) == (1, "day"),
        "区间未超上限：day 桶步长 1 天",
        f"{resolve_step_days(3, 'day', 30)}",
    )
    c.check(
        resolve_step_days(30, "day", 30) == (1, "day"),
        "区间恰等于上限：day 桶步长 1 天（30 点）",
        f"{resolve_step_days(30, 'day', 30)}",
    )
    c.check(
        resolve_step_days(90, "day", 30) == (3, "auto"),
        "区间超上限：自动收紧为等间隔（90/30 → 3 天，模式如实回“auto”）",
        f"{resolve_step_days(90, 'day', 30)}",
    )
    c.check(
        resolve_step_days(365, "auto", 30) == (13, "auto"),
        "auto 桶：步长 = ceil(range/max_points)（365/30 → 13 天）",
        f"{resolve_step_days(365, 'auto', 30)}",
    )

    # --- 1.6 aggregate_trend：空桶 None 不插值、桶内每笔记取最新再求和 ---
    now = datetime(2026, 10, 12, 12, 0, tzinfo=tz)
    trend_snaps = [
        _snap(11, now - timedelta(days=2) + timedelta(hours=2), note_id="nA", views=100),
        _snap(12, now - timedelta(days=1), note_id="nA", views=180),
        _snap(13, now - timedelta(hours=2), note_id="nA", views=200),   # 同桶较早
        _snap(14, now - timedelta(minutes=5), note_id="nA", views=260),  # 同桶最新 → 取
        _snap(15, now - timedelta(hours=1), note_id="nB", views=40),     # 另一笔记求和
    ]
    trend = aggregate_trend(
        trend_snaps, metric="views", range_days=3, step_days=1, now=now
    )
    c.check(
        [point["value"] for point in trend["points"]] == [100, 180, 300],
        "aggregate_trend 桶内每笔记取最新非空值再求和（200 被 260 覆盖；nB 并入 → 300）",
        f"{trend['points']}",
    )
    c.check(
        trend["points"][0]["bucket"] == (now.date() - timedelta(days=2)).isoformat()
        and trend["notes_with_data"] == 2
        and trend["points_with_data"] == 3,
        "趋势桶标签为本地自然日、覆盖计数正确",
        f"{trend['points']}, notes={trend['notes_with_data']}",
    )
    gap_trend = aggregate_trend(
        [_snap(16, now, note_id="nA", views=9)],
        metric="views",
        range_days=4,
        step_days=1,
        now=now,
    )
    c.check(
        gap_trend["points"][0]["value"] is None
        and [point["value"] for point in gap_trend["points"][1:]] == [None, None, 9],
        "空桶给 None（禁止补 0 / 插值），只有今天桶有值",
        f"{gap_trend['points']}",
    )
    c.check(
        range_start_local(now, 4).isoformat() == (now - timedelta(days=3)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat(),
        "range_start_local 为「近 N 自然日（含今天）」的本地 00:00 起点",
        f"{range_start_local(now, 4)}",
    )

    # --- 1.7 rank_items：降序 / 缺失最后 / tie 按发布晚优先 / limit ---
    meta = [
        {"note_id": "nA", "title": "A", "published_at": "2026-10-10T10:00:00+08:00"},
        {"note_id": "nB", "title": "B", "published_at": "2026-10-11T10:00:00+08:00"},
        {"note_id": "nC", "title": "C", "published_at": "2026-10-12T10:00:00+08:00"},
        {"note_id": "nD", "title": "D", "published_at": "2026-10-09T10:00:00+08:00"},
    ]
    rank_latest = {
        "nA": {"views": 50},
        "nB": {"views": 50},
        "nC": {"views": None},   # 无值 → 最后（即使标题/时间更新）
        "nD": {"views": 10},
    }
    ranked = rank_items(rank_latest, meta, metric="views", limit=10)
    c.check(
        [item["note_id"] for item in ranked] == ["nB", "nA", "nD", "nC"],
        "rank_items 降序 + 缺失排最后 + tie 按发布晚优先",
        f"{[item['note_id'] for item in ranked]}",
    )
    c.check(
        len(rank_items(rank_latest, meta, metric="views", limit=2)) == 2,
        "rank_items limit 截断生效",
    )

    # --- 1.8 周报周期解析（纯函数）：默认 / 显式 / 非法 ---
    now_l = now_local()
    start, end, span = resolve_period(now=now_l)
    default_days = max(1, int(settings.WEEKLY_DEFAULT_DAYS))
    c.check(
        span == default_days
        and start.hour == 0
        and start.date() == now_l.date() - timedelta(days=default_days - 1)
        and end == now_l,
        f"resolve_period 默认周期 = 近 {default_days} 天（含今天）的本地 00:00 起",
        f"{start} ~ {end} span={span}",
    )
    s2, e2, span2 = resolve_period(period_start="2026-10-01", period_end="2026-10-07")
    c.check(
        span2 == 7 and s2.hour == 0 and e2.hour == 23 and e2.minute == 59,
        "resolve_period 显式日期：起点整天 00:00、终点整天 23:59",
        f"{s2} ~ {e2} span={span2}",
    )
    bad_cases: list[tuple[str, dict[str, Any]]] = [
        ("days 越界（0）", {"days": 0}),
        ("days 越界（>上限）", {"days": 10**6}),
        ("days 与显式起止互斥", {"days": 3, "period_start": "2026-10-01"}),
        ("起点晚于终点", {"period_start": "2026-06-01", "period_end": "2026-05-01"}),
        ("非法日期串", {"period_start": "not-a-date"}),
    ]
    all_raised = True
    for label, kwargs in bad_cases:
        try:
            resolve_period(**kwargs)
            all_raised = False
            print(f"       -> {label} 未抛 WeeklyContextError")
        except WeeklyContextError:
            pass
    c.check(all_raised, "resolve_period 五类非法输入全部抛 WeeklyContextError", "")

    # --- 1.9 单笔记周期增量：主口径 / 退化口径 / 无基线 None ---
    d_now = datetime(2026, 10, 12, 12, 0, tzinfo=tz)
    delta_snaps = [
        _snap(21, d_now - timedelta(days=3), note_id="nA", views=100, likes=10),
        _snap(22, d_now - timedelta(days=1), note_id="nA", views=180, likes=18),
        _snap(23, d_now, note_id="nA", views=260, likes=26),
    ]
    main_delta = period_delta(
        delta_snaps, start=d_now - timedelta(days=2), end=d_now
    )
    c.check(
        main_delta["views"] == 160 and main_delta["likes"] == 16,
        "period_delta 主口径 = 周期内末值 − 周期前末值（260-100=160）",
        f"{main_delta}",
    )
    degraded = period_delta(delta_snaps[1:], start=d_now - timedelta(days=2), end=d_now)
    c.check(
        degraded["views"] == 80,
        "period_delta 无周期前基线时退化为「周期内末 − 周期内首」（260-180=80）",
        f"{degraded}",
    )
    single = period_delta([delta_snaps[-1]], start=d_now - timedelta(days=2), end=d_now)
    c.check(
        single["views"] is None,
        "period_delta 只有一条周期内快照且无基线 → None（不算增量）",
        f"{single}",
    )

    # --- 1.10 报告归一：解包 / 硬伤 / 置信度宽容 / 限长 ---
    normalized, errors, warnings = normalize_report_payload(
        {"report": GOOD_WEEKLY_JSON},
        period_start="2026-10-06T00:00:00+08:00",
        period_end="2026-10-12T21:00:00+08:00",
    )
    c.check(
        not errors
        and normalized["summary"] == GOOD_WEEKLY_JSON["summary"]
        and len(normalized["next_week_suggestions"]) == 3
        and normalized["period_start"] == "2026-10-06T00:00:00+08:00",
        "normalize_report_payload 宽容解包 report 包装层、周期以程序事实为准",
        f"errors={errors}",
    )
    _, err_empty, _ = normalize_report_payload(
        {**GOOD_WEEKLY_JSON, "next_week_suggestions": []},
        period_start="a",
        period_end="b",
    )
    c.check(
        len(err_empty) >= 1,
        "next_week_suggestions 为空 → 硬伤（触发回炉）",
        f"{err_empty}",
    )
    _, err_bad, _ = normalize_report_payload(
        ["不是对象"], period_start="a", period_end="b"
    )
    c.check(len(err_bad) >= 1, "非对象载荷 → 硬伤（触发回炉）", f"{err_bad}")
    fixed, err_fixed, warn_fixed = normalize_report_payload(
        {**GOOD_WEEKLY_JSON, "confidence": 7.5, "summary": "长" * 2000},
        period_start="a",
        period_end="b",
        summary_max_len=600,
    )
    c.check(
        not err_fixed and fixed["confidence"] is None and len(fixed["summary"]) == 600,
        "置信度越界 → None + warning（宽容，不打回）；summary 超长截断",
        f"conf={fixed['confidence']} summary_len={len(fixed['summary'])} warn={warn_fixed}",
    )


# ---------------------------------------------------------------------------
# 组 1b：周期事实构建（触库；复用看板取数管道）
# ---------------------------------------------------------------------------
def group1b_period_facts(c: Checker, fx: Fixture) -> dict[str, Any]:
    now_l = now_local()
    start, end, _span = resolve_period(days=7, now=now_l)
    with SessionLocal() as db:
        facts = build_period_facts(db, period_start=start, period_end=end)

    counts = facts["counts"]
    c.check(
        counts["notes_published_in_period"] >= 1
        and counts["notes_ready"] >= 1
        and counts["notes_draft"] >= 1
        and counts["topics_writing"] >= 1,
        "build_period_facts 四项计数 ≥ 夹具下限（周期内发布 / ready / draft / 在写选题）",
        f"{counts}",
    )
    htc = facts["hotspot_topic_counts"]
    c.check(
        htc["hotspots_fetched_in_period"] >= 1 and htc["topics_created_in_period"] >= 1,
        "build_period_facts 热点 / 选题计数 ≥ 夹具下限（被采用热点为当前累计 _total）",
        f"{htc}",
    )
    totals = facts["period_totals"]
    c.check(
        totals["latest_totals"]["views"] >= 260
        and totals["covered_notes"] <= totals["published_notes"]
        and set(totals["metric_coverage"]) == set(METRIC_KEYS),
        "周期合计复用看板口径（views ≥ 260、覆盖率可识别、六指标齐全）",
        f"views={totals['latest_totals']['views']} covered={totals['covered_notes']}/{totals['published_notes']}",
    )
    c.check(
        (facts["period_deltas"]["views"] or 0) >= 160,
        "周期增量合计 ≥ 夹具下限（笔记A Δviews=160 至少在内）",
        f"{facts['period_deltas']}",
    )
    # 保留「契约字段齐全」的超集断言：允许后续在列表项上新增字段；
    # 改成严格相等会让任何向后兼容的新增字段都判失败。
    c.check(
        any(str(item.get("title", "")).startswith(PREFIX) for item in facts["new_published_notes"])
        and all(
            set(item.keys()) >= {"note_id", "title", "published_at", "latest", "deltas"}
            for item in facts["new_published_notes"]
        ),
        "new_published_notes 含夹具笔记且字段齐全（title / published_at / 最新值 / 增量）",
        f"{[item.get('title') for item in facts['new_published_notes']][:3]}",
    )
    c.check(
        any(
            "[SELFTEST]" in suggestion
            for item in facts["diagnostics"]
            for suggestion in item["suggestions"]
        ),
        "diagnostics 摘要含周期内夹具诊断（类型 / 评分 / 核心建议）",
        f"{len(facts['diagnostics'])} 条",
    )
    gaps = facts["data_gaps"]
    c.check(
        isinstance(gaps, list) and all(isinstance(item, str) for item in gaps),
        "data_gaps 为字符串列表（数据不足如实声明）",
        f"{gaps}",
    )
    return facts


# ---------------------------------------------------------------------------
# 组 2：看板接口（只读；进程内 TestClient）
# ---------------------------------------------------------------------------
def group2_dashboard_api(c: Checker, client: TestClient, fx: Fixture) -> None:
    # --- 2.1 summary ---
    resp = client.get("/api/v1/dashboard/summary", params={"range_days": 7})
    c.check(resp.status_code == 200, "GET /dashboard/summary 200", f"{resp.status_code}")
    body = resp.json()
    counts = body.get("counts") or {}
    c.check(
        set(counts) == COUNT_KEYS,
        "summary.counts 恰为七键（账号 / 笔记总数 / 已发布 / 待发布 / 草稿 / 在写选题 / 周期内发布）",
        f"{sorted(counts)}",
    )
    c.check(
        counts.get("notes_published_in_range", 0) >= 1
        and counts.get("notes_ready", 0) >= 1
        and counts.get("notes_draft", 0) >= 1
        and counts.get("topics_writing", 0) >= 1,
        "summary 计数 ≥ 夹具下限（周期内发布 / ready / draft / writing）",
        f"{counts}",
    )
    c.check(
        body.get("range_days") == 7 and bool(body.get("generated_at")),
        "summary 回显 range_days 且带 generated_at",
        f"{body.get('range_days')} / {body.get('generated_at')}",
    )
    funnel = body.get("funnel") or {}
    totals = funnel.get("latest_totals") or {}
    rates = funnel.get("rates") or {}
    c.check(
        set(totals) == set(METRIC_KEYS)
        and set(rates) == set(RATE_KEYS)
        and set(funnel.get("metric_coverage") or {}) == set(METRIC_KEYS),
        "summary.funnel 六指标 + 六率 + 覆盖率键齐全",
        f"{sorted(funnel)}",
    )
    views = totals.get("views") or 0
    expected_rates = {
        "like_rate": round((totals.get("likes") or 0) / views, 4),
        "collect_rate": round((totals.get("collects") or 0) / views, 4),
        "comment_rate": round((totals.get("comments") or 0) / views, 4),
        "share_rate": round((totals.get("shares") or 0) / views, 4),
        "follow_rate": round((totals.get("new_followers") or 0) / views, 4),
        "engagement_rate": round(
            (
                (totals.get("likes") or 0)
                + (totals.get("collects") or 0)
                + (totals.get("comments") or 0)
                + (totals.get("shares") or 0)
            )
            / views,
            4,
        ),
    }
    c.check(
        views > 0 and rates == expected_rates,
        "summary.funnel.rates 与 latest_totals 自洽重算一致（4 位小数、合计口径）",
        f"views={views} rates={rates}",
    )
    c.check(
        funnel.get("covered_notes", 0) <= funnel.get("published_notes", 0)
        and funnel.get("published_notes", 0) >= 2,
        "funnel 覆盖笔记数 ≤ 已发布总数（覆盖率可识别）",
        f"covered={funnel.get('covered_notes')} published={funnel.get('published_notes')}",
    )

    # --- 2.2 summary 参数校验 ---
    c.check(
        client.get("/api/v1/dashboard/summary", params={"range_days": 0}).status_code == 422
        and client.get("/api/v1/dashboard/summary", params={"range_days": 366}).status_code
        == 422,
        "summary range_days 越界（0 / 366）→ 422（服务端校验）",
    )

    # --- 2.3 trend：指定笔记三点精确值（D-2/D-1/今天）---
    resp = client.get(
        "/api/v1/dashboard/trend",
        params={
            "metric": "views",
            "range_days": 3,
            "note_id": str(fx.note_a_id),
            "bucket": "day",
        },
    )
    c.check(resp.status_code == 200, "GET /dashboard/trend 200", f"{resp.status_code}")
    trend = resp.json()
    points = trend.get("points") or []
    c.check(
        [point.get("value") for point in points] == [100, 180, 260]
        and len(points) == 3,
        "trend（note A / views）三点精确值 [100, 180, 260]（桶内取最新、不插值）",
        f"{[point.get('value') for point in points]}",
    )
    coverage = trend.get("coverage") or {}
    c.check(
        coverage.get("step_days") == 1
        and coverage.get("points_total") == 3
        and coverage.get("notes_with_data") == 1
        and coverage.get("points_with_data") == 3,
        "trend.coverage 如实回传步长 / 点数 / 覆盖计数",
        f"{coverage}",
    )
    c.check(
        trend.get("metric") == "views" and trend.get("bucket") == "day",
        "trend 回显 metric 与生效 bucket",
        f"{trend.get('metric')} / {trend.get('bucket')}",
    )

    # --- 2.4 trend：无快照笔记 → 全 None（空集合语义，不报错）---
    resp = client.get(
        "/api/v1/dashboard/trend",
        params={
            "metric": "views",
            "range_days": 3,
            "note_id": str(fx.note_b_id),
            "bucket": "day",
        },
    )
    body_b = resp.json()
    c.check(
        resp.status_code == 200
        and all(point.get("value") is None for point in (body_b.get("points") or []))
        and (body_b.get("coverage") or {}).get("points_with_data") == 0,
        "无快照笔记 trend：200 + 全 None（不报错、不补 0）",
        f"{[point.get('value') for point in (body_b.get('points') or [])]}",
    )

    # --- 2.5 trend：全账号 ---
    resp = client.get(
        "/api/v1/dashboard/trend",
        params={"metric": "views", "range_days": 30, "bucket": "auto"},
    )
    body_all = resp.json()
    c.check(
        resp.status_code == 200
        and (body_all.get("coverage") or {}).get("notes", 0) >= 2
        and bool(body_all.get("points")),
        "全账号 trend 200 且覆盖计数 ≥ 夹具已发布数",
        f"{body_all.get('coverage')}",
    )

    # --- 2.6 trend 失败面 ---
    missing = uuid.uuid4()
    c.check(
        client.get(
            "/api/v1/dashboard/trend",
            params={"metric": "views", "note_id": str(missing)},
        ).status_code
        == 404,
        "trend 指定不存在的 note_id → 404",
    )
    c.check(
        client.get(
            "/api/v1/dashboard/trend",
            params={"metric": "views", "note_id": "not-a-uuid"},
        ).status_code
        == 422,
        "trend note_id 非法 UUID → 422",
    )
    c.check(
        client.get("/api/v1/dashboard/trend", params={"metric": "bogus"}).status_code == 422,
        "trend metric 非法 → 422（枚举 Literal 校验）",
    )

    # --- 2.7 ranking ---
    resp = client.get(
        "/api/v1/dashboard/ranking", params={"metric": "likes", "limit": 100}
    )
    c.check(resp.status_code == 200, "GET /dashboard/ranking 200", f"{resp.status_code}")
    rank = resp.json()
    items = rank.get("items") or []
    ids = [item.get("note_id") for item in items]
    values = [item.get("value") for item in items]
    non_null = [value for value in values if value is not None]
    c.check(
        non_null == sorted(non_null, reverse=True)
        and all(value is None for value in values[len(non_null):]),
        "ranking 按最新非空值降序、缺失值全部排在最后",
        f"{values}",
    )
    c.check(
        str(fx.note_a_id) in ids
        and next(item["value"] for item in items if item["note_id"] == str(fx.note_a_id))
        == 26,
        "ranking 含夹具笔记A（likes=26）",
        f"{ids[:5]}",
    )
    c.check(
        str(fx.note_c_id) not in ids,
        "ranking 不含 draft 笔记（仅 published 口径）",
    )
    c.check(
        len((client.get("/api/v1/dashboard/ranking", params={"metric": "likes", "limit": 2}).json().get("items") or []))
        == 2,
        "ranking limit 生效（2 条）",
    )
    c.check(
        len((client.get("/api/v1/dashboard/ranking", params={"metric": "views"}).json().get("items") or []))
        <= settings.DASHBOARD_RANKING_TOP_N,
        f"ranking 默认 limit = DASHBOARD_RANKING_TOP_N（{settings.DASHBOARD_RANKING_TOP_N}）",
    )
    c.check(
        client.get("/api/v1/dashboard/ranking", params={"metric": "bogus"}).status_code == 422
        and client.get(
            "/api/v1/dashboard/ranking", params={"metric": "views", "limit": 0}
        ).status_code
        == 422,
        "ranking metric / limit 非法 → 422",
    )


# ---------------------------------------------------------------------------
# 组 3：周报成功路径（假模型）
# ---------------------------------------------------------------------------
def group3_weekly_success(c: Checker, client: TestClient, fx: Fixture) -> None:
    before_runs = _count_runs()
    baseline_tables = _table_counts()

    model = WeeklyScriptModel(responses=[GOOD_WEEKLY_JSON])
    with _ScriptedModel(model) as fake:
        resp = _post_generate(client, {"days": 7})

    c.check(resp.status_code == 201, "POST /reports/weekly/generate 201（同步生成）", f"{resp.status_code}")
    body = resp.json()
    run_id = str(body.get("run_id") or "")
    c.check(bool(run_id), "响应含 run_id", f"{run_id}")
    c.check(
        fake.calls == 1,
        "成功路径假模型恰 1 次调用（首轮解析成功）",
        f"calls={fake.calls}",
    )
    c.check(
        body.get("parse_attempts") == 1,
        "响应 parse_attempts = 1（首轮即合法 JSON）",
        f"{body.get('parse_attempts')}",
    )
    report = body.get("report") or {}
    c.check(
        set(report) >= {
            "period_start",
            "period_end",
            "summary",
            "highlights",
            "issues",
            "data_evidence",
            "next_week_suggestions",
            "data_gaps",
            "confidence",
        },
        "report 九键齐全（周期 / 概述 / 亮点 / 问题 / 依据 / 建议 / 缺口 / 置信度）",
        f"{sorted(report)}",
    )
    c.check(
        len(report.get("next_week_suggestions") or []) == 3
        and report.get("summary", "").startswith("本周期"),
        "报告建议非空且按序、summary 原样保留",
        f"建议 {len(report.get('next_week_suggestions') or [])} 条",
    )
    c.check(
        report.get("period_start") == body.get("period_start")
        and report.get("period_end") == body.get("period_end"),
        "周期回显一致（顶层与 report 内相同）",
        f"{body.get('period_start')} ~ {body.get('period_end')}",
    )

    # --- 提示词断言 ---
    prompt = fake.prompts[-1] if fake.prompts else ""
    c.check(
        "周期事实" in prompt and "只输出一个 JSON 对象" in prompt,
        "提示词含「周期事实」底稿与严格 JSON 输出要求",
    )
    c.check(
        PREFIX in prompt,
        "提示词含夹具笔记标题（周期事实真实注入）",
    )
    c.check(
        "上一次的输出存在下列问题" not in prompt,
        "首轮无回炉反馈块（无中生有会被抓出）",
    )
    c.check(
        "严禁引入任何外部数字" in prompt,
        "提示词含「严禁外部数字」硬约束（不编造行业均值 / 爆款阈值）",
    )

    # --- 响应体安全面 ---
    c.check(
        "llm_raw" not in resp.text and "sk-" not in resp.text,
        "生成响应不含 llm_raw / 密钥字样",
    )

    # --- agent_runs 复核 ---
    run = _fetch_run(run_id)
    c.check(
        run.get("run_type") == "weekly_report" and run.get("trigger") == "manual",
        "agent_runs 行 run_type=weekly_report、trigger=manual",
        f"{run.get('run_type')} / {run.get('trigger')}",
    )
    c.check(
        run.get("status") == "success",
        "run 状态 success（persist_run 节点正常完成）",
        f"{run.get('status')}",
    )
    outputs = run.get("outputs") or {}
    c.check(
        WEEKLY_OUTPUT_KEYS <= set(outputs),
        "outputs 含 report / 周期 / parse_attempts / prompt_version / persisted",
        f"{sorted(outputs)}",
    )
    c.check(
        outputs.get("persisted") is True
        and (outputs.get("report") or {}).get("summary") == GOOD_WEEKLY_JSON["summary"],
        "outputs.report 为归一后完整报告（persisted=true）",
    )
    c.check(
        outputs.get("prompt_version") == settings.WEEKLY_PROMPT_VERSION,
        "outputs.prompt_version = 配置值（WEEKLY_PROMPT_VERSION）",
        f"{outputs.get('prompt_version')}",
    )
    c.check(
        "llm_raw" not in outputs,
        "outputs **不含** llm_raw（llm_raw 只进 agent_runs 列、接口一律不返回）",
        f"{sorted(outputs)}",
    )
    raw = run.get("llm_raw")
    c.check(
        isinstance(raw, str)
        and len(raw) > 0
        and len(raw) <= weekly_service.LLM_RAW_MAX_LEN
        and "sk-" not in raw,
        "llm_raw 回填且经脱敏截断（非空、≤ 20000、无密钥前缀）",
        f"len={len(raw) if isinstance(raw, str) else raw}",
    )
    c.check(
        run.get("json_parse_attempts") == 1,
        "run.json_parse_attempts = 1",
        f"{run.get('json_parse_attempts')}",
    )
    c.check(
        int(run.get("total_tokens") or 0) == int(run.get("prompt_tokens") or 0)
        + int(run.get("completion_tokens") or 0)
        and int(run.get("total_tokens") or 0) > 0,
        "token 用量落库且自洽（prompt + completion = total > 0）",
        f"{run.get('prompt_tokens')} + {run.get('completion_tokens')} = {run.get('total_tokens')}",
    )
    c.check(
        _count_runs() == before_runs + 1,
        "成功路径恰新增 1 条 agent_runs",
        f"{before_runs} → {_count_runs()}",
    )

    # --- 列表 / 详情 ---
    resp = client.get("/api/v1/reports/weekly", params={"limit": 5})
    c.check(resp.status_code == 200, "GET /reports/weekly 200", f"{resp.status_code}")
    listing = resp.json().get("items") or []
    ids = [str(item.get("run_id")) for item in listing]
    c.check(
        run_id in ids,
        "周报历史列表含本次 run",
        f"{ids}",
    )
    created = [item.get("created_at") or "" for item in listing]
    c.check(
        created == sorted(created, reverse=True),
        "周报历史按 created_at 倒序",
        f"{created}",
    )
    # summary 是 LLM 自由文本：只断言非空 + 建议数结构，不硬编码关键词
    # （硬编码会造成脆弱测试，模型换一种措辞就误报）。
    first = next((item for item in listing if str(item.get("run_id")) == run_id), {})
    c.check(
        bool(first.get("summary")) and first.get("next_week_suggestions_count") == 3,
        "列表项含截断 summary 与建议计数",
        f"{first.get('summary', '')[:20]}…",
    )
    c.check(
        "llm_raw" not in resp.text,
        "列表响应不含 llm_raw",
    )

    resp = client.get(f"/api/v1/reports/weekly/{run_id}")
    c.check(resp.status_code == 200, "GET /reports/weekly/{run_id} 200（详情）", f"{resp.status_code}")
    detail = resp.json()
    c.check(
        (detail.get("report") or {}).get("summary") == GOOD_WEEKLY_JSON["summary"]
        and detail.get("parse_attempts") == 1
        and bool(detail.get("llm_model"))
        and int(detail.get("total_tokens") or 0) > 0,
        "详情回填 report / parse_attempts / llm_model / total_tokens",
        f"model={detail.get('llm_model')}",
    )
    c.check(
        "llm_raw" not in resp.text and "Traceback" not in resp.text,
        "详情响应不含 llm_raw / Traceback",
    )
    c.check(
        client.get(f"/api/v1/reports/weekly/{uuid.uuid4()}").status_code == 404,
        "不存在的 run_id → 404",
    )
    c.check(
        client.get("/api/v1/reports/weekly/not-a-uuid").status_code == 422,
        "非法 UUID → 422",
    )

    # --- 只读断言（看板 GET × N + 周报 POST 全程未动业务表）---
    after_tables = _table_counts()
    c.check(
        after_tables == baseline_tables,
        "看板与周报全程只读：八张业务表计数与运行前完全一致",
        f"{baseline_tables} → {after_tables}",
    )


# ---------------------------------------------------------------------------
# 组 4：失败路径
# ---------------------------------------------------------------------------
def group4_failures(c: Checker, client: TestClient, fx: Fixture) -> None:
    # --- 4.1 全坏 JSON 超限 → 400，且调用次数 = 回路上界 ---
    before_runs = _count_runs()
    expected_calls = settings.AGENT_JSON_MAX_ATTEMPTS * (
        1 + settings.AGENT_NODE_MAX_RETRIES
    )
    model = WeeklyScriptModel(responses=[BAD_JSON_TEXT])
    with _ScriptedModel(model):
        resp = _post_generate(client, {"days": 7})
    c.check(
        resp.status_code == 400,
        "全坏 JSON：POST → 400（结构校验连续超限）",
        f"{resp.status_code}",
    )
    c.check(
        model.calls == expected_calls,
        f"模型调用次数 = 回路上界 {settings.AGENT_JSON_MAX_ATTEMPTS}×（1+{settings.AGENT_NODE_MAX_RETRIES}）"
        f"={expected_calls}",
        f"calls={model.calls}",
    )
    body = resp.json()
    c.check(
        "Traceback" not in resp.text and body.get("message"),
        "失败响应为统一错误体且无 Traceback",
        f"{str(body.get('message'))[:60]}…",
    )
    c.check("sk-" not in resp.text, "失败响应无密钥字样")
    c.check(
        _count_runs() == before_runs + 1,
        "失败路径恰新增 1 条 agent_runs（记 failed）",
        f"{before_runs} → {_count_runs()}",
    )
    with SessionLocal() as db:
        failed = db.execute(
            text(
                "SELECT id, status, error_message FROM agent_runs "
                "WHERE run_type = 'weekly_report' AND status = 'failed' "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            )
        ).one_or_none()
    c.check(
        failed is not None and bool(failed[2]) and "Traceback" not in str(failed[2]),
        "失败 run 落库：status=failed 且 error_message 非空、无 Traceback",
        f"{str(failed[2])[:60] if failed else '未找到'}" if failed else "未找到失败 run",
    )

    # --- 4.2 LLM 未配置 → 400 且零请求零 run ---
    before_runs = _count_runs()
    with _UnconfiguredModel():
        resp = _post_generate(client, {"days": 7})
    c.check(
        resp.status_code == 400,
        "LLM 未配置：POST → 400（零请求）",
        f"{resp.status_code}",
    )
    c.check(
        _count_runs() == before_runs,
        "LLM 未配置：零 agent_runs 产生（模型工厂抛 AgentConfigError，未进 run_agent）",
        f"{before_runs} → {_count_runs()}",
    )
    c.check(
        "Traceback" not in resp.text and "sk-" not in resp.text,
        "未配置错误响应无 Traceback / 密钥字样",
    )

    # --- 4.3 周期非法 → 422 且零 run ---
    before_runs = _count_runs()
    cases = [
        ("days=0（越界）", {"days": 0}),
        ("days=999（越界）", {"days": 999}),
        ("days 与显式起止互斥", {"days": 3, "period_start": "2026-10-01"}),
        ("起点晚于终点", {"period_start": "2026-06-01", "period_end": "2026-05-01"}),
        ("非法日期串", {"period_start": "not-a-date"}),
    ]
    statuses = []
    for _label, payload in cases:
        resp = _post_generate(client, payload)
        statuses.append(resp.status_code)
    c.check(
        statuses == [422, 422, 422, 422, 422],
        "五类非法周期参数 → 全部 422（服务端校验）",
        f"{statuses}",
    )
    c.check(
        _count_runs() == before_runs,
        "周期非法：零 agent_runs 产生（先校验后跑图）",
        f"{before_runs} → {_count_runs()}",
    )


# ---------------------------------------------------------------------------
# 组 5：接口面
# ---------------------------------------------------------------------------
def group5_surface(c: Checker) -> None:
    paths = app.openapi()["paths"]
    c.check(
        sorted(paths.get("/api/v1/dashboard/summary", {})) == ["get"],
        "/api/v1/dashboard/summary 恰为 GET",
        f"{sorted(paths.get('/api/v1/dashboard/summary', {}))}",
    )
    c.check(
        sorted(paths.get("/api/v1/dashboard/trend", {})) == ["get"],
        "/api/v1/dashboard/trend 恰为 GET",
        f"{sorted(paths.get('/api/v1/dashboard/trend', {}))}",
    )
    c.check(
        sorted(paths.get("/api/v1/dashboard/ranking", {})) == ["get"],
        "/api/v1/dashboard/ranking 恰为 GET",
        f"{sorted(paths.get('/api/v1/dashboard/ranking', {}))}",
    )
    c.check(
        sorted(paths.get("/api/v1/reports/weekly/generate", {})) == ["post"],
        "/api/v1/reports/weekly/generate 恰为 POST（静态路径先于动态段注册）",
        f"{sorted(paths.get('/api/v1/reports/weekly/generate', {}))}",
    )
    c.check(
        sorted(paths.get("/api/v1/reports/weekly", {})) == ["get"],
        "/api/v1/reports/weekly 恰为 GET（历史）",
        f"{sorted(paths.get('/api/v1/reports/weekly', {}))}",
    )
    c.check(
        sorted(paths.get("/api/v1/reports/weekly/{run_id}", {})) == ["get"],
        "/api/v1/reports/weekly/{run_id} 恰为 GET（详情）",
        f"{sorted(paths.get('/api/v1/reports/weekly/{run_id}', {}))}",
    )
    bad = [
        path
        for path in paths
        if ("/dashboard" in path or "/reports" in path)
        and any(method in paths[path] for method in ("put", "patch", "delete"))
    ]
    c.check(not bad, "看板 / 周报路由不存在 PUT / PATCH / DELETE（看板只读、周报只增）", f"{bad}")

    schemas = app.openapi()["components"]["schemas"]
    forbidden = {"llm_raw", "api_key", "secret", "password", "cookie", "authorization"}
    checked: list[str] = []
    for name in (
        "DashboardSummaryOut",
        "DashboardTrendOut",
        "DashboardRankingOut",
        "WeeklyReportGenerateRequest",
        "WeeklyReportSections",
        "WeeklyReportOut",
        "WeeklyReportDetailOut",
        "WeeklyReportListItem",
        "WeeklyReportListOut",
    ):
        props = set((schemas.get(name) or {}).get("properties") or {})
        checked.append(name)
        hits = [flag for flag in forbidden if any(flag in prop.lower() for prop in props)]
        c.check(
            not hits,
            f"{name} 无敏感字段（llm_raw / key / 凭据）",
            f"hits={hits} props={sorted(props)}",
        )
    c.check(
        len(checked) == 9,
        "九个第 17 步 schema 全部完成敏感字段扫描",
        f"{checked}",
    )
    request_props = set(
        (schemas.get("WeeklyReportGenerateRequest") or {}).get("properties") or {}
    )
    c.check(
        request_props == {"period_start", "period_end", "days"},
        "WeeklyReportGenerateRequest 属性恰为周期三参数",
        f"{sorted(request_props)}",
    )

    main_src = (BACKEND_DIR / "app" / "main.py").read_text(encoding="utf-8")
    c.check(
        main_src.count("include_router(") >= 11
        and "dashboard_router" in main_src
        and "reports_router" in main_src,
        "main.py include_router ≥ 11 且 dashboard / reports 在册（第 10、11 只，不回退）",
        f"{main_src.count('include_router(')}",
    )
    c.check(
        main_src.count("lifespan=") == 1 and main_src.count("async def lifespan") == 1,
        "全局 lifespan 仍唯一（周报定时合并进同一 lifespan，未新增）",
        f"lifespan={main_src.count('lifespan=')} / def={main_src.count('async def lifespan')}",
    )
    c.check(
        "start_weekly_scheduler" in main_src and "shutdown_weekly_scheduler" in main_src,
        "周报定时器已接入唯一 lifespan（启动 / 关闭成对）",
    )

    # --- 周报定时默认关闭（字段默认值 + 实测）---
    default_value = Settings.model_fields["WEEKLY_SCHEDULER_ENABLED"].default
    c.check(
        default_value is False,
        "WEEKLY_SCHEDULER_ENABLED 字段默认 False（周报定时默认关闭）",
        f"{default_value}",
    )
    if not settings.WEEKLY_SCHEDULER_ENABLED:
        started = start_weekly_scheduler()
        c.check(
            started is None and get_weekly_scheduler() is None,
            "本机配置下 start_weekly_scheduler() 返回 None 且不注册作业（零调度）",
            f"{started}",
        )
    else:
        print("[SKIP] 本机已开启 WEEKLY_SCHEDULER_ENABLED，跳过「默认关闭」行为断言")


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

    # 全前端域名恰 1 处（第 14 步发布外链；本步未新增）
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
        total == 1
        and holders
        == ["frontend/src/app/notes/[id]/note-studio-client.tsx"],
        "全前端平台域名仍恰 1 处（第 14 步发布外链；本步未新增）",
        f"total={total} holders={holders}",
    )
    c.check(
        len(ts_files) > 40,
        "前端扫描覆盖全部 ts / tsx（非抽样）",
        f"files={len(ts_files)}",
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    c = Checker()
    print("=" * 78)
    print("第 17 步 · 运营看板 + 周报 Agent 离线自检（不联网、不需要 API Key）")
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
        group1b_period_facts(c, fx)
        group2_dashboard_api(c, client, fx)
        group3_weekly_success(c, client, fx)
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
    print("全部 PASS ✔（第 17 步运营看板 + 周报自检，依赖本地 docker PostgreSQL）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
