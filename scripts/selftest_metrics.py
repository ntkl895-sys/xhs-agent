"""第 15 步 · 数据回填（截图识别 + 手工录入）后端自检
（**依赖本地 docker PostgreSQL；不联网、不需要真实 API Key**）。

覆盖任务书要求的全部断言，分 9 组：

0. 图结构：识别图是真 LangGraph（四节点 + 回炉条件边 + 有限回路）；
   ``normalize_payload`` 纯函数的别名 / 单位换算 / 占位符 / 冲突处理路径；
1. 手工录入：POST 逐列落库 + 时间线回读（倒序）+ 可空非负整数校验（422）+
   ``captured_at`` 必填与无时区按 UTC+8 解释 + note 404 + 同时刻 409 +
   客户端伪传 ``screenshot_path`` 被忽略（落库恒为 null，除非服务端受控写入）；
2. 识别成功路径（假模型）：完整图返回六指标候选且 ``note_metrics`` **行数不变**；
   额外字段（impressions / clicks / ctr）被忽略；missing / uncertain 正确登记；
   坏 JSON 有限重试后成功（``parse_attempts`` 递增）；不传 ``note_id`` 的纯识别；
3. 识别失败路径：全坏 JSON 重试超限 → 400（轮数 = ``AGENT_JSON_MAX_ATTEMPTS``）；
   模型不支持图片 → 400（附视觉文档，无原始堆栈）；视觉未配置 → 400（不发请求、
   不建 run）；``note_id`` 不存在 → 404；
4. 图片校验：声明类型不在白名单 / 文件头魔数不符 / 空文件 / 超大小上限 → 422；
5. 截图留档：默认（SAVE_SCREENSHOT=false）**绝不落盘**；显式开启且用户确认时
   落盘 + ``evidence.screenshot_file`` 回显 + 经 POST /metrics 受控写入
   ``screenshot_path``（含路径注入拒绝）；
6. 接口面：``/api/v1/metrics`` 恰为 GET + POST、``/recognize`` 恰为 POST、
   无 PUT / DELETE；请求模型**不含** ``screenshot_path``；响应模型无
   ``llm_raw`` / 敏感字段；``main.py`` 路由注册数不回退（≥8）；
7. 零迁移：``migrations/versions`` 仍恰 2 支、``alembic current`` = ``0002 (head)``、
   ``alembic check`` 退出码 0；
8. 合规扫描：本步后端 / 前端文件不含小红书域名 / 登录态凭据 / 密钥前缀；
   识别响应与 ``agent_runs`` 不含图片 base64 / 绝对路径 / key；
9. 清理复查：测试数据（前缀 ``[SELFTEST]数据回填``）按 id 删 + 前缀兜底扫，
   双口径残留 0；检查点线程与 agent_runs 按**运行前后差集**安全清理。

HTTP 断言走**进程内** ``TestClient``（不需要 uvicorn）；数据复核走 SQLAlchemy 会话；
假模型经 monkeypatch 注入 ``app.agent.metrics.service.get_chat_model``（**不发真实请求**）。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\selftest_metrics.py

全部通过退出码 0；任一项 FAIL 退出码 1。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import timezone as dt_timezone
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

import app.agent.metrics.service as metrics_service  # noqa: E402
from app.agent.checkpoint import close_checkpointer, init_checkpointer  # noqa: E402
from app.agent.metrics.graph import (  # noqa: E402
    NODE_GIVE_UP,
    NODE_LOAD_IMAGE,
    NODE_NORMALIZE,
    NODE_TRANSCRIBE,
    build_metric_graph,
    normalize_payload,
)
from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.models.enums import (  # noqa: E402
    AccountStatus,
    ComplianceStatus,
    NoteStatus,
    PersonaStatus,
    TopicStatus,
)
from app.models.note import Note  # noqa: E402
from app.models.persona import Persona  # noqa: E402
from app.models.topic import Topic  # noqa: E402

#: 本脚本全部测试数据统一前缀（清理与复查的双口径都靠它兜底）
PREFIX = "[SELFTEST]数据回填"

VERSIONS_DIR = BACKEND_DIR / "migrations" / "versions"

#: 本步触碰过的后端文件（合规扫描对象）
SCANNED_FILES = (
    BACKEND_DIR / "app" / "agent" / "metrics" / "__init__.py",
    BACKEND_DIR / "app" / "agent" / "metrics" / "contracts.py",
    BACKEND_DIR / "app" / "agent" / "metrics" / "state.py",
    BACKEND_DIR / "app" / "agent" / "metrics" / "prompts.py",
    BACKEND_DIR / "app" / "agent" / "metrics" / "graph.py",
    BACKEND_DIR / "app" / "agent" / "metrics" / "service.py",
    BACKEND_DIR / "app" / "schemas" / "metric.py",
    BACKEND_DIR / "app" / "crud" / "metric.py",
    BACKEND_DIR / "app" / "api" / "routes" / "metrics.py",
    BACKEND_DIR / "app" / "agent" / "llm" / "factory.py",
    BACKEND_DIR / "app" / "agent" / "llm" / "__init__.py",
    BACKEND_DIR / "app" / "models" / "enums.py",
    BACKEND_DIR / "app" / "main.py",
    BACKEND_DIR / "app" / "core" / "config.py",
)

#: 本步触碰过的前端文件（合规扫描对象）。
#: 注意：`notes/[id]/note-studio-client.tsx` **不在此清单** —— 该文件含第 14 步既有
#: 的「前往发布页」外链常量（含平台域名）与隐私说明文字，已由 ``selftest_studio.py``
#: 的专用断言背书（全前端恰 1 处且位于笔记相关文件）；本步对其改动仅为新增
#: 「数据回填 →」跳转链接，不引入任何新的禁词。
FRONTEND_SCANNED = (
    FRONTEND_SRC / "app" / "metrics" / "page.tsx",
    FRONTEND_SRC / "app" / "api" / "v1" / "metrics" / "route.ts",
    FRONTEND_SRC / "app" / "api" / "v1" / "metrics" / "recognize" / "route.ts",
    FRONTEND_SRC / "lib" / "api-client.ts",
    FRONTEND_SRC / "lib" / "api-server.ts",
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

#: 六个指标（与后端 METRIC_FIELDS 一致，测试侧不复用后端常量以防「同源错误」）
METRIC_FIELDS = ("views", "likes", "collects", "comments", "shares", "new_followers")

#: 合法 PNG 文件头 + 填充（内容不解析，只过魔数校验）
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"X" * 300

#: 完整识别结果（一次命中；六指标齐全、raw 与 value 一致）
GOOD_JSON: dict[str, Any] = {
    "metrics": {
        "views": {"raw": "1.2万", "value": 12000, "confidence": 0.95},
        "likes": {"raw": "3456", "value": 3456, "confidence": 0.97},
        "collects": {"raw": "890", "value": 890, "confidence": 0.9},
        "comments": {"raw": "123", "value": 123, "confidence": 0.9},
        "shares": {"raw": "45", "value": 45, "confidence": 0.9},
        "new_followers": {"raw": "67", "value": 67, "confidence": 0.9},
    },
    "missing_fields": [],
    "uncertain_fields": [],
    "overall_confidence": 0.93,
    "warnings": [],
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
# 假模型（照 selftest_content_agent 的确定性脚本模型：dict 自动转 JSON / 异常抛出）
# ---------------------------------------------------------------------------
class MetricScriptModel(BaseChatModel):
    """确定性假模型：按调用次序取响应；末项会被重复使用（模拟模型固执己见）。"""

    responses: list[Any] = Field(default_factory=list)
    calls: int = 0
    prompts: list[str] = Field(default_factory=list)
    usage_in: int = 21
    usage_out: int = 34

    @property
    def _llm_type(self) -> str:
        return "metric-script"

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
        item = sequence[index] if sequence else '{"metrics": {}}'
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
    """上下文管理器：把识别 service 的模型工厂换成假模型，退出时恢复。"""

    def __init__(self, model: MetricScriptModel) -> None:
        self.model = model
        self._original: Any = None

    def __enter__(self) -> MetricScriptModel:
        self._original = metrics_service.get_chat_model
        metrics_service.get_chat_model = lambda **kwargs: self.model  # noqa: ARG005
        return self.model

    def __exit__(self, *exc_info: Any) -> None:
        metrics_service.get_chat_model = self._original


# ---------------------------------------------------------------------------
# 夹具与清理
# ---------------------------------------------------------------------------
class Fixture:
    def __init__(self) -> None:
        self.account_id: uuid.UUID | None = None
        self.persona_id: uuid.UUID | None = None
        self.topic_id: uuid.UUID | None = None
        self.note_id: uuid.UUID | None = None
        self.before_threads: set[str] = set()
        self.before_run_ids: set[str] = set()
        self.created_screenshot_files: list[str] = []
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
                body="已发布正文",
                tags=["自检"],
                status=NoteStatus.PUBLISHED,
                compliance_status=ComplianceStatus.PASSED,
            )
            db.add(note)
            db.flush()
            self.account_id = account.id
            self.persona_id = persona.id
            self.topic_id = topic.id
            self.note_id = note.id
            db.commit()

    # ------------------------------------------------------------- 清理
    def cleanup(self) -> None:
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
        # 2) 本次自检新建的 agent_runs（差集；覆盖带/不带 note_id 的运行）
        new_runs = _run_ids_now() - self.before_run_ids
        if new_runs:
            with SessionLocal() as db:
                db.execute(
                    text("DELETE FROM agent_runs WHERE id = ANY(:rids)"),
                    {"rids": list(new_runs)},
                )
                db.commit()
        # 3) 截图留档文件（自检产物，逐个删除）
        directory = settings.screenshot_dir_path
        for name in self.created_screenshot_files:
            try:
                (directory / name).unlink(missing_ok=True)
            except OSError:
                pass
        # 4) 业务数据：note_metrics → notes → topics → personas → accounts
        like = f"%{PREFIX}%"
        with SessionLocal() as db:
            if self.note_id is not None:
                db.execute(
                    text("DELETE FROM note_metrics WHERE note_id = :n"),
                    {"n": str(self.note_id)},
                )
            db.execute(
                text(
                    "DELETE FROM note_metrics WHERE note_id IN "
                    "(SELECT id FROM notes WHERE title LIKE :t)"
                ),
                {"t": like},
            )
            if self.note_id is not None:
                db.execute(text("DELETE FROM notes WHERE id = :n"), {"n": str(self.note_id)})
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
        with SessionLocal() as db:
            return {
                "note_metrics(by_note_id)": db.execute(
                    text("SELECT count(*) FROM note_metrics WHERE note_id = :n"),
                    {"n": str(self.note_id)},
                ).scalar_one(),
                "note_metrics(prefix)": db.execute(
                    text(
                        "SELECT count(*) FROM note_metrics WHERE note_id IN "
                        "(SELECT id FROM notes WHERE title LIKE :t)"
                    ),
                    {"t": like},
                ).scalar_one(),
                "notes(by_id)": db.execute(
                    text("SELECT count(*) FROM notes WHERE id = :n"),
                    {"n": str(self.note_id)},
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


def _fetch_metric(metric_id: str) -> dict[str, Any]:
    cols = (
        "id, note_id, captured_at, data_source, views, likes, collects, comments, "
        "shares, new_followers, evidence, screenshot_path"
    )
    with SessionLocal() as db:
        row = db.execute(
            text(f"SELECT {cols} FROM note_metrics WHERE id = :m"),  # noqa: S608
            {"m": str(metric_id)},
        ).one_or_none()
    if row is None:
        return {}
    keys = [item.strip() for item in cols.split(",")]
    return dict(zip(keys, row, strict=True))


def _fetch_run(run_id: str) -> dict[str, Any]:
    cols = "id, run_type, trigger, status, outputs, llm_raw, error_message, note_id, json_parse_attempts"
    with SessionLocal() as db:
        row = db.execute(
            text(f"SELECT {cols} FROM agent_runs WHERE id = :r"),  # noqa: S608
            {"r": str(run_id)},
        ).one_or_none()
    if row is None:
        return {}
    keys = [item.strip() for item in cols.split(",")]
    return dict(zip(keys, row, strict=True))


def _count_metrics(note_id: uuid.UUID) -> int:
    with SessionLocal() as db:
        return int(
            db.execute(
                text("SELECT count(*) FROM note_metrics WHERE note_id = :n"),
                {"n": str(note_id)},
            ).scalar_one()
        )


def _count_runs() -> int:
    with SessionLocal() as db:
        return int(db.execute(text("SELECT count(*) FROM agent_runs")).scalar_one())


def _recognize(
    client: TestClient,
    *,
    content: bytes = PNG_BYTES,
    mime: str = "image/png",
    name: str = "panel.png",
    note_id: uuid.UUID | str | None = None,
    save_screenshot: bool | None = None,
):
    files = {"file": (name, content, mime)}
    data: dict[str, str] = {}
    if note_id is not None:
        data["note_id"] = str(note_id)
    if save_screenshot is not None:
        data["save_screenshot"] = "true" if save_screenshot else "false"
    return client.post("/api/v1/metrics/recognize", files=files, data=data)


def _post_snapshot(client: TestClient, payload: dict[str, Any]):
    return client.post("/api/v1/metrics", json=payload)


# ---------------------------------------------------------------------------
# 组 0：图结构与归一纯函数
# ---------------------------------------------------------------------------
def group0_graph(c: Checker) -> None:
    graph = build_metric_graph(
        model=MetricScriptModel(responses=["{}"]),
        image_bytes=PNG_BYTES,
        image_mime="image/png",
        use_checkpointer=False,
    )
    c.check(
        "Compiled" in type(graph).__name__,
        "build_metric_graph 返回已编译的 LangGraph 图",
        f"{type(graph).__name__}",
    )
    view = graph.get_graph()
    nodes = set(view.nodes.keys())
    c.check(
        {NODE_LOAD_IMAGE, NODE_TRANSCRIBE, NODE_NORMALIZE, NODE_GIVE_UP}.issubset(nodes),
        "图含 load_image / transcribe / normalize_validate / give_up 四节点",
        f"实际={sorted(nodes)}",
    )
    edges = {(edge.source, edge.target, bool(edge.conditional)) for edge in view.edges}
    c.check(
        (NODE_LOAD_IMAGE, NODE_TRANSCRIBE, False) in edges,
        "load_image → transcribe 为直连边",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_TRANSCRIBE, NODE_TRANSCRIBE, True) in edges,
        "transcribe 存在「JSON 失败回炉」条件边（有限回路的载体）",
    )
    c.check(
        (NODE_TRANSCRIBE, NODE_NORMALIZE, True) in edges
        and (NODE_TRANSCRIBE, NODE_GIVE_UP, True) in edges,
        "transcribe 存在去 normalize / give_up 的条件边",
    )
    c.check(
        (NODE_NORMALIZE, "__end__", False) in edges and (NODE_GIVE_UP, "__end__", False) in edges,
        "normalize / give_up 均连接 END",
    )
    # 保留有界性断言（>=1）而非固定值：AGENT_JSON_MAX_ATTEMPTS 是部署可调配置，
    # 固定成 3 会把测试与某一份 .env 绑死；这里只验证「回路确实有上限」。
    c.check(
        settings.AGENT_JSON_MAX_ATTEMPTS >= 1,
        "回路有界（上限来自 AGENT_JSON_MAX_ATTEMPTS）",
        f"{settings.AGENT_JSON_MAX_ATTEMPTS}",
    )

    # ---- normalize 纯函数：别名 / 单位 / 占位符 / 冲突 ----
    r = normalize_payload(
        {
            "metrics": {
                "浏览": {"raw": "1.2万"},
                "like": {"raw": "3.4k"},
                "collects": {"raw": "1,234"},
                "评论": {"raw": "1，234"},
                "shares": {"raw": "--"},
                "new_followers": {"raw": "1.2亿"},
                "impressions": 999,
            },
            "missing_fields": [],
            "overall_confidence": 0.9,
        }
    )
    c.check(
        r["metrics"]
        == {
            "views": 12000,
            "likes": 3400,
            "collects": 1234,
            "comments": 1234,
            "shares": None,
            "new_followers": 120000000,
        },
        "别名对齐 + 万/k/亿/千分位换算逐字段正确",
        f"{r['metrics']}",
    )
    c.check(r["missing_fields"] == ["shares"], "占位符（--）计入 missing", f"{r['missing_fields']}")
    c.check(
        r["evidence"]["ignored_fields"] == ["impressions"],
        "六字段之外的键记入 evidence.ignored_fields 并忽略",
        f"{r['evidence']['ignored_fields']}",
    )

    r2 = normalize_payload(
        {
            "metrics": {
                "views": {"raw": "1.2万", "value": 9999},
                "likes": {"value": -5},
                "comments": {"raw": "3.2%"},
            }
        }
    )
    c.check(r2["metrics"]["views"] == 12000, "raw 与 value 冲突时以 raw 换算为准")
    c.check(r2["uncertain_fields"] == ["views"], "冲突字段计入 uncertain", f"{r2['uncertain_fields']}")
    c.check(r2["metrics"]["likes"] is None, "负数 value 按无效处理（不臆造）")
    c.check(r2["metrics"]["comments"] is None, "含 % 的文本按无效处理（六指标均为计数）")


# ---------------------------------------------------------------------------
# 组 1：手工录入（POST /metrics 逐列落库 + 边界）
# ---------------------------------------------------------------------------
def group1_manual(c: Checker, client: TestClient, fx: Fixture) -> None:
    assert fx.note_id is not None

    # 1.1 完整六指标落库
    payload = {
        "note_id": str(fx.note_id),
        "captured_at": "2026-09-01T10:00:00+08:00",
        "data_source": "manual",
        "views": 12000,
        "likes": 3456,
        "collects": 890,
        "comments": 123,
        "shares": 45,
        "new_followers": 67,
    }
    r = _post_snapshot(client, payload)
    c.check(
        r.status_code == 201,
        "手工录入 POST /metrics 返回 201",
        f"{r.status_code} {r.text[:200]}",
    )
    metric_id = r.json().get("id", "")
    row = _fetch_metric(metric_id)
    c.check(
        all(row.get(key) == payload[key] for key in METRIC_FIELDS)
        and row.get("data_source") == "manual"
        and row.get("note_id") == fx.note_id,
        "六指标 + 来源 + 笔记 id 逐列落库一致",
        f"{ {k: row.get(k) for k in METRIC_FIELDS} }/{row.get('data_source')}",
    )
    c.check(row.get("screenshot_path") is None, "默认 screenshot_path 为 null（不落盘）")
    c.check(
        r.json().get("screenshot_path") is None and "llm_raw" not in r.text,
        "响应不含 llm_raw / 截图路径",
    )

    # 1.2 全空六指标可落库（缺失留空，绝不臆造）
    r = _post_snapshot(
        client,
        {
            "note_id": str(fx.note_id),
            "captured_at": "2026-09-02T10:00:00+08:00",
        },
    )
    c.check(r.status_code == 201, "六指标全空也可落库（可空非负整数）", f"{r.status_code}")
    row2 = _fetch_metric(r.json().get("id", ""))
    c.check(
        all(row2.get(key) is None for key in METRIC_FIELDS)
        and row2.get("data_source") == "manual",
        "全空六指标落库为 NULL；data_source 默认 manual",
    )

    # 1.3 无时区输入按 UTC+8 解释（落库后为 02:00Z）
    r = _post_snapshot(
        client,
        {"note_id": str(fx.note_id), "captured_at": "2026-09-04T10:00", "views": 5},
    )
    c.check(r.status_code == 201, "captured_at 无时区也接受（按 UTC+8 解释）", f"{r.status_code}")
    row3 = _fetch_metric(r.json().get("id", ""))
    captured = row3.get("captured_at")
    captured_utc = (
        captured.astimezone(dt_timezone.utc)
        if captured is not None and captured.utcoffset() is not None
        else None
    )
    c.check(
        captured_utc is not None and captured_utc.hour == 2 and captured_utc.minute == 0,
        "无时区输入落库为 02:00Z（即本地 10:00 +08:00）",
        f"{captured}",
    )

    # 1.4 客户端伪传 screenshot_path 被忽略（extra=ignore）
    r = _post_snapshot(
        client,
        {
            "note_id": str(fx.note_id),
            "captured_at": "2026-09-03T10:00:00+08:00",
            "screenshot_path": "/etc/passwd",
            "foo": 1,
        },
    )
    c.check(r.status_code == 201, "多余字段不报错（extra=ignore）", f"{r.status_code}")
    row4 = _fetch_metric(r.json().get("id", ""))
    c.check(
        row4.get("screenshot_path") is None,
        "客户端伪传 screenshot_path 被忽略（落库仍为 null）",
        f"{row4.get('screenshot_path')!r}",
    )

    # 1.5 422：必填缺失 / 非负整数校验
    bad_cases: list[tuple[dict[str, Any], str]] = [
        ({"captured_at": "2026-09-06T10:00:00+08:00"}, "缺 note_id → 422"),
        ({"note_id": str(fx.note_id)}, "缺 captured_at → 422"),
        (
            {"note_id": str(fx.note_id), "captured_at": "2026-09-06T10:00:00+08:00", "views": -1},
            "views 负数 → 422",
        ),
        (
            {"note_id": str(fx.note_id), "captured_at": "2026-09-06T10:00:00+08:00", "views": "abc"},
            "views 非数字 → 422",
        ),
        (
            {
                "note_id": str(fx.note_id),
                "captured_at": "2026-09-06T10:00:00+08:00",
                "views": 3.5,
            },
            "views 非整数 → 422",
        ),
        (
            {"note_id": "not-a-uuid", "captured_at": "2026-09-06T10:00:00+08:00"},
            "note_id 非法 UUID → 422",
        ),
    ]
    for body, label in bad_cases:
        r = _post_snapshot(client, body)
        c.check(r.status_code == 422 and r.json().get("code") == "validation", label, f"{r.status_code}")

    # 1.6 404：笔记不存在
    r = _post_snapshot(
        client,
        {"note_id": str(uuid.uuid4()), "captured_at": "2026-09-07T10:00:00+08:00"},
    )
    c.check(
        r.status_code == 404 and r.json().get("code") == "not_found",
        "不存在的笔记 → 404",
        f"{r.status_code}",
    )

    # 1.7 409：同笔记同 captured_at 唯一
    r = _post_snapshot(client, payload)  # 与 1.1 完全相同（同 note + 同 captured_at）
    c.check(
        r.status_code == 409 and r.json().get("code") == "conflict",
        "同笔记同采集时刻 → 409（uq_metric_snapshot）",
        f"{r.status_code} {r.text[:200]}",
    )
    c.check("captured_at" in json.dumps(r.json(), ensure_ascii=False), "409 提示指向 captured_at")

    # 1.8 时间线：倒序 + 统一错误体
    r = client.get(f"/api/v1/metrics?note_id={fx.note_id}")
    c.check(r.status_code == 200, "GET /metrics?note_id= 返回 200", f"{r.status_code}")
    items = r.json().get("items", [])
    c.check(len(items) == 4, "时间线条目数 = 已落库快照数（4 条）", f"{len(items)}")
    times = [item["captured_at"] for item in items]
    c.check(times == sorted(times, reverse=True), "时间线按 captured_at 倒序", f"{times}")
    c.check(
        all(set(item.keys()) >= {"id", "note_id", "data_source"} for item in items),
        "时间线条目含 id / note_id / data_source",
    )
    r_missing = client.get("/api/v1/metrics")
    c.check(
        r_missing.status_code == 400 and r_missing.json().get("code") == "bad_request",
        "GET /metrics 缺 note_id → 400",
        f"{r_missing.status_code}",
    )
    r_404 = client.get(f"/api/v1/metrics?note_id={uuid.uuid4()}")
    c.check(
        r_404.status_code == 404 and r_404.json().get("code") == "not_found",
        "GET /metrics 笔记不存在 → 404",
        f"{r_404.status_code}",
    )


# ---------------------------------------------------------------------------
# 组 2：识别成功路径
# ---------------------------------------------------------------------------
def group2_recognize_success(c: Checker, client: TestClient, fx: Fixture) -> None:
    assert fx.note_id is not None

    # 2.1 完整图返回六指标候选（且绝不写库）
    before_rows = _count_metrics(fx.note_id)
    model = MetricScriptModel(responses=[GOOD_JSON])
    with _ScriptedModel(model):
        r = _recognize(client, note_id=fx.note_id)
    c.check(r.status_code == 200, "识别成功返回 200", f"{r.status_code} {r.text[:200]}")
    data = r.json()
    c.check(
        data.get("metrics")
        == {
            "views": 12000,
            "likes": 3456,
            "collects": 890,
            "comments": 123,
            "shares": 45,
            "new_followers": 67,
        },
        "六指标候选逐字段正确（raw 文本经本地换算）",
        f"{data.get('metrics')}",
    )
    c.check(
        data.get("missing_fields") == [] and data.get("uncertain_fields") == [],
        "missing / uncertain 均为空",
        f"{data.get('missing_fields')}/{data.get('uncertain_fields')}",
    )
    c.check(data.get("parse_attempts") == 1, "parse_attempts = 1（一次命中）", f"{data.get('parse_attempts')}")
    c.check(data.get("data_source") == "screenshot", "data_source 恒为 screenshot")
    c.check(
        data.get("note_id") == str(fx.note_id) and data.get("note_title") == f"{PREFIX}已发布笔记",
        "note_id / note_title 回显（人工核对用）",
        f"{data.get('note_id')}/{data.get('note_title')}",
    )
    c.check(model.calls == 1, "假模型恰调用 1 次（一次命中）", f"{model.calls}")
    c.check(
        _count_metrics(fx.note_id) == before_rows,
        "识别**不写库**：note_metrics 行数不变（人工核对是硬规则）",
    )
    blob = r.text
    c.check(
        "base64" not in blob and "data:image" not in blob and "llm_raw" not in blob,
        "响应不含 base64 / data URL / llm_raw",
    )
    c.check(
        "D:\\\\" not in blob and "/home/" not in blob and "file://" not in blob,
        "响应不含绝对路径 / 文件 URI",
    )
    prompt = model.prompts[0] if model.prompts else ""
    c.check(
        all(field in prompt for field in METRIC_FIELDS) and "null" in prompt,
        "提示词含六字段名与「取不到留 null」约束",
    )

    # 2.2 审计回填：run 记录（outputs 摘要 / llm_raw / note_id）
    run_id = data.get("run_id", "")
    run = _fetch_run(run_id)
    c.check(
        run.get("run_type") == "metric_recognize" and run.get("status") == "success",
        "agent_runs：run_type=metric_recognize、status=success",
        f"{run.get('run_type')}/{run.get('status')}",
    )
    c.check(
        isinstance(run.get("outputs"), dict)
        and (run["outputs"].get("metrics") or {}).get("views") == 12000,
        "outputs 回填识别摘要（metrics.views=12000）",
        f"{run.get('outputs')}",
    )
    c.check(run.get("note_id") == fx.note_id, "run 的 note_id 已回填（harness 不转发，service 补齐）")
    raw_text = str(run.get("llm_raw") or "")
    c.check(
        raw_text and "sk-" not in raw_text and "base64" not in raw_text,
        "llm_raw 已入库且不含密钥前缀 / base64",
    )
    c.check(
        run.get("json_parse_attempts") == 1,
        "json_parse_attempts = 1",
        f"{run.get('json_parse_attempts')}",
    )

    # 2.3 额外字段被忽略 + missing / uncertain 正确登记
    json2 = {
        "metrics": {
            "views": {"raw": "1.2万", "value": 9999},   # 冲突 → raw 优先 + uncertain
            "点赞": {"raw": "3.4千"},                    # 别名 → 3400
            "收藏": {"value": 890},
            "评论": "暂无",                              # 占位 → None
            "分享": "--",                                # 占位 → None
            "impressions": 88888,                        # 六字段之外 → 忽略
            "clicks": 1234,
            "ctr": "3.2%",
        },
        "missing_fields": ["评论"],
        "uncertain_fields": ["分享"],
        "overall_confidence": 0.8,
    }
    with _ScriptedModel(MetricScriptModel(responses=[json2])):
        r = _recognize(client, note_id=fx.note_id)
    c.check(r.status_code == 200, "含额外字段的识别仍成功", f"{r.status_code}")
    data2 = r.json()
    c.check(
        data2["metrics"]["views"] == 12000 and data2["metrics"]["likes"] == 3400,
        "raw/value 冲突取 raw 换算；中文别名正确对齐",
        f"{data2['metrics']}",
    )
    c.check(
        set(data2["metrics"].keys()) == set(METRIC_FIELDS),
        "metrics 恰含六字段（impressions / clicks / ctr 已忽略）",
        f"{sorted(data2['metrics'].keys())}",
    )
    c.check(
        "impressions" not in data2["metrics"] and "clicks" not in data2["metrics"],
        "额外字段不作为指标字段返回",
    )
    c.check(
        data2["missing_fields"] == ["comments", "shares", "new_followers"],
        "missing_fields = 值为空的字段（本地重新计算）",
        f"{data2['missing_fields']}",
    )
    c.check(
        data2["uncertain_fields"] == ["views"],
        "uncertain_fields：raw/value 冲突字段（值为空的不计）",
        f"{data2['uncertain_fields']}",
    )
    c.check(
        data2["evidence"]["ignored_fields"] == ["clicks", "ctr", "impressions"],
        "evidence.ignored_fields 登记被忽略字段",
        f"{data2['evidence']['ignored_fields']}",
    )
    c.check(
        any("忽略" in text for text in data2["warnings"]),
        "warnings 提示已忽略非同口径字段",
        f"{data2['warnings']}",
    )

    # 2.4 坏 JSON 有限重试后成功（invoke_json 内部纠正重问）
    with _ScriptedModel(MetricScriptModel(responses=["完全不是 JSON 的纯文字。", GOOD_JSON])):
        r = _recognize(client, note_id=fx.note_id)
    c.check(r.status_code == 200, "首次坏 JSON 经纠正重问后成功", f"{r.status_code}")
    c.check(
        r.json().get("parse_attempts") == 2,
        "parse_attempts = 2（纠正后命中）",
        f"{r.json().get('parse_attempts')}",
    )

    # 2.5 不传 note_id 的纯识别
    with _ScriptedModel(MetricScriptModel(responses=[GOOD_JSON])):
        r = _recognize(client)  # 不带 note_id
    c.check(r.status_code == 200, "不带 note_id 也能识别（纯候选）", f"{r.status_code}")
    c.check(
        r.json().get("note_id") is None and r.json().get("note_title") is None,
        "note_id / note_title 为 null",
    )


# ---------------------------------------------------------------------------
# 组 3：识别失败路径（友好错误、无原始堆栈）
# ---------------------------------------------------------------------------
def group3_recognize_failures(c: Checker, client: TestClient, fx: Fixture) -> None:
    assert fx.note_id is not None
    attempts_limit = max(1, settings.AGENT_JSON_MAX_ATTEMPTS)

    # 3.1 全坏 JSON：回路重试耗尽 → 400
    model = MetricScriptModel(responses=["这不是 JSON。仍然不是 JSON。"])
    before_runs = _run_ids_now()
    with _ScriptedModel(model):
        r = _recognize(client, note_id=fx.note_id)
    c.check(
        r.status_code == 400 and r.json().get("code") == "bad_request",
        "JSON 连续失败重试超限 → 400",
        f"{r.status_code} {r.text[:200]}",
    )
    c.check("识别失败" in r.json().get("message", ""), "错误消息为友好中文指引")
    c.check("Traceback" not in r.text and "File \"" not in r.text, "响应不含原始堆栈")
    c.check(
        model.calls == attempts_limit * attempts_limit,
        f"模型调用次数 = 图回炉 {attempts_limit} 轮 × 单轮 {attempts_limit} 次 = {attempts_limit * attempts_limit}",
        f"{model.calls}",
    )
    new_runs = _run_ids_now() - before_runs
    failed_rows = [_fetch_run(rid) for rid in new_runs]
    c.check(
        len(new_runs) == 1 and failed_rows[0].get("status") == "failed",
        "失败运行落 agent_runs=status:failed",
        f"{[(row.get('status'), row.get('error_message')) for row in failed_rows]}",
    )
    err_msg = str(failed_rows[0].get("error_message") or "")
    c.check("识别失败" in err_msg and "sk-" not in err_msg, "error_message 为可读摘要且无密钥前缀")

    # 3.2 模型不支持图片 → 400（附视觉文档；不重试）
    model = MetricScriptModel(
        responses=[RuntimeError("this model does not support vision (image input)")]
    )
    before_runs = _run_ids_now()
    with _ScriptedModel(model):
        r = _recognize(client, note_id=fx.note_id)
    c.check(
        r.status_code == 400 and r.json().get("code") == "bad_request",
        "模型不支持图片 → 400（换支持视觉的模型可解）",
        f"{r.status_code} {r.text[:200]}",
    )
    c.check("不支持" in r.json().get("message", ""), "消息包含「不支持」与操作指引")
    c.check(
        "api-docs.deepseek.com" in json.dumps(r.json(), ensure_ascii=False),
        "details 附带视觉文档地址（LLM_VISION_MODEL 配置指引）",
    )
    c.check(model.calls == 1, "不支持图片不进入回炉（首轮即分流 give_up）", f"{model.calls}")
    new_runs = _run_ids_now() - before_runs
    c.check(
        len(new_runs) == 1 and _fetch_run(next(iter(new_runs))).get("status") == "failed",
        "不支持图片的运行同样落 failed（审计不丢）",
    )

    # 3.3 视觉未配置 → 400（构造阶段拒绝，不发请求、不建 run）
    before_runs = _run_ids_now()
    before_runs_count = _count_runs()
    original_enabled = settings.AGENT_LLM_ENABLED
    settings.AGENT_LLM_ENABLED = False
    try:
        r = _recognize(client, note_id=fx.note_id)  # 不注入假模型：走真实工厂闸门
    finally:
        settings.AGENT_LLM_ENABLED = original_enabled
    c.check(
        r.status_code == 400 and r.json().get("code") == "bad_request",
        "视觉未配置（AGENT_LLM_ENABLED=false）→ 400",
        f"{r.status_code} {r.text[:200]}",
    )
    c.check(
        "视觉识别未就绪" in r.json().get("message", "")
        and "AGENT_LLM_ENABLED" in json.dumps(r.json(), ensure_ascii=False),
        "未配置错误信息指向开关 / key（可操作指引）",
    )
    c.check(_count_runs() == before_runs_count, "未配置时**不创建** agent_runs（零请求）")

    # 3.4 note_id 不存在 → 404
    with _ScriptedModel(MetricScriptModel(responses=[GOOD_JSON])):
        r = _recognize(client, note_id=uuid.uuid4())
    c.check(
        r.status_code == 404 and r.json().get("code") == "not_found",
        "识别时 note_id 不存在 → 404",
        f"{r.status_code} {r.text[:200]}",
    )
    c.check("笔记不存在" in r.json().get("message", ""), "404 消息为友好中文")


# ---------------------------------------------------------------------------
# 组 4：图片校验（422）
# ---------------------------------------------------------------------------
def group4_image_validation(c: Checker, client: TestClient, fx: Fixture) -> None:
    assert fx.note_id is not None

    with _ScriptedModel(MetricScriptModel(responses=[GOOD_JSON])):
        r = _recognize(client, mime="text/plain", name="a.txt")
    c.check(r.status_code == 422, "声明类型不在白名单 → 422", f"{r.status_code}")

    with _ScriptedModel(MetricScriptModel(responses=[GOOD_JSON])):
        r = _recognize(client, content=b"GIF89a" + b"0" * 100, mime="image/png")
    c.check(r.status_code == 422, "文件头魔数与声明类型不符 → 422（改名的非图片文件）", f"{r.status_code}")
    c.check("文件头" in r.json().get("message", ""), "错误消息指出文件头校验失败")

    with _ScriptedModel(MetricScriptModel(responses=[GOOD_JSON])):
        r = _recognize(client, content=b"", mime="image/png")
    c.check(r.status_code == 422, "空文件 → 422", f"{r.status_code}")

    oversize = b"\x89PNG\r\n\x1a\n" + b"\x00" * settings.METRIC_MAX_IMAGE_BYTES
    with _ScriptedModel(MetricScriptModel(responses=[GOOD_JSON])):
        r = _recognize(client, content=oversize, mime="image/png")
    c.check(r.status_code == 422, "超过大小上限 → 422", f"{r.status_code}")
    c.check("大小上限" in r.json().get("message", ""), "错误消息指出大小上限与调整方式")

    r = client.post("/api/v1/metrics/recognize", data={"note_id": str(fx.note_id)})
    c.check(r.status_code == 422, "缺 file 字段 → 422（请求校验）", f"{r.status_code}")


# ---------------------------------------------------------------------------
# 组 5：截图留档（默认不落盘；显式开启 + 用户确认才落盘并可受控关联）
# ---------------------------------------------------------------------------
def group5_screenshot_save(c: Checker, client: TestClient, fx: Fixture) -> None:
    assert fx.note_id is not None
    directory = settings.screenshot_dir_path

    def files_now() -> set[str]:
        if not directory.exists():
            return set()
        return {item.name for item in directory.glob("*") if item.is_file()}

    # 5.1 默认关闭：即使表单请求保存也不落盘
    original_save = settings.SAVE_SCREENSHOT
    c.check(original_save is False, "本机 SAVE_SCREENSHOT 默认 false")
    before_files = files_now()
    with _ScriptedModel(MetricScriptModel(responses=[GOOD_JSON])):
        r = _recognize(client, note_id=fx.note_id, save_screenshot=True)
    c.check(
        r.status_code == 200 and r.json().get("screenshot_file") is None,
        "默认关闭时「请求保存」也不落盘（screenshot_file=null）",
        f"{r.status_code}",
    )
    c.check(files_now() == before_files, "SCREENSHOT_DIR 无新文件（差集为空）")
    c.check(
        "screenshot_file" not in (r.json().get("evidence") or {}),
        "默认关闭时 evidence 不含 screenshot_file",
    )

    # 5.2 显式开启 + 用户确认：落盘 + evidence 回显 + 受控写入 screenshot_path
    settings.SAVE_SCREENSHOT = True
    try:
        with _ScriptedModel(MetricScriptModel(responses=[GOOD_JSON])):
            r = _recognize(client, note_id=fx.note_id, save_screenshot=True)
        c.check(r.status_code == 200, "开启后识别仍 200", f"{r.status_code}")
        name = r.json().get("screenshot_file") or ""
        c.check(
            bool(name) and "/" not in name and "\\" not in name,
            "返回的截图文件名是纯文件名（不含目录）",
            f"{name!r}",
        )
        c.check((directory / name).exists(), "截图确实写入 SCREENSHOT_DIR", f"{directory}")
        c.check(
            (r.json().get("evidence") or {}).get("screenshot_file") == name,
            "evidence.screenshot_file 与返回值一致（落库提取通道）",
        )
        if name:
            fx.created_screenshot_files.append(name)

        # 用该 evidence 落库：screenshot_path 由服务端受控写入
        r2 = _post_snapshot(
            client,
            {
                "note_id": str(fx.note_id),
                "captured_at": "2026-09-05T10:00:00+08:00",
                "data_source": "screenshot",
                "views": 12000,
                "evidence": r.json().get("evidence"),
            },
        )
        c.check(r2.status_code == 201, "带 evidence 的快照落库 201", f"{r2.status_code} {r2.text[:200]}")
        row = _fetch_metric(r2.json().get("id", ""))
        c.check(
            row.get("screenshot_path") == name,
            "screenshot_path 经 evidence 受控写入（文件名一致）",
            f"{row.get('screenshot_path')!r}",
        )

        # 回读：时间线只给文件名
        r3 = client.get(f"/api/v1/metrics?note_id={fx.note_id}")
        match = [item for item in r3.json().get("items", []) if item.get("screenshot_path")]
        c.check(
            len(match) == 1 and match[0]["screenshot_path"] == name,
            "时间线回读 screenshot_path（仅文件名）",
            f"{match}",
        )

        # 未确认保存（save_screenshot 不传）→ 不落盘
        before_files = files_now()
        with _ScriptedModel(MetricScriptModel(responses=[GOOD_JSON])):
            r4 = _recognize(client, note_id=fx.note_id)
        c.check(
            r4.json().get("screenshot_file") is None and files_now() == before_files,
            "开启配置但用户未确认时仍不落盘（两把闸门都开才写）",
        )

        # 路径注入拒绝：evidence.screenshot_file 形状不合法 → 落库 null
        r5 = _post_snapshot(
            client,
            {
                "note_id": str(fx.note_id),
                "captured_at": "2026-09-06T10:00:00+08:00",
                "evidence": {"screenshot_file": "../../etc/passwd"},
            },
        )
        row5 = _fetch_metric(r5.json().get("id", ""))
        c.check(
            r5.status_code == 201 and row5.get("screenshot_path") is None,
            "非法文件名（路径注入）被拒绝，screenshot_path 保持 null",
            f"{row5.get('screenshot_path')!r}",
        )
    finally:
        settings.SAVE_SCREENSHOT = original_save


# ---------------------------------------------------------------------------
# 组 6：接口面与 schema
# ---------------------------------------------------------------------------
def group6_surface(c: Checker) -> None:
    paths = app.openapi()["paths"]
    c.check(
        sorted(paths.get("/api/v1/metrics", {})) == ["get", "post"],
        "/api/v1/metrics 恰为 GET + POST",
        f"{sorted(paths.get('/api/v1/metrics', {}))}",
    )
    c.check(
        sorted(paths.get("/api/v1/metrics/recognize", {})) == ["post"],
        "/api/v1/metrics/recognize 恰为 POST（multipart）",
        f"{sorted(paths.get('/api/v1/metrics/recognize', {}))}",
    )
    bad = [
        path
        for path in paths
        if "/metrics" in path and any(method in paths[path] for method in ("put", "delete", "patch"))
    ]
    c.check(not bad, "metrics 路由中不存在 PUT / DELETE / PATCH（本步不提供）", f"{bad}")

    schemas = app.openapi()["components"]["schemas"]
    create_props = set(schemas["MetricSnapshotCreate"]["properties"])
    c.check(
        "screenshot_path" not in create_props,
        "MetricSnapshotCreate **不含** screenshot_path（客户端不可传入）",
        f"{sorted(create_props)}",
    )
    c.check(
        create_props
        == {
            "note_id",
            "captured_at",
            "data_source",
            *METRIC_FIELDS,
            "evidence",
        },
        "MetricSnapshotCreate 属性恰为约定字段",
        f"{sorted(create_props)}",
    )
    recog_props = set(schemas["MetricRecognizeResult"]["properties"])
    sensitive = {"token", "cookie", "secret", "password", "llm_raw", "api_key", "screenshot_path"}
    c.check(
        not any(flag in name.lower() for flag in sensitive for name in recog_props),
        "MetricRecognizeResult 无 llm_raw / 密钥 / 路径字段",
        f"{sorted(recog_props)}",
    )
    out_props = set(schemas["MetricSnapshotOut"]["properties"])
    c.check("llm_raw" not in out_props, "MetricSnapshotOut 不含 llm_raw")

    main_src = (BACKEND_DIR / "app" / "main.py").read_text(encoding="utf-8")
    c.check(
        main_src.count("include_router(") >= 8 and "metrics_router" in main_src,
        "main.py 路由注册数 ≥ 8 且 metrics_router 在册（不回退）",
        f"{main_src.count('include_router(')}",
    )
    c.check(
        main_src.count("lifespan=") == 1 and main_src.count("async def lifespan") == 1,
        "全局 lifespan 仍唯一（第 15 步未新增）",
        f"lifespan={main_src.count('lifespan=')} / def={main_src.count('async def lifespan')}",
    )


# ---------------------------------------------------------------------------
# 组 7：零迁移
# ---------------------------------------------------------------------------
def group7_migrations(c: Checker) -> None:
    files = sorted(p.name for p in VERSIONS_DIR.glob("*.py") if not p.name.startswith("__"))
    c.check(len(files) == 2, "migrations/versions 仍恰 2 支迁移（本步零迁移）", f"{files}")
    c.check(
        all(name.startswith(("0001", "0002")) for name in files),
        "迁移文件名仍为 0001 / 0002（未新增）",
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
# 组 8：合规扫描
# ---------------------------------------------------------------------------
def group8_compliance(c: Checker) -> None:
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


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    c = Checker()
    print("=" * 78)
    print("第 15 步 · 数据回填（截图识别 + 手工录入）离线自检（不联网、不需要 API Key）")
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
        group1_manual(c, client, fx)
        group2_recognize_success(c, client, fx)
        group3_recognize_failures(c, client, fx)
        group4_image_validation(c, client, fx)
        group5_screenshot_save(c, client, fx)
        group6_surface(c)
        group7_migrations(c)
        group8_compliance(c)
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
    print("全部 PASS ✔（第 15 步数据回填自检，依赖本地 docker PostgreSQL）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
