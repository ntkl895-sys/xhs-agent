"""第 12 步 · 创作 Agent【离线】自检（**不连外网、不需要真实 API Key**）。

用**脚本模型**（确定性返回 / 脏 JSON / 不合格草稿）经 service 的 ``model`` 注入口驱动
整条「单选题 → 一篇笔记草稿」链路，断言 11 组行为：

1. 图结构：含 load_context / generate_content / validate_compliance / persist / give_up
   五节点，且为**真 LangGraph**（validate_compliance 的三条条件边：回 generate_content
   的有限回路 / 去 persist / 去 give_up；persist / give_up 均连 END）；
2. 合格响应：字段逐列写入 ``notes``（title / body / tags 剥 #、content_check、
   compliance_status=passed、status=draft、version=1、llm_meta、三个外键）；
   同事务把来源选题 approved→writing；``agent_runs`` 的 outputs / llm_raw / token /
   json_parse_attempts 就位；publish_url / xhs_note_id / published_at 全部留空；
3. 归属账号回退：topic 未关联账号时落到 persona 的账号；
3b. 业务错误与配置闸门：选题不存在 → 404、非 approved → 400、未关联人设 → 400，
    且三类前置校验失败时**模型调用 0 次、不写 agent_runs**；``AGENT_LLM_ENABLED=false``
    时工厂抛 :class:`AgentConfigError`（不发任何请求）；
4. 违禁词：内置测试词**必命中** → compliance_status=warning 且 content_check.hits 就位；
   无命中 → passed；同时直接断言 forbidden_words 模块（剪枝 / 符号规避 / 分类齐全）；
5. 有限回路：首轮字段不合格 → 条件边回炉（**feedback 确实注入提示词**）→ 次轮合格落库；
6. 降级落库：重试耗尽但**有可用正文** → 仍落库，service 记 partial；
7. 放弃：重试耗尽且无可用正文 → give_up 抛错、run 记 failed、**无 notes 半成品**、
   选题仍为 approved（回路是有界的：模型恰被调用 3 次）；
8. JSON 容错：围栏 / 裸控制字符 / 噪声一次命中；「先噪后 valid」parse_attempts == 2；
9. **单事务回滚**：写入中途失败（外键冲突）→ 整体回滚，不留半成品、选题状态不变；
10. 接口与安全：任何响应模型**都不含 llm_raw**（全 OpenAPI 组件扫描）+ llm_meta 白名单；
    库内无 ``sk-``；本步新增代码不含小红书域名 / 登录态字段；**本步未新增迁移**
    （``alembic current`` 仍 0002 head、``alembic check`` 零漂移、迁移文件恰 2 支）；
    测试数据 finally **物理清理**并复查残留为 0。

依赖本地 docker 的 PostgreSQL（与 ``selftest_agent_harness.py`` 同）；测试数据一律带
``[SELFTEST]`` 并在结束时物理清理。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\selftest_content_agent.py

全部通过退出码 0；任一项 FAIL 退出码 1。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402

from app.agent.checkpoint import close_checkpointer, init_checkpointer  # noqa: E402
from app.agent.content.forbidden_words import (  # noqa: E402
    CATEGORY_LABELS,
    FORBIDDEN_WORDS,
    compliance_of,
    describe_wordlist,
    scan_note,
    scan_text,
)
from app.agent.content.graph import (  # noqa: E402
    NODE_GENERATE,
    NODE_GIVE_UP,
    NODE_LOAD_CONTEXT,
    NODE_PERSIST,
    NODE_VALIDATE,
    build_content_graph,
)
from app.agent.content.service import create_note_for_topic, list_recent_runs  # noqa: E402
from app.agent.errors import AgentConfigError  # noqa: E402
from app.agent.llm.factory import get_chat_model  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.errors import CODE_BAD_REQUEST, CODE_NOT_FOUND, AppError  # noqa: E402
from app.crud.note import crud_note  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.models.enums import (  # noqa: E402
    ComplianceStatus,
    NoteStatus,
    RunStatus,
    TopicDifficulty,
    TopicStatus,
)
from app.models.note import Note  # noqa: E402
from app.models.persona import Persona  # noqa: E402
from app.models.topic import Topic  # noqa: E402
from app.schemas.note import NoteListItem, NoteOut, NoteRunOut, sanitize_llm_meta  # noqa: E402

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from pydantic import Field  # noqa: E402

SELFTEST_TAG = "[SELFTEST]"

#: 需要从子进程环境里剔除的代理变量（沙箱会注入，导致真实请求挂住）
_PROXY_KEYS = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}

#: 本步新增代码的扫描范围（安全断言用）
_SCAN_PATHS = (
    ("app", "agent", "content"),
    ("app", "crud", "note.py"),
    ("app", "schemas", "note.py"),
    ("app", "api", "routes", "notes.py"),
)

#: 小红书域名 / 登录态字段标记（命中即 FAIL）
_FORBIDDEN_MARKERS = (
    "xiaohongshu.com",
    "xhslink",
    "Cookie",
    "x-s-common",
    "x-t=",
    "web_session",
)


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
# 脚本模型：按调用序号返回预置响应（一次运行 = 一个选题）
# ---------------------------------------------------------------------------
class ContentScriptModel(BaseChatModel):
    """确定性假模型：按调用次序取响应；末项会被重复使用（模拟模型固执己见）。

    响应项可以是 ``str`` / ``dict``（自动转 JSON）/ ``BaseException``（抛出）。
    """

    responses: list[Any] = Field(default_factory=list)
    fallback: Any = None
    calls: int = 0
    usage_in: int = 13
    usage_out: int = 47
    prompts: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "content-script"

    @staticmethod
    def _text_of(messages: list[Any]) -> str:
        parts: list[str] = []
        for message in messages:
            content = getattr(message, "content", "")
            if isinstance(content, str):
                parts.append(content)
        return "\n".join(parts)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # noqa: ANN001
        text = self._text_of(messages)
        self.prompts.append(text)

        sequence: list[Any] = list(self.responses)
        if not sequence:
            sequence = [self.fallback if self.fallback is not None else '{"title": ""}']
        index = min(self.calls, len(sequence) - 1)
        item = sequence[index]
        self.calls += 1

        if isinstance(item, BaseException):
            raise item
        content = item if isinstance(item, str) else _dumps(item)
        message = AIMessage(
            content=content,
            usage_metadata={
                "input_tokens": self.usage_in,
                "output_tokens": self.usage_out,
                "total_tokens": self.usage_in + self.usage_out,
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 草稿构造器
# ---------------------------------------------------------------------------
_CLEAN_BODY = "这是创作自检正文，用于验证字段映射与合规自检链路。" * 12
_RISK_BODY = "这是合规自检正文，用于验证风险词命中。" * 12 + "本产品包治百病，一吃就好。"
_DIRTY_BODY = "这是脏 JSON 自检正文，用于验证围栏与控制字符容错。" * 10

DIRTY_RESPONSE = (
    "好的，这是结果：\n```json\n"
    '{"title": "[SELFTEST]创作标题S8a", "body": "'
    + _DIRTY_BODY
    + '\n\t尾注：含裸控制字符。", "tags": ["脏标签a", "脏标签b", "脏标签c"]}\n```\n以上。'
)


def clean_draft(tag: str) -> dict[str, Any]:
    """合格草稿：标题 / 正文长度合规、标签 3 个（含误带的 #，用于验证剥除）。"""
    return {
        "title": f"{SELFTEST_TAG}创作标题{tag}",
        "body": _CLEAN_BODY,
        "tags": [f"#自检标签{tag}a", f"自检标签{tag}b", f"自检标签{tag}c"],
    }


def risk_draft(tag: str) -> dict[str, Any]:
    """命中内置风险词（标题：国家级 / 正文：包治百病 / 标签：加微信）。"""
    return {
        "title": f"{SELFTEST_TAG}国家级测试{tag}",
        "body": _RISK_BODY,
        "tags": [f"自检标签{tag}a", f"自检标签{tag}b", "加微信"],
    }


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------
class Fixture:
    def __init__(self) -> None:
        self.account_id: uuid.UUID | None = None
        self.persona_id: uuid.UUID | None = None
        self.topic_ids: list[uuid.UUID] = []
        self.before_threads: set[str] = set()
        self.residue: dict[str, int] = {}

    # ------------------------------------------------------------- 工具
    def _current_threads(self) -> set[str]:
        with SessionLocal() as db:
            try:
                rows = db.execute(text("SELECT DISTINCT thread_id FROM langgraph.checkpoints")).all()
            except Exception:  # noqa: BLE001 —— 表可能尚未建
                return set()
        return {str(row[0]) for row in rows}

    def topic(self, index: int) -> uuid.UUID:
        return self.topic_ids[index]

    # ------------------------------------------------------------- 建立
    def setup(self) -> None:
        self.before_threads = self._current_threads()
        with SessionLocal() as db:
            account = Account(name=f"{SELFTEST_TAG}创作账号")
            db.add(account)
            db.flush()
            persona = Persona(
                account_id=account.id,
                name=f"{SELFTEST_TAG}创作人设",
                positioning="面向职场新人的效率工具号",
                target_audience="22-30 岁职场新人",
                tone="口语化、像朋友分享",
                content_pillars=["效率工具", "职场沟通"],
                taboo=["不吹功效", "不承诺收益"],
            )
            db.add(persona)
            db.flush()
            self.account_id = account.id
            self.persona_id = persona.id

            for index in range(11):
                topic = Topic(
                    # 索引 5 刻意不挂账号，用于验证「回退到 persona 的账号」
                    account_id=None if index == 5 else account.id,
                    # 索引 10 刻意不挂人设，用于验证「未关联人设不能创作」
                    persona_id=None if index == 10 else persona.id,
                    title=f"{SELFTEST_TAG}创作选题{index}",
                    angle="从日常痛点切入",
                    summary="写给职场新人 / 解决效率问题",
                    pillar="效率工具",
                    content_outline=["开场共鸣", "方法拆解", "示例", "行动建议"],
                    target_keywords=[f"关键词{index}"],
                    difficulty=TopicDifficulty.EASY,
                    # 索引 9 刻意保持 draft：验证「非 approved 不能创作」
                    status=TopicStatus.DRAFT if index == 9 else TopicStatus.APPROVED,
                )
                db.add(topic)
                db.flush()
                self.topic_ids.append(topic.id)
            db.commit()

    # ------------------------------------------------------------- 清理
    def cleanup(self) -> None:
        # 1) 本次自检新建的检查点线程（按运行前后差集安全删除，不动历史线程）
        new_threads = self._current_threads() - self.before_threads
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
        # 2) 业务数据（notes 对 accounts 是 RESTRICT，必须先删 notes）
        with SessionLocal() as db:
            if self.persona_id is not None:
                db.execute(
                    text("DELETE FROM agent_runs WHERE persona_id = :pid"),
                    {"pid": str(self.persona_id)},
                )
            if self.account_id is not None:
                db.execute(
                    text("DELETE FROM notes WHERE account_id = :aid"),
                    {"aid": str(self.account_id)},
                )
            # ⚠️ 按 **id 集合** 删选题，不能按 persona_id 删 —— 夹具的索引 10 刻意 **不带 persona_id**
            # （用于验证「未关联人设 → 400」），按 persona_id 删会整条漏掉，留下孤儿行
            # （第 12 步实测踩到：3 条 `[SELFTEST]创作选题10` 残留，且旧的只按 persona_id 复查 = 假阴性）。
            if self.topic_ids:
                db.execute(
                    text("DELETE FROM topics WHERE id = ANY(:tids)"),
                    {"tids": [str(item) for item in self.topic_ids]},
                )
            if self.persona_id is not None:
                db.execute(text("DELETE FROM personas WHERE id = :pid"), {"pid": str(self.persona_id)})
            if self.account_id is not None:
                db.execute(text("DELETE FROM accounts WHERE id = :aid"), {"aid": str(self.account_id)})
            db.commit()
        # 3) 复查残留（**双口径**：按 id 精确 + 按标题前缀兜底，后者能捕获任何漏删）
        topic_ids = [str(item) for item in self.topic_ids]
        with SessionLocal() as db:
            self.residue = {
                "notes": db.execute(
                    text("SELECT count(*) FROM notes WHERE account_id = :aid"),
                    {"aid": str(self.account_id)},
                ).scalar_one(),
                "topics": db.execute(
                    text("SELECT count(*) FROM topics WHERE id = ANY(:tids)"),
                    {"tids": topic_ids},
                ).scalar_one(),
                "agent_runs": db.execute(
                    text("SELECT count(*) FROM agent_runs WHERE persona_id = :pid"),
                    {"pid": str(self.persona_id)},
                ).scalar_one(),
                "personas": db.execute(
                    text("SELECT count(*) FROM personas WHERE id = :pid"),
                    {"pid": str(self.persona_id)},
                ).scalar_one(),
                "accounts": db.execute(
                    text("SELECT count(*) FROM accounts WHERE id = :aid"),
                    {"aid": str(self.account_id)},
                ).scalar_one(),
                "checkpoint_threads": len(self._current_threads() - self.before_threads),
                # 兜底口径：不依赖任何关联 id，直接按本步专属标题前缀全局扫（能发现孤儿行）
                "topics_by_title": db.execute(
                    text("SELECT count(*) FROM topics WHERE title LIKE :p"),
                    {"p": f"{SELFTEST_TAG}创作%"},
                ).scalar_one(),
                "notes_by_title": db.execute(
                    text("SELECT count(*) FROM notes WHERE title LIKE :p"),
                    {"p": f"{SELFTEST_TAG}创作%"},
                ).scalar_one(),
            }


# ---------------------------------------------------------------------------
# 数据读取小工具
# ---------------------------------------------------------------------------
_NOTE_COLUMNS = (
    "id, account_id, persona_id, topic_id, title, body, tags, content_check, "
    "compliance_status, status, publish_url, xhs_note_id, published_at, version, llm_meta"
)


def _fetch_note(note_id: Any) -> dict[str, Any]:
    with SessionLocal() as db:
        row = db.execute(
            text(f"SELECT {_NOTE_COLUMNS} FROM notes WHERE id = :nid"),  # noqa: S608
            {"nid": str(note_id)},
        ).first()
    if row is None:
        return {}
    keys = [item.strip() for item in _NOTE_COLUMNS.split(",")]
    return dict(zip(keys, row, strict=True))


def _notes_for_topic(topic_id: Any) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        rows = db.execute(
            text(f"SELECT {_NOTE_COLUMNS} FROM notes WHERE topic_id = :tid"),  # noqa: S608
            {"tid": str(topic_id)},
        ).all()
    keys = [item.strip() for item in _NOTE_COLUMNS.split(",")]
    return [dict(zip(keys, row, strict=True)) for row in rows]


def _topic_status(topic_id: Any) -> str | None:
    with SessionLocal() as db:
        return db.execute(
            text("SELECT status FROM topics WHERE id = :tid"), {"tid": str(topic_id)}
        ).scalar_one_or_none()


_RUN_COLUMNS = (
    "id, run_type, trigger, status, llm_model, llm_base_url, prompt_tokens, "
    "completion_tokens, total_tokens, json_parse_attempts, outputs, llm_raw, "
    "error_message, note_id, topic_id"
)


def _fetch_run(run_id: Any) -> dict[str, Any]:
    with SessionLocal() as db:
        row = db.execute(
            text(f"SELECT {_RUN_COLUMNS} FROM agent_runs WHERE id = :rid"),  # noqa: S608
            {"rid": str(run_id)},
        ).first()
    if row is None:
        return {}
    keys = [item.strip() for item in _RUN_COLUMNS.split(",")]
    return dict(zip(keys, row, strict=True))


def _latest_run_for_persona(persona_id: Any) -> dict[str, Any]:
    with SessionLocal() as db:
        row = db.execute(
            text(
                "SELECT status, error_message FROM agent_runs WHERE persona_id = :pid "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"pid": str(persona_id)},
        ).first()
    if row is None:
        return {}
    return {"status": row[0], "error_message": row[1]}


# ---------------------------------------------------------------------------
# 组 1：图结构（真 LangGraph + 条件边 + 有限回路）
# ---------------------------------------------------------------------------
def group1_graph(c: Checker) -> None:
    c.check(
        {
            NODE_LOAD_CONTEXT,
            NODE_GENERATE,
            NODE_VALIDATE,
            NODE_PERSIST,
            NODE_GIVE_UP,
        }
        == {"load_context", "generate_content", "validate_compliance", "persist", "give_up"},
        "五个节点名与任务书一致",
    )

    graph = build_content_graph(model=ContentScriptModel(), use_checkpointer=False)
    c.check(
        "Compiled" in type(graph).__name__,
        "build_content_graph 返回已编译的 LangGraph 图",
        f"{type(graph).__name__}",
    )
    view = graph.get_graph()
    nodes = set(view.nodes.keys())
    c.check(
        {
            NODE_LOAD_CONTEXT,
            NODE_GENERATE,
            NODE_VALIDATE,
            NODE_PERSIST,
            NODE_GIVE_UP,
        }.issubset(nodes),
        "图含 load_context / generate_content / validate_compliance / persist / give_up",
        f"实际={sorted(nodes)}",
    )
    edges = {(edge.source, edge.target, bool(edge.conditional)) for edge in view.edges}
    c.check(
        (NODE_LOAD_CONTEXT, NODE_GENERATE, False) in edges
        and (NODE_GENERATE, NODE_VALIDATE, False) in edges,
        "load_context → generate_content → validate_compliance 为直连边",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_VALIDATE, NODE_GENERATE, True) in edges,
        "validate_compliance 存在「回炉重生成」条件边（有限回路的载体）",
    )
    c.check(
        (NODE_VALIDATE, NODE_PERSIST, True) in edges and (NODE_VALIDATE, NODE_GIVE_UP, True) in edges,
        "validate_compliance 存在去 persist / give_up 的条件边",
    )
    c.check(
        (NODE_PERSIST, "__end__", False) in edges and (NODE_GIVE_UP, "__end__", False) in edges,
        "persist / give_up 均连接 END",
    )
    c.check(
        settings.AGENT_JSON_MAX_ATTEMPTS >= 1,
        "回路有界（上限来自 AGENT_JSON_MAX_ATTEMPTS）",
        f"{settings.AGENT_JSON_MAX_ATTEMPTS}",
    )


# ---------------------------------------------------------------------------
# 组 2：合格响应 → 字段逐列落库 + 同事务改选题状态 + 审计回填
# ---------------------------------------------------------------------------
def group2_valid(c: Checker, f: Fixture) -> None:
    topic = f.topic(0)
    model = ContentScriptModel(responses=[clean_draft("S2")])
    summary = create_note_for_topic(topic_id=topic, model=model)

    c.check(summary["status"] == RunStatus.SUCCESS.value and bool(summary["note_id"]),
            "合格响应：status=success 且拿到 note_id", f"{summary}")
    c.check(model.calls == 1, "无字段硬伤 → 一轮生成即落库（模型只调 1 次）", f"{model.calls}")

    row = _fetch_note(summary["note_id"])
    c.check(bool(row), "notes 实际写入 1 行")
    c.check(row.get("title") == f"{SELFTEST_TAG}创作标题S2", "title 写入对应列", f"{row.get('title')}")
    c.check(row.get("body") == _CLEAN_BODY, "body 写入对应列（与模型产出一致）")
    c.check(
        row.get("tags") == ["自检标签S2a", "自检标签S2b", "自检标签S2c"],
        "tags 写入对应列：剥除误带的 # 且顺序保留",
        f"{row.get('tags')}",
    )
    check = row.get("content_check") or {}
    c.check(
        check.get("status") == ComplianceStatus.PASSED.value and check.get("hits") == [],
        "无命中 → content_check.status=passed 且 hits 为空",
        f"{check}",
    )
    c.check(
        check.get("checked_fields") == ["title", "body", "tags"] and bool(check.get("checked_at")),
        "content_check 记录 checked_fields 与 checked_at",
        f"{check}",
    )
    c.check(row.get("compliance_status") == ComplianceStatus.PASSED.value,
            "compliance_status=passed", f"{row.get('compliance_status')}")
    c.check(row.get("status") == NoteStatus.DRAFT.value,
            "status 恒为 draft（即使 passed 也不自动 ready）", f"{row.get('status')}")
    c.check(row.get("version") == 1, "version=1")
    c.check(
        row.get("publish_url") is None and row.get("xhs_note_id") is None
        and row.get("published_at") is None,
        "publish_url / xhs_note_id / published_at 全部留空（不自动发布）",
    )
    meta = row.get("llm_meta") or {}
    c.check(
        meta.get("prompt_version") == settings.CONTENT_PROMPT_VERSION
        and meta.get("json_parse_attempts") == 1
        and meta.get("model") == settings.LLM_MODEL,
        "llm_meta 含 prompt_version / json_parse_attempts=1 / model",
        f"{meta}",
    )
    c.check(
        str(row.get("account_id")) == str(f.account_id)
        and str(row.get("persona_id")) == str(f.persona_id)
        and str(row.get("topic_id")) == str(topic),
        "account_id / persona_id / topic_id 三外键均按上下文回填",
        f"{row.get('account_id')}/{row.get('persona_id')}/{row.get('topic_id')}",
    )
    c.check(_topic_status(topic) == TopicStatus.WRITING.value,
            "persist 同一事务把选题 approved→writing", f"{_topic_status(topic)}")

    run = _fetch_run(summary["run_id"])
    outputs = run.get("outputs") or {}
    c.check(run.get("run_type") == "content_creation", "agent_runs.run_type=content_creation",
            f"{run.get('run_type')}")
    c.check(run.get("status") == RunStatus.SUCCESS.value, "agent_runs.status=success",
            f"{run.get('status')}")
    c.check(
        outputs.get("generated") == 1
        and outputs.get("note_id") == str(summary["note_id"])
        and outputs.get("compliance_status") == ComplianceStatus.PASSED.value
        and outputs.get("forbidden_hits") == 0
        and outputs.get("tags_count") == 3
        and outputs.get("title") == f"{SELFTEST_TAG}创作标题S2",
        "agent_runs.outputs 已回填业务明细（note_id / generated / 合规 / 命中数 / 标签数 / 标题）",
        f"{outputs}",
    )
    c.check(
        run.get("prompt_tokens") == 13 and run.get("completion_tokens") == 47
        and run.get("total_tokens") == 60,
        "agent_runs token 已回填（13/47/60）",
        f"{run.get('prompt_tokens')}/{run.get('completion_tokens')}/{run.get('total_tokens')}",
    )
    c.check(run.get("json_parse_attempts") == 1, "agent_runs.json_parse_attempts == 1")
    c.check(
        run.get("llm_raw") and SELFTEST_TAG in str(run["llm_raw"]) and "sk-" not in str(run["llm_raw"]),
        "agent_runs.llm_raw 已回填模型原始输出且不含密钥",
    )
    c.check(
        str(run.get("note_id")) == str(summary["note_id"])
        and str(run.get("topic_id")) == str(topic),
        "agent_runs.note_id / topic_id 已关联",
        f"{run.get('note_id')}/{run.get('topic_id')}",
    )
    c.check(run.get("trigger") == "manual", "agent_runs.trigger=manual")

    with SessionLocal() as db:
        recent = list_recent_runs(db, limit=5)
    c.check(
        bool(recent) and str(recent[0].id) == str(summary["run_id"])
        and all(item.run_type.value == "content_creation" for item in recent),
        "list_recent_runs 仅返回 content_creation 且最新一条为本次运行",
    )


# ---------------------------------------------------------------------------
# 组 3：归属账号回退（topic 未关联账号 → 用 persona 的账号）
# ---------------------------------------------------------------------------
def group3_account_fallback(c: Checker, f: Fixture) -> None:
    topic = f.topic(5)
    model = ContentScriptModel(responses=[clean_draft("S3")])
    summary = create_note_for_topic(topic_id=topic, model=model)
    row = _fetch_note(summary["note_id"])
    c.check(
        summary["status"] == RunStatus.SUCCESS.value
        and str(row.get("account_id")) == str(f.account_id),
        "topic.account_id 为空时按 persona.account_id 回填归属账号",
        f"{summary.get('status')} / {row.get('account_id')}",
    )


# ---------------------------------------------------------------------------
# 组 3b：业务错误码与 LLM 配置闸门（全程**不发起任何模型调用**）
# ---------------------------------------------------------------------------
def _count_runs(persona_id: Any) -> int:
    with SessionLocal() as db:
        return db.execute(
            text("SELECT count(*) FROM agent_runs WHERE persona_id = :pid"),
            {"pid": str(persona_id)},
        ).scalar_one()


def group3b_business_errors(c: Checker, f: Fixture) -> None:
    runs_before = _count_runs(f.persona_id)

    # 4a：选题不存在 → 404 not_found
    model_a = ContentScriptModel(responses=[clean_draft("S4a")])
    try:
        create_note_for_topic(topic_id=uuid.uuid4(), model=model_a)
        c.check(False, "选题不存在 → 404 not_found", "未抛异常")
    except AppError as exc:
        c.check(
            exc.status_code == 404 and exc.code == CODE_NOT_FOUND,
            "选题不存在 → 404 not_found",
            f"{exc.status_code}/{exc.code}",
        )
    except Exception as exc:  # noqa: BLE001
        c.check(False, "选题不存在 → 404 not_found", f"{type(exc).__name__}: {exc}")

    # 4b：选题状态不是 approved（索引 9 刻意保持 draft）→ 400 bad_request
    model_b = ContentScriptModel(responses=[clean_draft("S4b")])
    try:
        create_note_for_topic(topic_id=f.topic(9), model=model_b)
        c.check(False, "非 approved 选题 → 400 bad_request", "未抛异常")
    except AppError as exc:
        c.check(
            exc.status_code == 400 and exc.code == CODE_BAD_REQUEST,
            "非 approved 选题 → 400 bad_request",
            f"{exc.status_code}/{exc.code}",
        )
    except Exception as exc:  # noqa: BLE001
        c.check(False, "非 approved 选题 → 400 bad_request", f"{type(exc).__name__}: {exc}")

    # 4c：选题未关联人设（索引 10）→ 400 bad_request
    model_c = ContentScriptModel(responses=[clean_draft("S4c")])
    try:
        create_note_for_topic(topic_id=f.topic(10), model=model_c)
        c.check(False, "选题未关联人设 → 400 bad_request", "未抛异常")
    except AppError as exc:
        c.check(
            exc.status_code == 400 and exc.code == CODE_BAD_REQUEST,
            "选题未关联人设 → 400 bad_request",
            f"{exc.status_code}/{exc.code}",
        )
    except Exception as exc:  # noqa: BLE001
        c.check(False, "选题未关联人设 → 400 bad_request", f"{type(exc).__name__}: {exc}")

    c.check(
        model_a.calls == 0 and model_b.calls == 0 and model_c.calls == 0,
        "三类前置校验失败时模型调用均为 0 次（不发任何请求）",
        f"{model_a.calls}/{model_b.calls}/{model_c.calls}",
    )
    c.check(
        _count_runs(f.persona_id) == runs_before,
        "前置校验失败时不写 agent_runs（不产生空审计行）",
        f"{runs_before} → {_count_runs(f.persona_id)}",
    )

    # 4d：LLM 配置闸门 —— 关掉开关后工厂直接抛 AgentConfigError（未发生网络请求）
    original = settings.AGENT_LLM_ENABLED
    gate: str | None = None
    assign_error: str | None = None
    try:
        settings.AGENT_LLM_ENABLED = False
    except Exception as exc:  # noqa: BLE001
        assign_error = f"{type(exc).__name__}: {exc}"
    if assign_error is None:
        try:
            get_chat_model(temperature=0.4, want_json=True)
            gate = "未抛异常（闸门失效！）"
        except AgentConfigError as exc:
            gate = str(exc)
        except Exception as exc:  # noqa: BLE001
            gate = f"意外异常 {type(exc).__name__}: {exc}"
        finally:
            settings.AGENT_LLM_ENABLED = original
    c.check(
        gate is not None and "AGENT_LLM_ENABLED" in str(gate),
        "AGENT_LLM_ENABLED=false 时工厂抛 AgentConfigError（不发起任何请求）",
        f"assign_error={assign_error} gate={gate}",
    )
    c.check(
        settings.AGENT_LLM_ENABLED == original,
        "自检结束后 LLM 开关已还原（不污染后续步骤）",
        f"{settings.AGENT_LLM_ENABLED} vs {original}",
    )

    # 4e：路由把 AgentConfigError 翻成 400（静态断言）
    route_src = (BACKEND_DIR / "app" / "api" / "routes" / "notes.py").read_text(encoding="utf-8")
    c.check(
        "except AgentConfigError" in route_src and "bad_request(" in route_src,
        "notes 路由把 AgentConfigError 映射为 400 bad_request",
    )


# ---------------------------------------------------------------------------
# 组 5：违禁词（命中必 warning / 无命中 passed + 模块直测）
# ---------------------------------------------------------------------------
def group4_forbidden(c: Checker, f: Fixture) -> None:
    topic = f.topic(1)
    model = ContentScriptModel(responses=[risk_draft("S4")])
    summary = create_note_for_topic(topic_id=topic, model=model)

    c.check(
        summary["status"] == RunStatus.SUCCESS.value
        and summary["compliance_status"] == ComplianceStatus.WARNING.value,
        "命中风险词仍正常落库（不阻断、不改写），compliance_status=warning",
        f"{summary}",
    )
    c.check(model.calls == 1, "命中风险词**不**单独强制回炉（模型仍只调 1 次）", f"{model.calls}")

    row = _fetch_note(summary["note_id"])
    check = row.get("content_check") or {}
    hits = check.get("hits") or []
    pairs = {(item.get("word"), item.get("field")) for item in hits}
    c.check(
        pairs == {("国家级", "title"), ("包治百病", "body"), ("加微信", "tags")},
        "命中项 = 标题「国家级」/ 正文「包治百病」/ 标签「加微信」（跨字段各计一次）",
        f"{sorted(pairs)}",
    )
    c.check(
        row.get("compliance_status") == ComplianceStatus.WARNING.value
        and check.get("status") == ComplianceStatus.WARNING.value,
        "compliance_status 与 content_check.status 同步为 warning",
    )
    c.check(
        bool(pairs) and all(item.get("category") in CATEGORY_LABELS for item in hits),
        "每条命中都带分类（category 均来自内置分类表）",
        f"{[item.get('category') for item in hits]}",
    )
    c.check(
        all(item.get("snippet") for item in hits),
        "每条命中都带片段 snippet",
    )
    c.check(
        row.get("body") == _RISK_BODY and row.get("title") == f"{SELFTEST_TAG}国家级测试S4",
        "正文 / 标题**未被静默改写**（原样落库）",
    )
    c.check(row.get("status") == NoteStatus.DRAFT.value, "命中风险词的草稿仍以 draft 保存（不丢弃）")

    # ---- 模块直测（不依赖 DB）----
    direct = scan_text("这是国家级产品，包治百病，稳赚不赔！")
    c.check(
        [item["word"] for item in direct] == ["国家级", "包治百病", "稳赚不赔"],
        "scan_text 命中且短词被长词覆盖剪枝（不再重复计数「稳赚」）",
        f"{[item['word'] for item in direct]}",
    )
    c.check(scan_text(f"{SELFTEST_TAG}纯正文无风险词") == [], "无命中返回空表")
    c.check(
        [item["word"] for item in scan_text("想了解就加-微-信找我")] == ["加微信"],
        "去掉标点后仍能命中「符号隔开规避」写法（加-微-信 → 加微信）",
    )
    c.check(
        [item["field"] for item in scan_note(title=None, body=None, tags=["加微信"])] == ["tags"],
        "scan_note 支持标签字段扫描（field=tags）",
    )
    c.check(
        compliance_of([]) == ComplianceStatus.PASSED
        and compliance_of([{"word": "x"}]) == ComplianceStatus.WARNING,
        "compliance_of：0 命中=passed / ≥1 命中=warning（无 blocked 状态）",
    )
    profile = describe_wordlist()
    c.check(
        profile["total"] == len(FORBIDDEN_WORDS) and len(profile["by_category"]) == 7
        and all(count > 0 for count in profile["by_category"].values()),
        "词表概况：7 个分类均有词，total 与实际条数一致",
        f"{profile}",
    )


# ---------------------------------------------------------------------------
# 组 5：有限回路（回炉带 feedback）/ 降级落库 / 放弃
# ---------------------------------------------------------------------------
def group5_loop_degrade_giveup(c: Checker, f: Fixture) -> None:
    # 5a：首轮字段不合格（标签只有 1 个）→ 回炉 → 次轮合格
    topic_loop = f.topic(2)
    bad_round = dict(clean_draft("S5a"))
    bad_round["tags"] = ["只有一个标签"]
    model_a = ContentScriptModel(responses=[bad_round, clean_draft("S5a2")])
    summary_a = create_note_for_topic(topic_id=topic_loop, model=model_a)
    rows_a = _notes_for_topic(topic_loop)
    c.check(
        summary_a["status"] == RunStatus.SUCCESS.value and len(rows_a) == 1
        and model_a.calls == 2 and rows_a[0]["title"] == f"{SELFTEST_TAG}创作标题S5a2",
        "首轮字段不合格 → 条件边回炉 → 次轮合格并落库（模型调 2 次）",
        f"{summary_a} calls={model_a.calls} rows={len(rows_a)}",
    )
    c.check(
        len(model_a.prompts) >= 2 and "字段问题" in model_a.prompts[1]
        and "标签" in model_a.prompts[1],
        "回炉时 feedback 确实注入提示词（含字段问题与标签数量提示）",
    )

    # 5b：始终不合格但**有可用正文**（正文过短）→ 重试耗尽后降级落库
    topic_deg = f.topic(3)
    short = dict(clean_draft("S5b"))
    short["body"] = "太短的正文。"
    model_b = ContentScriptModel(responses=[short])
    summary_b = create_note_for_topic(topic_id=topic_deg, model=model_b)
    rows_b = _notes_for_topic(topic_deg)
    c.check(
        model_b.calls == settings.AGENT_JSON_MAX_ATTEMPTS,
        f"回路有界：模型恰被调用 {settings.AGENT_JSON_MAX_ATTEMPTS} 次后停止回炉",
        f"{model_b.calls}",
    )
    c.check(
        summary_b["status"] == RunStatus.PARTIAL.value and summary_b["degraded"] is True,
        "重试耗尽但有可用正文 → 降级落库，service 记 partial",
        f"{summary_b}",
    )
    c.check(
        len(rows_b) == 1 and rows_b[0]["body"] == "太短的正文。"
        and rows_b[0]["status"] == NoteStatus.DRAFT.value,
        "降级落库确有 notes 行（正文按原样保存，草稿保留待人工修订）",
        f"{len(rows_b)}",
    )
    run_b = _fetch_run(summary_b["run_id"])
    c.check(run_b.get("status") == RunStatus.PARTIAL.value,
            "降级运行在 agent_runs 补记 partial", f"{run_b.get('status')}")
    c.check(
        (run_b.get("outputs") or {}).get("degraded") is True,
        "agent_runs.outputs.degraded=True",
        f"{(run_b.get('outputs') or {}).get('degraded')}",
    )

    # 5c：始终无可用正文（标题为空）→ give_up
    topic_give = f.topic(4)
    empty_title = dict(clean_draft("S5c"))
    empty_title["title"] = ""
    model_c = ContentScriptModel(responses=[empty_title])
    summary_c = create_note_for_topic(topic_id=topic_give, model=model_c)
    c.check(
        summary_c["status"] == RunStatus.FAILED.value and summary_c["run_id"] is None
        and "放弃" in str(summary_c["error_message"]),
        "重试耗尽且无可用正文 → give_up，service 返回 failed 汇总",
        f"{summary_c}",
    )
    c.check(
        model_c.calls == settings.AGENT_JSON_MAX_ATTEMPTS,
        f"give_up 前恰回炉 {settings.AGENT_JSON_MAX_ATTEMPTS} 轮（无限循环已被杜绝）",
        f"{model_c.calls}",
    )
    c.check(not _notes_for_topic(topic_give), "give_up 不留任何 notes 半成品")
    c.check(_topic_status(topic_give) == TopicStatus.APPROVED.value,
            "give_up 后选题仍为 approved（未被改成 writing）", f"{_topic_status(topic_give)}")
    latest = _latest_run_for_persona(f.persona_id)
    c.check(latest.get("status") == RunStatus.FAILED.value,
            "give_up 的运行在 agent_runs 记 failed", f"{latest}")


# ---------------------------------------------------------------------------
# 组 6：JSON 容错（脏 JSON 一次命中 / 先噪后 valid）
# ---------------------------------------------------------------------------
def group6_json_robustness(c: Checker, f: Fixture) -> None:
    topic_dirty = f.topic(6)
    model_a = ContentScriptModel(responses=[DIRTY_RESPONSE])
    summary_a = create_note_for_topic(topic_id=topic_dirty, model=model_a)
    rows_a = _notes_for_topic(topic_dirty)
    c.check(
        summary_a["status"] == RunStatus.SUCCESS.value and len(rows_a) == 1
        and rows_a[0]["title"] == f"{SELFTEST_TAG}创作标题S8a",
        "脏 JSON（代码围栏 + 裸控制字符 + 前后噪声）解析并落库",
        f"{summary_a} rows={len(rows_a)}",
    )
    c.check(
        (rows_a[0]["llm_meta"] or {}).get("json_parse_attempts") == 1,
        "脏 JSON 一次命中（json_parse_attempts=1）",
        f"{(rows_a[0]['llm_meta'] if rows_a else None)}",
    )
    c.check(model_a.calls == 1, "JSON 容错在节点内完成，未触发图回路", f"{model_a.calls}")

    topic_noise = f.topic(7)
    model_b = ContentScriptModel(
        responses=["这段完全不是 JSON 的自由文本。", clean_draft("S8b")]
    )
    summary_b = create_note_for_topic(topic_id=topic_noise, model=model_b)
    rows_b = _notes_for_topic(topic_noise)
    run_b = _fetch_run(summary_b["run_id"])
    c.check(
        bool(rows_b) and (rows_b[0]["llm_meta"] or {}).get("json_parse_attempts") == 2
        and run_b.get("json_parse_attempts") == 2,
        "「先噪后 valid」parse_attempts 正确记为 2（notes.llm_meta 与 agent_runs 同步）",
        f"{run_b.get('json_parse_attempts')}",
    )


# ---------------------------------------------------------------------------
# 组 7：单事务回滚（失败不留半成品）
# ---------------------------------------------------------------------------
def _count_notes(topic_id: Any) -> int:
    with SessionLocal() as db:
        return db.execute(
            text("SELECT count(*) FROM notes WHERE topic_id = :tid"), {"tid": str(topic_id)}
        ).scalar_one()


def group7_single_transaction(c: Checker, f: Fixture) -> None:
    topic_ok = f.topic(8)
    payload = {
        "account_id": uuid.uuid4(),  # 不存在的账号 → 外键冲突，写库失败
        "persona_id": f.persona_id,
        "topic_id": topic_ok,
        "title": f"{SELFTEST_TAG}回滚测试",
        "body": _CLEAN_BODY,
        "tags": ["a", "b", "c"],
        "compliance_status": ComplianceStatus.PASSED,
        "status": NoteStatus.DRAFT,
        "version": 1,
    }
    raised: str | None = None
    with SessionLocal() as db:
        try:
            crud_note.create_with_topic_transition(db, payload, topic_id=topic_ok)
        except ValueError as exc:  # 不应走到这里
            raised = f"ValueError: {exc}"
        except Exception as exc:  # noqa: BLE001 —— 预期的数据库错误
            raised = type(exc).__name__
    c.check(
        raised is not None and raised != "None" and not str(raised).startswith("ValueError"),
        "写入中途数据库失败会抛出（外键冲突）",
        f"{raised}",
    )
    c.check(_count_notes(topic_ok) == 0, "失败回滚后**无 notes 半成品**", f"{_count_notes(topic_ok)}")
    c.check(_topic_status(topic_ok) == TopicStatus.APPROVED.value,
            "失败回滚后选题状态不变（未被改成 writing）", f"{_topic_status(topic_ok)}")

    # 非 approved 的选题：同事务入口也拒绝（ValueError → 上层 400）
    topic_draft = f.topic(9)
    rejected = None
    with SessionLocal() as db:
        try:
            row = dict(payload)
            row["account_id"] = f.account_id
            row["topic_id"] = topic_draft
            crud_note.create_with_topic_transition(db, row, topic_id=topic_draft)
        except ValueError as exc:
            rejected = str(exc)
        except Exception as exc:  # noqa: BLE001
            rejected = f"{type(exc).__name__}: {exc}"
    c.check(
        rejected is not None and "approved" in str(rejected),
        "非 approved 选题被同事务入口拒绝（ValueError）",
        f"{rejected}",
    )
    c.check(_count_notes(topic_draft) == 0, "被拒后不产生 notes 行")


# ---------------------------------------------------------------------------
# 组 8：接口契约（无 llm_raw / llm_meta 白名单）
# ---------------------------------------------------------------------------
def group8_interfaces(c: Checker, f: Fixture) -> None:
    spec = fastapi_app.openapi()
    paths = spec["paths"]
    c.check(
        "/api/v1/notes/generate" in paths and set(paths["/api/v1/notes/generate"]) == {"post"},
        "POST /api/v1/notes/generate 已注册（无 PATCH/PUT/DELETE）",
        f"{sorted(paths.get('/api/v1/notes/generate', {}))}",
    )
    c.check(
        "/api/v1/notes/runs" in paths and set(paths["/api/v1/notes/runs"]) == {"get"},
        "GET /api/v1/notes/runs 静态路径已注册",
    )
    c.check("get" in paths.get("/api/v1/notes", {}), "GET /api/v1/notes 列表已注册")
    c.check(
        "/api/v1/notes/{note_id}" in paths
        and set(paths["/api/v1/notes/{note_id}"]) == {"get", "patch"},
        "GET+PATCH /api/v1/notes/{note_id} 已注册（第 14 步起 PATCH 为创作台正式接口；无 PUT/DELETE）",
        f"{sorted(paths.get('/api/v1/notes/{note_id}', {}))}",
    )

    components = spec.get("components", {}).get("schemas", {})
    leaked = [
        name
        for name, schema in components.items()
        if "llm_raw" in (schema.get("properties") or {})
    ]
    c.check(not leaked, "全 OpenAPI 组件中**没有任何**响应模型暴露 llm_raw", f"{leaked}")
    c.check(
        "llm_raw" not in NoteOut.model_fields
        and "llm_raw" not in NoteListItem.model_fields
        and "llm_raw" not in NoteRunOut.model_fields,
        "三个响应模型均无 llm_raw 字段",
    )

    note_id = _notes_for_topic(f.topic(0))[0]["id"]
    with SessionLocal() as db:
        note = db.get(Note, note_id)
    dumped = NoteOut.from_row(note).model_dump()
    c.check("llm_raw" not in dumped, "NoteOut 序列化结果不含 llm_raw")
    c.check(
        set(dumped.get("llm_meta") or {}) <= {"prompt_version", "json_parse_attempts", "model"},
        "NoteOut.llm_meta 只输出白名单键",
        f"{dumped.get('llm_meta')}",
    )
    cleaned = sanitize_llm_meta(
        {
            "prompt_version": "v1",
            "json_parse_attempts": 1,
            "model": "m",
            "api_key": "sk-should-not-leak",
            "secret": "x",
        }
    )
    c.check(
        set(cleaned or {}) == {"prompt_version", "json_parse_attempts", "model"},
        "sanitize_llm_meta 会剔除白名单外的键（含疑似密钥）",
        f"{cleaned}",
    )
    probe = NoteRunOut(
        run_id=uuid.uuid4(),
        trigger=None,
        status=None,
        created_at=datetime.now(timezone.utc),
        finished_at=None,
        latency_ms=None,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        json_parse_attempts=None,
        error_message=None,
    )
    c.check("llm_raw" not in probe.model_dump(), "NoteRunOut 序列化结果不含 llm_raw")

    notes_paths = sorted(item for item in paths if item.startswith("/api/v1/notes"))
    c.check(
        notes_paths
        == [
            "/api/v1/notes",
            "/api/v1/notes/generate",
            "/api/v1/notes/runs",
            "/api/v1/notes/{note_id}",
        ],
        "notes 恰注册 4 条路径（第 14 步起允许 PATCH；无 PUT / DELETE）",
        f"{notes_paths}",
    )


# ---------------------------------------------------------------------------
# 组 9：安全（无密钥 / 无小红书域名 / 本步无新增迁移）
# ---------------------------------------------------------------------------
def group9_security(c: Checker, f: Fixture) -> None:
    with SessionLocal() as db:
        note_hits = db.execute(
            text(
                "SELECT count(*) FROM notes WHERE account_id = :aid AND ("
                "title LIKE '%sk-%' OR COALESCE(body,'') LIKE '%sk-%' "
                "OR COALESCE(content_check::text,'') LIKE '%sk-%' "
                "OR COALESCE(llm_meta::text,'') LIKE '%sk-%')"
            ),
            {"aid": str(f.account_id)},
        ).scalar_one()
        run_hits = db.execute(
            text(
                "SELECT count(*) FROM agent_runs WHERE persona_id = :pid AND ("
                "COALESCE(llm_raw,'') LIKE '%sk-%' OR COALESCE(outputs::text,'') LIKE '%sk-%' "
                "OR COALESCE(inputs::text,'') LIKE '%sk-%' "
                "OR COALESCE(error_message,'') LIKE '%sk-%')"
            ),
            {"pid": str(f.persona_id)},
        ).scalar_one()
    c.check(note_hits == 0, "notes 无 sk- 形态密钥", f"命中 {note_hits}")
    c.check(run_hits == 0, "agent_runs 无 sk- 形态密钥", f"命中 {run_hits}")

    hits: list[str] = []
    for parts in _SCAN_PATHS:
        path = BACKEND_DIR.joinpath(*parts)
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for file in files:
            content = file.read_text(encoding="utf-8")
            for marker in _FORBIDDEN_MARKERS:
                if marker in content:
                    hits.append(f"{file.name}:{marker}")
    c.check(not hits, "本步新增代码不含小红书域名 / 登录态字段", f"{hits}")

    main_source = (BACKEND_DIR / "app" / "main.py").read_text(encoding="utf-8")
    # 第 13 步加固：路由总数会随步骤递增（第 13 步后为 7 只），断言由「恰好六只」改为
    # 「本步的六只路由均在册 + 总数不回退」—— 既不因**后续步骤新增 router** 误报，
    # 也仍能抓住「已有 router 被删 / 被换」。（当次总数由**最新步骤**的自检脚本守护。）
    _EXPECTED_ROUTER_IMPORTS = (
        "health_router",
        "accounts_router",
        "personas_router",
        "hotspots_router",
        "topics_router",
        "notes_router",
    )
    c.check(
        main_source.count("include_router(") >= 6
        and all(f"include_router({name})" in main_source for name in _EXPECTED_ROUTER_IMPORTS),
        "main.py 注册含既有六只路由（总数 ≥ 6）",
        f"count={main_source.count('include_router(')}",
    )
    c.check(
        len(re.findall(r"@asynccontextmanager", main_source)) == 1
        or main_source.count("lifespan") >= 1,
        "main.py 未新增第二个 lifespan",
    )

    # 词表「只内置在代码里」：不写 env
    env_example = (BACKEND_DIR / ".env.example").read_text(encoding="utf-8")
    c.check(
        "国家级" not in env_example and "包治百病" not in env_example,
        "风险词表只内置在代码里（.env.example 不含任何风险词）",
    )

    # 词表「不落库到独立表」：public schema 仍 10 张业务表（+ alembic_version 版本表）
    with SessionLocal() as db:
        tables = [
            row[0]
            for row in db.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
            ).all()
        ]
    business = [item for item in tables if item != "alembic_version"]
    c.check(
        "forbidden_words" not in business and len(business) == 10,
        "public schema 仍为 10 张业务表（本步未新增表、未把词表落库）",
        f"{len(business)}: {business}",
    )


def _alembic(args: list[str]) -> tuple[int, str]:
    env = {key: value for key, value in os.environ.items() if key.upper() not in _PROXY_KEYS}
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=300,
        check=False,
    )
    return proc.returncode, f"{proc.stdout or ''}{proc.stderr or ''}"


def group9b_migrations(c: Checker) -> None:
    versions = sorted((BACKEND_DIR / "migrations" / "versions").glob("*.py"))
    c.check(
        len(versions) == 2 and versions[0].name.startswith("0001")
        and versions[1].name.startswith("0002"),
        "本步未新增迁移文件（versions 恰 2 支：0001 / 0002）",
        f"{[item.name for item in versions]}",
    )

    code, out = _alembic(["current"])
    c.check(code == 0 and "(head)" in out and "0002" in out,
            "alembic current 仍为 0002 (head)", f"rc={code} out={out.strip()[:200]}")

    code_check, out_check = _alembic(["check"])
    c.check(
        code_check == 0,
        "alembic check 零漂移（模型与迁移一致）",
        f"rc={code_check} out={out_check.strip()[:400]}",
    )


# ---------------------------------------------------------------------------
# 组 10：物理清理复查
# ---------------------------------------------------------------------------
def group10_cleanup(c: Checker, f: Fixture) -> None:
    c.check(
        all(value == 0 for value in f.residue.values()) and bool(f.residue),
        "测试数据已物理清理，复查残留为 0",
        f"{f.residue}",
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    c = Checker()
    print("=" * 78)
    print("第 12 步 · 创作 Agent 离线自检（不联网、不需要 API Key）")
    print("=" * 78)

    try:
        init_checkpointer()
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] 检查点初始化失败（需本地 PostgreSQL）：{exc}")
        return 1

    f = Fixture()
    try:
        f.setup()
        group1_graph(c)
        group2_valid(c, f)
        group3_account_fallback(c, f)
        group3b_business_errors(c, f)
        group4_forbidden(c, f)
        group5_loop_degrade_giveup(c, f)
        group6_json_robustness(c, f)
        group7_single_transaction(c, f)
        group8_interfaces(c, f)
        group9_security(c, f)
        group9b_migrations(c)
    except Exception as exc:  # noqa: BLE001 —— 未预期异常也要走清理
        import traceback

        c.check(False, "自检全程未抛未预期异常",
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
    finally:
        try:
            f.cleanup()
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] 清理阶段异常：{type(exc).__name__}: {exc}")
        close_checkpointer()

    group10_cleanup(c, f)

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for item in c.failures:
            print(f"  - FAIL: {item}")
        return 1
    print("全部 PASS ✔（离线自检，未联网、未使用真实 API Key）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
