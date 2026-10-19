"""第 23 步 · V2 增强（二）· 多版本创作 Agent【离线】自检（**不连外网、不需要真实 API Key**）。

用**脚本模型**（确定性返回 / 缺字段版本 / 违禁命中版本 / 脏 JSON）经 service 的
``model`` 注入口驱动整条「单选题 → N 个可横向对比的版本」链路，断言 10 组行为：

1. 图结构：含 load_context / generate_variants / review_variants / persist_variants /
   give_up 五节点，条件边（review → generate 有限回路 / review → persist / review →
   give_up）与 END 连接齐全；常量口径（N∈1~5、默认 3、角度池恰任务书六项）；
   解包 / 归一纯函数直测；
2. 合格路径：N 版逐列落库（title / body / tags、content_check、compliance_status=passed、
   status=draft、version=1、llm_meta.variant 批次信息、三个外键）、同批次同一
   variant_group_id（= run_id）、初始未采用；单事务把选题 approved→writing；
   agent_runs 回填（outputs / llm_raw / token / json_parse_attempts / 关联）；
3. 接口校验：n 越界 422、angles 数量 / 重复 / 空串 / 超长 422、缺 topic_id 422、
   不存在选题 404、draft 选题 400、无人设 400（以上全部**不发起模型调用**）；
   LLM 配置闸门（AGENT_LLM_ENABLED=false → AgentConfigError）；
4. N 边界与显式角度：n=1 / n=5 边界可用；显式角度注入提示词且逐版落库；
5. 空 title/body 不合格（rejected）：首轮不合格 → 有限回路（feedback 注入）→ 次轮
   合格；持续不合格 → 到上限落「部分合格」（rejected 计数 + 明细、run partial）；
6. 违禁命中 → 该版 blocked（原因随响应与 outputs 返回）、不阻断其他版（partial）、
   blocked 版本**不落库**；
7. 全失败：全 blocked / 全 rejected / 持续非 JSON → 零落库、选题仍 approved、
   run 记 failed（不落脏数据）；
8. 批次与采用：批次列表（排序 / 字段完整 / 无 llm_raw）、采用互斥单事务（可反复
   切换）、非批次笔记 400、不存在 404；
9. 接口与安全：OpenAPI 组件与实体序列化均无 llm_raw / 向量字段；路由文件无
   SQLAlchemy 语句、无 PUT/DELETE、AgentConfigError→400；库内无 sk-；本步新增
   代码不含平台域名 / 登录态字段；业务表仍 10 张（零迁移）；
10. 迁移：versions 恰 2 支、alembic current=0002 (head)、alembic check 零漂移；
    测试数据 finally 物理清理并复查残留为 0（双口径）。

依赖本地 docker 的 PostgreSQL（与 ``selftest_content_agent.py`` 同）；测试数据一律带
``[SELFTEST]`` 并在结束时物理清理。**全程不调用真实 LLM（脚本模型注入）。**

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\selftest_content_variants.py

全部通过退出码 0；任一项 FAIL 退出码 1。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.agent.checkpoint import close_checkpointer, init_checkpointer  # noqa: E402
from app.agent.content_variants.contracts import (  # noqa: E402
    ANGLE_MAX_LEN,
    DEFAULT_VARIANTS,
    MAX_VARIANTS,
    MIN_VARIANTS,
    OUTCOME_BLOCKED,
    OUTCOME_QUALIFIED,
    OUTCOME_REJECTED,
)
from app.agent.content_variants.graph import (  # noqa: E402
    NODE_GENERATE,
    NODE_GIVE_UP,
    NODE_LOAD_CONTEXT,
    NODE_PERSIST,
    NODE_REVIEW,
    build_content_variants_graph,
    normalize_variants,
    unwrap_variants,
)
from app.agent.content_variants.prompts import (  # noqa: E402
    ANGLE_POOL,
    CONTENT_VARIANTS_PROMPT_VERSION,
)
from app.agent.content_variants.service import (  # noqa: E402
    adopt_variant,
    generate_variants,
    list_topic_batches,
)
from app.agent.errors import AgentConfigError  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.errors import CODE_BAD_REQUEST, CODE_NOT_FOUND, AppError  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.models.enums import ComplianceStatus, NoteStatus, RunStatus, TopicDifficulty, TopicStatus  # noqa: E402
from app.models.persona import Persona  # noqa: E402
from app.models.topic import Topic  # noqa: E402
from app.schemas.content_variants import (  # noqa: E402
    AdoptVariantResponse,
    VariantsBatchesResponse,
    VariantsGenerateRequest,
    VariantsGenerateResponse,
)

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from pydantic import Field  # noqa: E402

SELFTEST_TAG = "[SELFTEST]"

#: 需要从子进程环境里剔除的代理变量（沙箱会注入，导致真实请求挂住）
_PROXY_KEYS = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}

#: 本步新增代码的扫描范围（安全断言用）
_SCAN_PATHS = (
    ("app", "agent", "content_variants"),
    ("app", "schemas", "content_variants.py"),
    ("app", "api", "routes", "content_variants.py"),
    ("app", "crud", "note.py"),
)

#: 平台域名 / 登录态字段标记（命中即 FAIL）
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
class VariantsScriptModel(BaseChatModel):
    """确定性假模型：按调用次序取响应；末项会被重复使用（模拟模型固执己见）。

    响应项可以是 ``str`` / ``dict`` / ``list``（自动转 JSON）/ ``BaseException``（抛出）。
    """

    responses: list[Any] = Field(default_factory=list)
    fallback: Any = None
    calls: int = 0
    usage_in: int = 11
    usage_out: int = 22
    prompts: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "content-variants-script"

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
            sequence = [self.fallback if self.fallback is not None else '[{"angle": "x", "title": "", "body": ""}]']
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
# 版本构造器
# ---------------------------------------------------------------------------
_CLEAN_BODY = "这是多版本自检正文，用于验证逐版归一、判定与落库链路。" * 10
_RISK_BODY = "这是合规自检正文，用于验证违禁命中拦截。" * 10 + "本产品包治百病，一吃就好。"


def mk_variant(
    angle: str,
    tag: str,
    *,
    title: str | None = None,
    body: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """一个合格版本：标题 / 正文 / 标签齐全（标签刻意含误带的 #，用于验证剥除）。"""
    return {
        "angle": angle,
        "title": f"{SELFTEST_TAG}多版本标题{tag}" if title is None else title,
        "body": _CLEAN_BODY if body is None else body,
        "tags": [f"#自检标签{tag}a", f"自检标签{tag}b", f"自检标签{tag}c"] if tags is None else tags,
    }


def mk_risk_variant(angle: str, tag: str) -> dict[str, Any]:
    """命中内置风险词（正文：包治百病）的版本。"""
    return mk_variant(angle, tag, body=_RISK_BODY)


def mk_empty_variant(angle: str, tag: str) -> dict[str, Any]:
    """缺少标题的版本（判 rejected）。"""
    return mk_variant(angle, tag, title="")


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
            account = Account(name=f"{SELFTEST_TAG}多版本账号")
            db.add(account)
            db.flush()
            persona = Persona(
                account_id=account.id,
                name=f"{SELFTEST_TAG}多版本人设",
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

            for index in range(13):
                topic = Topic(
                    # 索引 5 刻意不挂账号，用于验证「回退到 persona 的账号」
                    account_id=None if index == 5 else account.id,
                    # 索引 10 刻意不挂人设，用于验证「未关联人设不能创作」
                    persona_id=None if index == 10 else persona.id,
                    title=f"{SELFTEST_TAG}多版本选题{index}",
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
            # ⚠️ 按 id 集合删选题（索引 10 刻意不带 persona_id，按 persona 删会漏）
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
        with SessionLocal() as db:
            self.residue = {
                "notes": db.execute(
                    text("SELECT count(*) FROM notes WHERE account_id = :aid"),
                    {"aid": str(self.account_id)},
                ).scalar_one(),
                "topics": db.execute(
                    text("SELECT count(*) FROM topics WHERE id = ANY(:tids)"),
                    {"tids": [str(item) for item in self.topic_ids]},
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
                    {"p": f"{SELFTEST_TAG}多版本%"},
                ).scalar_one(),
                "notes_by_title": db.execute(
                    text("SELECT count(*) FROM notes WHERE title LIKE :p"),
                    {"p": f"{SELFTEST_TAG}多版本%"},
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
            text(
                f"SELECT {_NOTE_COLUMNS} FROM notes WHERE topic_id = :tid "
                "ORDER BY created_at ASC, id ASC"
            ),  # noqa: S608
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


def _latest_failed_run_messages(persona_id: Any, limit: int = 5) -> list[tuple[str, str | None]]:
    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT status, error_message FROM agent_runs WHERE persona_id = :pid "
                "ORDER BY created_at DESC LIMIT :n"
            ),
            {"pid": str(persona_id), "n": limit},
        ).all()
    return [(str(row[0]), row[1]) for row in rows]


# ---------------------------------------------------------------------------
# 组 1：图结构 + 常量 + 纯函数
# ---------------------------------------------------------------------------
def group1_graph(c: Checker) -> None:
    c.check(
        {NODE_LOAD_CONTEXT, NODE_GENERATE, NODE_REVIEW, NODE_PERSIST, NODE_GIVE_UP}
        == {"load_context", "generate_variants", "review_variants", "persist_variants", "give_up"},
        "五个节点名与任务书一致",
    )
    graph = build_content_variants_graph(model=VariantsScriptModel(), use_checkpointer=False)
    c.check(
        "Compiled" in type(graph).__name__,
        "build_content_variants_graph 返回已编译的 LangGraph 图",
        f"{type(graph).__name__}",
    )
    view = graph.get_graph()
    nodes = set(view.nodes.keys())
    c.check(
        {NODE_LOAD_CONTEXT, NODE_GENERATE, NODE_REVIEW, NODE_PERSIST, NODE_GIVE_UP}.issubset(nodes),
        "图含 load_context / generate_variants / review_variants / persist_variants / give_up",
        f"实际={sorted(nodes)}",
    )
    edges = {(edge.source, edge.target, bool(edge.conditional)) for edge in view.edges}
    c.check(
        (NODE_LOAD_CONTEXT, NODE_GENERATE, False) in edges
        and (NODE_GENERATE, NODE_REVIEW, False) in edges,
        "load_context → generate_variants → review_variants 为直连边",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_REVIEW, NODE_GENERATE, True) in edges,
        "review_variants 存在「回炉重生成」条件边（有限回路的载体）",
    )
    c.check(
        (NODE_REVIEW, NODE_PERSIST, True) in edges and (NODE_REVIEW, NODE_GIVE_UP, True) in edges,
        "review_variants 存在去 persist / give_up 的条件边",
    )
    c.check(
        (NODE_PERSIST, "__end__", False) in edges and (NODE_GIVE_UP, "__end__", False) in edges,
        "persist_variants / give_up 均连接 END",
    )
    c.check(
        settings.AGENT_JSON_MAX_ATTEMPTS >= 1,
        "回路有界（上限来自 AGENT_JSON_MAX_ATTEMPTS）",
        f"{settings.AGENT_JSON_MAX_ATTEMPTS}",
    )

    # ---- 常量口径 ----
    c.check(
        (MIN_VARIANTS, MAX_VARIANTS, DEFAULT_VARIANTS) == (1, 5, 3),
        "版本数常量 = 1~5、默认 3",
        f"{MIN_VARIANTS}/{MAX_VARIANTS}/{DEFAULT_VARIANTS}",
    )
    c.check(
        ANGLE_POOL == ("实用干货", "情绪共鸣", "反差观点", "亲身经历", "清单盘点", "避坑提醒"),
        "内置角度池恰为任务书六项",
        f"{ANGLE_POOL}",
    )
    c.check(
        CONTENT_VARIANTS_PROMPT_VERSION == settings.CONTENT_VARIANTS_PROMPT_VERSION,
        "提示词版本取自配置（不硬编码）",
        f"{CONTENT_VARIANTS_PROMPT_VERSION}",
    )

    # ---- 解包 / 归一纯函数直测 ----
    c.check(unwrap_variants([{"a": 1}]) == [{"a": 1}], "unwrap：顶层数组原样返回")
    c.check(unwrap_variants({"variants": [1]}) == [1], "unwrap：variants 包装键可解")
    c.check(unwrap_variants({"items": [1, 2]}) == [1, 2], "unwrap：items 包装键可解")
    c.check(unwrap_variants({"data": {"x": []}}) is None, "unwrap：只解一层（嵌套对象不解）")
    c.check(unwrap_variants("文本") is None, "unwrap：非数组 / 对象 → None")

    drafts, extra = normalize_variants([mk_variant("A", "U1"), mk_variant("B", "U2"), mk_variant("C", "U3")], 2)
    c.check(
        len(drafts) == 2 and extra == 1,
        "normalize：仅考虑前 n 条，超出计数为 extra_ignored",
        f"drafts={len(drafts)} extra={extra}",
    )
    c.check(
        drafts[0]["tags"] == ["自检标签U1a", "自检标签U1b", "自检标签U1c"],
        "normalize：标签剥 # （复用 ContentDraft 契约）",
        f"{drafts[0]['tags']}",
    )
    drafts, _ = normalize_variants([["不是对象"], mk_variant("B", "U2")], 2)
    c.check(
        drafts[0]["type_errors"] == ["数组元素不是 JSON 对象"] and not drafts[1]["type_errors"],
        "normalize：单条类型非法只记该条（不丢整批）",
        f"{drafts[0]}",
    )
    drafts, _ = normalize_variants(None, 3)
    c.check(drafts == [], "normalize：None → 空列表（由 review 判不合格）")


# ---------------------------------------------------------------------------
# 组 2：合格路径（N 版逐列落库 + 单事务过渡 + 审计回填）
# ---------------------------------------------------------------------------
def group2_clean(c: Checker, f: Fixture) -> None:
    topic = f.topic(0)
    model = VariantsScriptModel(
        responses=[
            [
                mk_variant("实用干货", "S2A"),
                mk_variant("情绪共鸣", "S2B"),
                mk_variant("清单盘点", "S2C"),
            ]
        ]
    )
    summary = generate_variants(topic_id=topic, n=3, model=model)

    c.check(
        summary["status"] == RunStatus.SUCCESS.value and len(summary["note_ids"]) == 3,
        "合格路径：status=success 且落库 3 条",
        f"{summary['status']}",
    )
    c.check(model.calls == 1, "全合格 → 一轮生成即落库（模型只调 1 次）", f"{model.calls}")
    c.check(
        summary["variant_group_id"] == summary["run_id"],
        "批次号 = run_id（variant_group_id）",
        f"{summary['variant_group_id']}",
    )

    prompt0 = model.prompts[0] if model.prompts else ""
    c.check(
        "内置角度池" in prompt0 and "实用干货" in prompt0 and "不得重复" in prompt0,
        "auto 模式提示词含角度池与「角度不得重复」约束",
    )
    c.check(
        "数组" in prompt0 and "angle" in prompt0 and "\\n" in prompt0,
        "提示词要求 JSON 数组输出且含 angle / 换行转义约定",
    )

    statuses = [item["status"] for item in summary["items"]]
    angles_seen = [item["angle"] for item in summary["items"]]
    c.check(statuses == ["qualified", "qualified", "qualified"], "逐版判定均为 qualified", f"{statuses}")
    c.check(
        angles_seen == ["实用干货", "情绪共鸣", "清单盘点"] and len(set(angles_seen)) == 3,
        "响应角度序列与模型输出一致且互不重复",
        f"{angles_seen}",
    )
    c.check(
        [item["note_id"] for item in summary["items"]] == summary["note_ids"],
        "逐版明细的 note_id 与落库顺序一致",
    )

    rows = _notes_for_topic(topic)
    c.check(len(rows) == 3, "notes 实际写入 3 行（同批次）", f"{len(rows)}")
    # ⚠️ 同事务写入的 created_at 相同 ⇒ 行序退化按 id（随机）；断言按 variant.index 归一取「首版」
    by_index = {
        int((item.get("llm_meta") or {}).get("variant", {}).get("index", -1)): item for item in rows
    }
    row = by_index.get(0) or {}
    c.check(
        row.get("title") == f"{SELFTEST_TAG}多版本标题S2A" and row.get("body") == _CLEAN_BODY,
        "首版 title / body 逐列写入（与模型产出一致）",
        f"{row.get('title')}",
    )
    c.check(
        row.get("tags") == ["自检标签S2Aa", "自检标签S2Ab", "自检标签S2Ac"],
        "tags 写入：剥除误带的 # 且顺序保留",
        f"{row.get('tags')}",
    )
    check = row.get("content_check") or {}
    c.check(
        check.get("status") == ComplianceStatus.PASSED.value and check.get("hits") == []
        and check.get("checked_fields") == ["title", "body", "tags"],
        "content_check.status=passed / hits 空 / checked_fields 齐全",
        f"{check}",
    )
    c.check(
        row.get("compliance_status") == ComplianceStatus.PASSED.value
        and row.get("status") == NoteStatus.DRAFT.value
        and row.get("version") == 1,
        "compliance=passed / status=draft（不自动 ready）/ version=1",
        f"{row.get('compliance_status')}/{row.get('status')}",
    )
    c.check(
        row.get("publish_url") is None and row.get("xhs_note_id") is None
        and row.get("published_at") is None,
        "publish_url / xhs_note_id / published_at 全部留空（不自动发布）",
    )
    meta = row.get("llm_meta") or {}
    variant = meta.get("variant") or {}
    c.check(
        meta.get("prompt_version") == CONTENT_VARIANTS_PROMPT_VERSION
        and meta.get("json_parse_attempts") == 1
        and meta.get("model") == settings.LLM_MODEL,
        "llm_meta 含 prompt_version / json_parse_attempts=1 / model",
        f"{meta}",
    )
    c.check(
        variant.get("angle") == "实用干货" and variant.get("index") == 0
        and variant.get("adopted") is False,
        "llm_meta.variant：角度 / 序号 / 初始未采用（零迁移映射）",
        f"{variant}",
    )
    groups = {(item.get("llm_meta") or {}).get("variant", {}).get("group_id") for item in rows}
    indexes = sorted((item.get("llm_meta") or {}).get("variant", {}).get("index") for item in rows)
    c.check(
        groups == {summary["run_id"]} and indexes == [0, 1, 2],
        "同批次三条同一 group_id、index 0/1/2",
        f"{groups} / {indexes}",
    )
    c.check(
        all(
            str(item.get("account_id")) == str(f.account_id)
            and str(item.get("persona_id")) == str(f.persona_id)
            and str(item.get("topic_id")) == str(topic)
            for item in rows
        ),
        "account_id / persona_id / topic_id 三外键均按上下文回填",
    )
    c.check(
        _topic_status(topic) == TopicStatus.WRITING.value,
        "persist 同一事务把选题 approved→writing（只过渡一次）",
        f"{_topic_status(topic)}",
    )

    run = _fetch_run(summary["run_id"])
    outputs = run.get("outputs") or {}
    c.check(run.get("run_type") == "content_variants", "agent_runs.run_type=content_variants", f"{run.get('run_type')}")
    c.check(run.get("status") == RunStatus.SUCCESS.value, "agent_runs.status=success", f"{run.get('status')}")
    c.check(
        outputs.get("variant_group_id") == summary["run_id"]
        and outputs.get("requested") == 3
        and outputs.get("generated") == 3
        and outputs.get("persisted") == 3
        and outputs.get("blocked") == 0
        and outputs.get("rejected") == 0
        and outputs.get("extra_ignored") == 0
        and len(outputs.get("note_ids") or []) == 3
        and outputs.get("angles") == ["实用干货", "情绪共鸣", "清单盘点"],
        "agent_runs.outputs 已回填业务明细（批次 / 计数 / note_ids / 角度）",
        f"{outputs}",
    )
    c.check(
        run.get("prompt_tokens") == 11 and run.get("completion_tokens") == 22
        and run.get("total_tokens") == 33,
        "agent_runs token 已回填（11/22/33）",
        f"{run.get('prompt_tokens')}/{run.get('completion_tokens')}/{run.get('total_tokens')}",
    )
    c.check(run.get("json_parse_attempts") == 1, "agent_runs.json_parse_attempts == 1")
    c.check(
        run.get("llm_raw") and SELFTEST_TAG in str(run["llm_raw"]) and "sk-" not in str(run["llm_raw"]),
        "agent_runs.llm_raw 已回填模型原始输出且不含密钥",
    )
    c.check(
        str(run.get("note_id")) == str(summary["note_ids"][0])
        and str(run.get("topic_id")) == str(topic),
        "agent_runs.note_id（首条）/ topic_id 已关联",
        f"{run.get('note_id')}/{run.get('topic_id')}",
    )
    c.check(run.get("trigger") == "manual", "agent_runs.trigger=manual")


# ---------------------------------------------------------------------------
# 组 3：接口校验与配置闸门（全部为「模型调用前」的失败路径）
# ---------------------------------------------------------------------------
def _count_runs(persona_id: Any) -> int:
    with SessionLocal() as db:
        return db.execute(
            text("SELECT count(*) FROM agent_runs WHERE persona_id = :pid"),
            {"pid": str(persona_id)},
        ).scalar_one()


def _count_notes(account_id: Any) -> int:
    with SessionLocal() as db:
        return db.execute(
            text("SELECT count(*) FROM notes WHERE account_id = :aid"),
            {"aid": str(account_id)},
        ).scalar_one()


def group3_validation(c: Checker, f: Fixture) -> None:
    # ---- schema 直测 ----
    req = VariantsGenerateRequest.model_validate({"topic_id": str(f.topic(0))})
    c.check(req.n == 3 and req.angles is None, "schema：n 默认 3、angles 缺省 None", f"n={req.n}")
    req2 = VariantsGenerateRequest.model_validate(
        {"topic_id": str(f.topic(0)), "n": 3, "angles": [" 实用干货 ", "反差观点", "避坑提醒"]}
    )
    c.check(
        req2.angles == ["实用干货", "反差观点", "避坑提醒"],
        "schema：angles 归一（strip）且保留顺序",
        f"{req2.angles}",
    )
    req3 = VariantsGenerateRequest.model_validate({"topic_id": str(f.topic(0)), "n": 2, "angles": []})
    c.check(req3.angles is None, "schema：angles=[] 视同留空（自动）")

    runs_before = _count_runs(f.persona_id)
    notes_before = _count_notes(f.account_id)
    client = TestClient(fastapi_app, raise_server_exceptions=False)

    no_topic = {"topic_id": str(uuid.uuid4()), "n": 3}
    r = client.post("/api/v1/content-variants/generate", json=no_topic)
    c.check(
        r.status_code == 404 and r.json().get("code") == CODE_NOT_FOUND,
        "不存在选题 → 404 not_found",
        f"{r.status_code} {r.text[:120]}",
    )
    r = client.post("/api/v1/content-variants/generate", json={"topic_id": str(f.topic(9)), "n": 3})
    c.check(
        r.status_code == 400 and r.json().get("code") == CODE_BAD_REQUEST,
        "非 approved 选题 → 400 bad_request",
        f"{r.status_code} {r.text[:160]}",
    )
    r = client.post("/api/v1/content-variants/generate", json={"topic_id": str(f.topic(10)), "n": 3})
    c.check(
        r.status_code == 400 and r.json().get("code") == CODE_BAD_REQUEST,
        "选题未关联人设 → 400 bad_request",
        f"{r.status_code} {r.text[:160]}",
    )
    for bad_n in (0, 6, -1, 99):
        r = client.post(
            "/api/v1/content-variants/generate",
            json={"topic_id": str(f.topic(0)), "n": bad_n},
        )
        c.check(r.status_code == 422, f"n={bad_n} 越界 → 422", f"{r.status_code}")
    r = client.post("/api/v1/content-variants/generate", json={"n": 3})
    c.check(r.status_code == 422, "缺 topic_id → 422", f"{r.status_code}")
    bad_angles_cases = {
        "数量与 n 不一致": ["实用干货", "反差观点"],
        "角度重复": ["实用干货", "实用干货", "避坑提醒"],
        "空字符串": ["实用干货", "", "避坑提醒"],
        "单条超长": ["x" * (ANGLE_MAX_LEN + 1), "反差观点", "避坑提醒"],
    }
    for label, angles_value in bad_angles_cases.items():
        r = client.post(
            "/api/v1/content-variants/generate",
            json={"topic_id": str(f.topic(0)), "n": 3, "angles": angles_value},
        )
        c.check(r.status_code == 422, f"angles {label} → 422", f"{r.status_code} {r.text[:120]}")

    c.check(
        _count_runs(f.persona_id) == runs_before,
        "全部校验失败路径未产生审计行（未发起模型调用）",
        f"{runs_before} → {_count_runs(f.persona_id)}",
    )
    c.check(
        _count_notes(f.account_id) == notes_before,
        "全部校验失败路径未写入 notes",
        f"{notes_before} → {_count_notes(f.account_id)}",
    )

    # ---- LLM 配置闸门：关掉开关后工厂直接抛 AgentConfigError（未发生网络请求）----
    from app.agent.llm.factory import get_chat_model  # noqa: PLC0415 —— 局部导入避免顶部未用告警

    original = settings.AGENT_LLM_ENABLED
    gate: str | None = None
    assign_error: str | None = None
    try:
        settings.AGENT_LLM_ENABLED = False
    except Exception as exc:  # noqa: BLE001
        assign_error = f"{type(exc).__name__}: {exc}"
    if assign_error is None:
        try:
            get_chat_model(temperature=0.7, want_json=True)
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
    route_src = (BACKEND_DIR / "app" / "api" / "routes" / "content_variants.py").read_text(encoding="utf-8")
    c.check(
        "except AgentConfigError" in route_src and "bad_request(" in route_src,
        "content_variants 路由把 AgentConfigError 映射为 400 bad_request",
    )


# ---------------------------------------------------------------------------
# 组 4：N 边界（n=1 / n=5）与显式角度
# ---------------------------------------------------------------------------
def group4_angles_bounds(c: Checker, f: Fixture) -> None:
    # 4a：显式角度（n=3）
    topic = f.topic(1)
    angles = ["实用干货", "反差观点", "避坑提醒"]
    model = VariantsScriptModel(
        responses=[
            [
                mk_variant("实用干货", "S4A"),
                mk_variant("反差观点", "S4B"),
                mk_variant("避坑提醒", "S4C"),
            ]
        ]
    )
    summary = generate_variants(topic_id=topic, n=3, angles=angles, model=model)
    c.check(
        summary["status"] == RunStatus.SUCCESS.value and summary["persisted"] == 3,
        "显式角度：status=success 且落库 3 条",
        f"{summary['status']}",
    )
    prompt0 = model.prompts[0] if model.prompts else ""
    c.check(
        "严格使用" in prompt0 and "实用干货、反差观点、避坑提醒" in prompt0,
        "显式角度注入提示词（严格使用 + 全部列出）",
    )
    rows = _notes_for_topic(topic)
    rows_sorted = sorted(
        rows,
        key=lambda item: int((item.get("llm_meta") or {}).get("variant", {}).get("index") or 0),
    )
    c.check(
        [
            (item.get("llm_meta") or {}).get("variant", {}).get("angle") for item in rows_sorted
        ]
        == angles,
        "显式角度逐版落库（按 index 归一后顺序一致）",
        f"{[(item.get('llm_meta') or {}).get('variant', {}).get('angle') for item in rows_sorted]}",
    )
    run = _fetch_run(summary["run_id"])
    c.check(
        (run.get("outputs") or {}).get("angles") == angles,
        "agent_runs.outputs.angles 记录显式角度",
        f"{(run.get('outputs') or {}).get('angles')}",
    )

    # 4b：n=1 边界（单版本）
    topic1 = f.topic(2)
    model1 = VariantsScriptModel(responses=[[mk_variant("实用干货", "S4D")]])
    summary1 = generate_variants(topic_id=topic1, n=1, model=model1)
    c.check(
        summary1["status"] == RunStatus.SUCCESS.value and summary1["persisted"] == 1
        and len(_notes_for_topic(topic1)) == 1,
        "n=1 边界（单版本）可用",
        f"{summary1['status']}",
    )

    # 4c：n=5 边界（五版本）
    topic5 = f.topic(3)
    five_tags = ["S4E0", "S4E1", "S4E2", "S4E3", "S4E4"]
    five_angles = ["实用干货", "情绪共鸣", "反差观点", "亲身经历", "清单盘点"]
    model5 = VariantsScriptModel(
        responses=[[mk_variant(angle, tag) for angle, tag in zip(five_angles, five_tags, strict=True)]]
    )
    summary5 = generate_variants(topic_id=topic5, n=5, model=model5)
    rows5 = _notes_for_topic(topic5)
    c.check(
        summary5["status"] == RunStatus.SUCCESS.value and summary5["persisted"] == 5 and len(rows5) == 5,
        "n=5 边界可用（五版全部落库）",
        f"{summary5['status']}/{len(rows5)}",
    )
    c.check(
        len({item["angle"] for item in summary5["items"]}) == 5,
        "5 版角度互不重复",
        f"{[item['angle'] for item in summary5['items']]}",
    )
    c.check(
        sorted((row.get("llm_meta") or {}).get("variant", {}).get("index") for row in rows5)
        == [0, 1, 2, 3, 4],
        "n=5 批次 index 0~4 连续",
    )


# ---------------------------------------------------------------------------
# 组 5：空 title/body 不合格（rejected）—— 有限回路 / 部分合格 / 回退账号
# ---------------------------------------------------------------------------
def group5_reject_loop(c: Checker, f: Fixture) -> None:
    # 5a：首轮一版缺标题 → 回炉（feedback 注入）→ 次轮合格落库
    topic = f.topic(4)
    model = VariantsScriptModel(
        responses=[
            [mk_variant("实用干货", "S5A"), mk_variant("情绪共鸣", "S5A2"), mk_empty_variant("避坑提醒", "S5A3")],
            [mk_variant("实用干货", "S5B"), mk_variant("情绪共鸣", "S5B2"), mk_variant("避坑提醒", "S5B3")],
        ]
    )
    summary = generate_variants(topic_id=topic, n=3, model=model)
    rows = _notes_for_topic(topic)
    c.check(
        summary["status"] == RunStatus.SUCCESS.value and len(rows) == 3 and model.calls == 2,
        "首轮缺标题 → 条件边回炉 → 次轮全合格并落库（模型调 2 次）",
        f"{summary['status']} calls={model.calls} rows={len(rows)}",
    )
    c.check(
        len(model.prompts) >= 2 and "上一轮" in model.prompts[1] and "缺少标题" in model.prompts[1],
        "回炉时 feedback 确实注入提示词（含不合格原因）",
    )
    c.check(
        {row.get("title") for row in rows}
        == {f"{SELFTEST_TAG}多版本标题S5B", f"{SELFTEST_TAG}多版本标题S5B2", f"{SELFTEST_TAG}多版本标题S5B3"},
        "落库的是次轮（合格轮）版本",
    )

    # 5b：持续一版缺标题 → 到上限，落「部分合格」（persisted=2 / rejected=1 / run partial）
    topic_b = f.topic(6)
    model_b = VariantsScriptModel(
        responses=[
            [mk_variant("实用干货", "S5C"), mk_variant("情绪共鸣", "S5C2"), mk_empty_variant("避坑提醒", "S5C3")]
        ]
    )
    summary_b = generate_variants(topic_id=topic_b, n=3, model=model_b)
    rows_b = _notes_for_topic(topic_b)
    c.check(
        model_b.calls == settings.AGENT_JSON_MAX_ATTEMPTS,
        f"回路有界：模型恰被调用 {settings.AGENT_JSON_MAX_ATTEMPTS} 次后停止回炉",
        f"{model_b.calls}",
    )
    c.check(
        summary_b["status"] == RunStatus.PARTIAL.value
        and summary_b["persisted"] == 2
        and summary_b["blocked"] == 0
        and summary_b["rejected"] == 1,
        "持续不合格 → 部分合格（persisted=2 / rejected=1 / partial）",
        f"{summary_b['status']} p={summary_b['persisted']} r={summary_b['rejected']}",
    )
    rejected_items = [item for item in summary_b["items"] if item["status"] == "rejected"]
    c.check(
        len(rejected_items) == 1 and "缺少标题" in str(rejected_items[0]["reason"])
        and rejected_items[0]["note_id"] is None,
        "rejected 明细带原因且无 note_id（不落库）",
        f"{rejected_items}",
    )
    c.check(len(rows_b) == 2, "rejected 版本未写入 notes（仅 2 行）", f"{len(rows_b)}")
    run_b = _fetch_run(summary_b["run_id"])
    c.check(run_b.get("status") == RunStatus.PARTIAL.value, "部分合格在 agent_runs 记 partial", f"{run_b.get('status')}")
    c.check(
        (run_b.get("outputs") or {}).get("rejected") == 1
        and len((run_b.get("outputs") or {}).get("rejected_details") or []) == 1,
        "agent_runs.outputs 记录 rejected 计数与明细",
        f"{(run_b.get('outputs') or {}).get('rejected_details')}",
    )

    # 5c：归属账号回退（topic 未关联账号 → 用 persona 的账号）
    topic_c = f.topic(5)
    model_c = VariantsScriptModel(responses=[[mk_variant("实用干货", "S5D")]])
    summary_c = generate_variants(topic_id=topic_c, n=1, model=model_c)
    rows_c = _notes_for_topic(topic_c)
    c.check(
        summary_c["status"] == RunStatus.SUCCESS.value
        and len(rows_c) == 1
        and str(rows_c[0].get("account_id")) == str(f.account_id),
        "topic.account_id 为空时按 persona.account_id 回填归属账号",
        f"{summary_c['status']} / {rows_c[0].get('account_id') if rows_c else None}",
    )


# ---------------------------------------------------------------------------
# 组 6：违禁命中 → 该版 blocked（不阻断其他版、不落库）
# ---------------------------------------------------------------------------
def group6_blocked(c: Checker, f: Fixture) -> None:
    topic = f.topic(7)
    model = VariantsScriptModel(
        responses=[
            [mk_variant("实用干货", "S6A"), mk_risk_variant("反差观点", "S6B"), mk_variant("避坑提醒", "S6C")]
        ]
    )
    summary = generate_variants(topic_id=topic, n=3, model=model)

    c.check(model.calls == 1, "违禁命中**不**触发回炉（模型仍只调 1 次）", f"{model.calls}")
    c.check(
        summary["status"] == RunStatus.PARTIAL.value
        and summary["persisted"] == 2
        and summary["blocked"] == 1
        and summary["rejected"] == 0,
        "一版命中 → 部分合格（persisted=2 / blocked=1），不阻断其他版",
        f"{summary['status']} p={summary['persisted']} b={summary['blocked']}",
    )
    blocked_items = [item for item in summary["items"] if item["status"] == "blocked"]
    c.check(
        len(blocked_items) == 1
        and "包治百病" in str(blocked_items[0]["reason"])
        and bool(blocked_items[0]["hits"])
        and blocked_items[0]["note_id"] is None,
        "blocked 版本带命中原因 / hits 明细且无 note_id",
        f"{blocked_items}",
    )
    c.check(
        [item["status"] for item in summary["items"]] == ["qualified", "blocked", "qualified"],
        "其余版本不受影响（逐版独立判定）",
        f"{[item['status'] for item in summary['items']]}",
    )

    rows = _notes_for_topic(topic)
    c.check(len(rows) == 2, "blocked 版本未写入 notes（仅 2 行）", f"{len(rows)}")
    titles = {row.get("title") for row in rows}
    c.check(
        f"{SELFTEST_TAG}多版本标题S6B" not in titles
        and all("包治百病" not in str(row.get("body") or "") for row in rows),
        "blocked 版本的标题 / 正文均未落库（不落脏数据）",
        f"{titles}",
    )
    c.check(
        _topic_status(topic) == TopicStatus.WRITING.value,
        "有 2 版合格落库 → 选题照常 approved→writing（一次过渡）",
        f"{_topic_status(topic)}",
    )
    run = _fetch_run(summary["run_id"])
    outputs = run.get("outputs") or {}
    c.check(run.get("status") == RunStatus.PARTIAL.value, "agent_runs.status=partial", f"{run.get('status')}")
    c.check(
        outputs.get("blocked") == 1
        and outputs.get("persisted") == 2
        and len(outputs.get("blocked_details") or []) == 1
        and "包治百病" in str(outputs.get("blocked_details")),
        "agent_runs.outputs 记录 blocked 计数与命中明细",
        f"{outputs.get('blocked_details')}",
    )


# ---------------------------------------------------------------------------
# 组 7：全失败（全 blocked / 全 rejected / 持续非 JSON）—— 零落库、选题不动
# ---------------------------------------------------------------------------
def group7_all_failed(c: Checker, f: Fixture) -> None:
    # 7a：全 blocked → failed、零落库、选题仍 approved
    topic_a = f.topic(8)
    model_a = VariantsScriptModel(
        responses=[[mk_risk_variant("实用干货", "S7A"), mk_risk_variant("反差观点", "S7B")]]
    )
    summary_a = generate_variants(topic_id=topic_a, n=2, model=model_a)
    c.check(
        summary_a["status"] == RunStatus.FAILED.value
        and summary_a["persisted"] == 0
        and summary_a["blocked"] == 2,
        "全 blocked → status=failed / persisted=0 / blocked=2",
        f"{summary_a['status']} p={summary_a['persisted']} b={summary_a['blocked']}",
    )
    c.check(len(_notes_for_topic(topic_a)) == 0, "全 blocked 零落库", "")
    c.check(
        _topic_status(topic_a) == TopicStatus.APPROVED.value,
        "零落库时选题保持 approved（不推进 writing）",
        f"{_topic_status(topic_a)}",
    )
    run_a = _fetch_run(summary_a["run_id"])
    c.check(
        run_a.get("status") == RunStatus.FAILED.value,
        "全 blocked 的 run 在 agent_runs 记 failed（全失败正确落库）",
        f"{run_a.get('status')}",
    )
    c.check(
        (run_a.get("outputs") or {}).get("persisted") == 0
        and (run_a.get("outputs") or {}).get("blocked") == 2,
        "agent_runs.outputs 如实记录全 blocked 计数",
        f"{(run_a.get('outputs') or {}).get('persisted')}",
    )

    # 7b：全 rejected（持续缺标题）→ give_up 抛错、run failed、无 notes 半成品
    topic_b = f.topic(12)
    model_b = VariantsScriptModel(responses=[[mk_empty_variant("实用干货", "S7C")]])
    summary_b = generate_variants(topic_id=topic_b, n=1, model=model_b)
    c.check(
        model_b.calls == settings.AGENT_JSON_MAX_ATTEMPTS,
        f"全 rejected：回路有界（模型恰被调用 {settings.AGENT_JSON_MAX_ATTEMPTS} 次）",
        f"{model_b.calls}",
    )
    c.check(
        summary_b["status"] == RunStatus.FAILED.value
        and summary_b["run_id"] is None
        and "未产出任何合格版本" in str(summary_b["error_message"]),
        "全 rejected → give_up（失败汇总含可读原因）",
        f"{summary_b['error_message']}",
    )
    c.check(len(_notes_for_topic(topic_b)) == 0, "全 rejected 零落库（无半成品）", "")
    c.check(
        _topic_status(topic_b) == TopicStatus.APPROVED.value,
        "全 rejected 时选题仍 approved",
        f"{_topic_status(topic_b)}",
    )
    latest_b = _latest_failed_run_messages(f.persona_id, limit=1)
    c.check(
        bool(latest_b) and latest_b[0][0] == RunStatus.FAILED.value,
        "该次运行在 agent_runs 记 failed",
        f"{latest_b}",
    )

    # 7c：持续非 JSON → AgentJSONError（分层重试后）→ run failed、零落库
    topic_c = f.topic(11)
    model_c = VariantsScriptModel(responses=["这不是 JSON，永远不是。"])
    summary_c = generate_variants(topic_id=topic_c, n=2, model=model_c)
    expected_calls = settings.AGENT_JSON_MAX_ATTEMPTS * max(1, settings.AGENT_NODE_MAX_RETRIES + 1)
    c.check(
        model_c.calls == expected_calls,
        f"持续非 JSON：分层重试恰为 {expected_calls} 次（JSON 层 × 节点层）",
        f"{model_c.calls}",
    )
    c.check(
        summary_c["status"] == RunStatus.FAILED.value
        and "JSON" in str(summary_c["error_message"]),
        "持续非 JSON → failed 且 error_message 说明 JSON 问题",
        f"{summary_c['error_message']}",
    )
    c.check(len(_notes_for_topic(topic_c)) == 0, "JSON 全失败零落库", "")
    c.check(
        _topic_status(topic_c) == TopicStatus.APPROVED.value,
        "JSON 全失败时选题仍 approved",
        f"{_topic_status(topic_c)}",
    )
    latest_c = _latest_failed_run_messages(f.persona_id, limit=1)
    c.check(
        bool(latest_c) and latest_c[0][0] == RunStatus.FAILED.value
        and bool(latest_c[0][1]),
        "JSON 全失败的 run 记 failed 且带 error_message",
        f"{latest_c}",
    )


# ---------------------------------------------------------------------------
# 组 8：批次列表 / 采用互斥（单事务）/ 非批次与不存在
# ---------------------------------------------------------------------------
def group8_batches_adopt(c: Checker, f: Fixture) -> None:
    topic = f.topic(0)  # group2 的批次（3 版，全合格）
    client = TestClient(fastapi_app, raise_server_exceptions=False)

    # ---- 批次列表（service 层）----
    batches = list_topic_batches(topic)
    c.check(
        len(batches["batches"]) == 1
        and str(batches["topic_id"]) == str(topic),
        "批次列表：恰 1 个批次且 topic_id 对应",
        f"{len(batches['batches'])}",
    )
    batch = batches["batches"][0]
    c.check(
        len(batch["variants"]) == 3
        and [item["angle"] for item in batch["variants"]] == ["实用干货", "情绪共鸣", "清单盘点"],
        "批次内 3 版按 index 升序且角度正确",
    )
    c.check(
        all(item["adopted"] is False for item in batch["variants"]),
        "采用前全部 adopted=False",
    )
    expected_keys = {"note_id", "angle", "index", "title", "body", "tags", "compliance_status", "adopted", "created_at"}
    c.check(
        all(set(item.keys()) == expected_keys for item in batch["variants"]),
        "版本出参字段恰为约定集合（含 angle / 合规 / 采用态）",
        f"{set(batch['variants'][0].keys()) ^ expected_keys}",
    )
    serialized = json.dumps(batches, ensure_ascii=False, default=str)
    c.check(
        "llm_raw" not in serialized and "embedding" not in serialized,
        "批次列表序列化不含 llm_raw / 向量字段",
    )

    # ---- 采用互斥（service 层：单事务、可反复切换）----
    first = batch["variants"][0]["note_id"]
    outcome = adopt_variant(uuid.UUID(str(first)))
    c.check(
        str(outcome["adopted_note_id"]) == str(first)
        and [item["adopted"] for item in outcome["variants"]] == [True, False, False],
        "采用第 1 版：该版置采用、其余置未采用（单事务）",
        f"{[item['adopted'] for item in outcome['variants']]}",
    )
    third = batch["variants"][2]["note_id"]
    outcome3 = adopt_variant(uuid.UUID(str(third)))
    c.check(
        [item["adopted"] for item in outcome3["variants"]] == [False, False, True],
        "切换到第 3 版：互斥翻转（可反复切换）",
        f"{[item['adopted'] for item in outcome3['variants']]}",
    )
    rows = _notes_for_topic(topic)
    flags = {str(row["id"]): (row["llm_meta"] or {}).get("variant", {}).get("adopted") for row in rows}
    c.check(
        flags.get(str(third)) is True and sum(1 for v in flags.values() if v) == 1,
        "库内复查：恰一版 adopted=True（互斥口径落库）",
        f"{flags}",
    )
    c.check(
        all(row["status"] == NoteStatus.DRAFT.value for row in rows),
        "采用不改笔记状态（仍 draft，进入创作台继续人工编辑）",
        f"{[row['status'] for row in rows]}",
    )

    # ---- HTTP：GET 批次 / PATCH 采用 ----
    r = client.get(f"/api/v1/content-variants?topic_id={topic}")
    c.check(
        r.status_code == 200 and len(r.json().get("batches", [])) == 1,
        "GET /content-variants?topic_id= → 200 且批次在册",
        f"{r.status_code}",
    )
    c.check("llm_raw" not in r.text, "GET 响应体不含 llm_raw")
    r = client.get("/api/v1/content-variants")
    c.check(r.status_code == 422, "GET 缺 topic_id → 422", f"{r.status_code}")
    r = client.get("/api/v1/content-variants?topic_id=not-a-uuid")
    c.check(r.status_code == 422, "GET topic_id 非法 → 422", f"{r.status_code}")
    r = client.get(f"/api/v1/content-variants?topic_id={uuid.uuid4()}")
    c.check(
        r.status_code == 404 and r.json().get("code") == CODE_NOT_FOUND,
        "GET 不存在的选题 → 404",
        f"{r.status_code}",
    )

    second = batch["variants"][1]["note_id"]
    r = client.patch(f"/api/v1/content-variants/{second}/adopt")
    c.check(
        r.status_code == 200
        and r.json()["adopted_note_id"] == str(second)
        and [item["adopted"] for item in r.json()["variants"]] == [False, True, False],
        "PATCH 采用（HTTP）→ 200 且互斥翻转",
        f"{r.status_code} {r.text[:160]}",
    )
    r = client.patch(f"/api/v1/content-variants/{uuid.uuid4()}/adopt")
    c.check(
        r.status_code == 404 and r.json().get("code") == CODE_NOT_FOUND,
        "采用不存在的笔记 → 404",
        f"{r.status_code}",
    )

    # 非批次笔记（无 llm_meta.variant）→ 400
    plain_id: Any = None
    with SessionLocal() as db:
        plain_id = db.execute(
            text(
                "INSERT INTO notes (account_id, persona_id, topic_id, title, body, tags, "
                "compliance_status, status, version, llm_meta) "
                "VALUES (:a, :p, :t, :title, :body, NULL, 'pending', 'draft', 1, NULL) RETURNING id"
            ),
            {
                "a": str(f.account_id),
                "p": str(f.persona_id),
                "t": str(topic),
                "title": f"{SELFTEST_TAG}多版本非批次笔记",
                "body": "非批次笔记（用于负例）。",
            },
        ).scalar_one()
        db.commit()
    r = client.patch(f"/api/v1/content-variants/{plain_id}/adopt")
    c.check(
        r.status_code == 400 and r.json().get("code") == CODE_BAD_REQUEST,
        "采用「非批次」笔记 → 400（不属于多版本批次）",
        f"{r.status_code} {r.text[:160]}",
    )


# ---------------------------------------------------------------------------
# 组 9：接口与安全（OpenAPI / 路由静态 / 库内密钥 / 平台标记 / 表数）
# ---------------------------------------------------------------------------
def group9_interfaces_security(c: Checker, f: Fixture) -> None:
    spec = fastapi_app.openapi()
    # ⚠️ 口径与第 12 步一致：按「字段名」扫描（文档描述里出现 llm_raw 字样是正常的）
    components = spec.get("components", {}).get("schemas", {})
    leaked = [
        name
        for name, schema in components.items()
        if "llm_raw" in (schema.get("properties") or {})
    ]
    c.check(not leaked, "全 OpenAPI 组件中**没有任何**响应模型暴露 llm_raw 字段", f"{leaked}")

    spec_paths = spec.get("paths") or {}
    expected_paths = {
        "/api/v1/content-variants/generate",
        "/api/v1/content-variants",
        "/api/v1/content-variants/{note_id}/adopt",
    }
    c.check(expected_paths <= set(spec_paths), "新路由三端点均在 OpenAPI 路径表内")
    methods: set[str] = set()
    for path in expected_paths:
        methods |= {key for key in (spec_paths.get(path) or {}) if key in {"get", "post", "put", "patch", "delete"}}
    c.check(
        methods == {"get", "post", "patch"},
        "新路由仅 GET / POST / PATCH（无 PUT / DELETE）",
        f"{sorted(methods)}",
    )

    for model in (
        VariantsGenerateRequest,
        VariantsGenerateResponse,
        VariantsBatchesResponse,
        AdoptVariantResponse,
    ):
        fields = set(model.model_fields)
        c.check(
            "llm_raw" not in fields and "embedding" not in fields,
            f"{model.__name__} 字段不含 llm_raw / embedding",
            f"{sorted(fields)}",
        )

    route_src = (BACKEND_DIR / "app" / "api" / "routes" / "content_variants.py").read_text(encoding="utf-8")
    c.check("sqlalchemy" not in route_src, "路由层不 import SQLAlchemy（静态断言）")
    c.check(
        "@router.put" not in route_src and "@router.delete" not in route_src,
        "路由文件无 PUT / DELETE 注册",
    )
    gen_pos = route_src.find('"/generate"')
    adopt_pos = route_src.find('"/{note_id}/adopt"')
    c.check(
        gen_pos != -1 and adopt_pos != -1 and gen_pos < adopt_pos,
        "静态路径 /generate 先于动态路径 /{note_id}/adopt 注册",
        f"{gen_pos} < {adopt_pos}",
    )

    main_src = (BACKEND_DIR / "app" / "main.py").read_text(encoding="utf-8")
    c.check(
        main_src.count("include_router(") >= 14
        and "include_router(content_variants_router)" in main_src,
        "main.py 注册含本步新 router（总数 ≥ 14）",
        f"count={main_src.count('include_router(')}",
    )
    c.check(
        len(re.findall(r"@asynccontextmanager", main_src)) == 1,
        "main.py 未新增第二个 lifespan",
    )

    # ---- 库内无 sk- 形态密钥 ----
    with SessionLocal() as db:
        note_hits = db.execute(
            text(
                "SELECT count(*) FROM notes WHERE account_id = :aid AND ("
                "COALESCE(title,'') LIKE '%sk-%' OR COALESCE(body,'') LIKE '%sk-%' "
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

    # ---- 本步新增代码不含平台域名 / 登录态字段 ----
    hits: list[str] = []
    for parts in _SCAN_PATHS:
        path = BACKEND_DIR.joinpath(*parts)
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for file in files:
            content = file.read_text(encoding="utf-8")
            for marker in _FORBIDDEN_MARKERS:
                if marker in content:
                    hits.append(f"{file.name}:{marker}")
    c.check(not hits, "本步新增代码不含平台域名 / 登录态字段", f"{hits}")

    # ---- public schema 仍 10 张业务表（零迁移）----
    with SessionLocal() as db:
        tables = [
            row[0]
            for row in db.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
            ).all()
        ]
    business = [item for item in tables if item != "alembic_version"]
    c.check(
        len(business) == 10 and "notes" in business,
        "public schema 仍为 10 张业务表（本步未新增表 / 未新增列）",
        f"{len(business)}: {business}",
    )


# ---------------------------------------------------------------------------
# 组 10：零迁移（versions 恰 2 支 / current 0002 head / check 零漂移）
# ---------------------------------------------------------------------------
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


def group10_migrations(c: Checker) -> None:
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
        "alembic check 零漂移（模型与迁移一致；零迁移策略）",
        f"rc={code_check} out={out_check.strip()[:400]}",
    )


# ---------------------------------------------------------------------------
# 组 11：物理清理复查
# ---------------------------------------------------------------------------
def group11_cleanup(c: Checker, f: Fixture) -> None:
    c.check(
        all(value == 0 for value in f.residue.values()) and bool(f.residue),
        "测试数据已物理清理，复查残留为 0（双口径）",
        f"{f.residue}",
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    c = Checker()
    print("=" * 78)
    print("第 23 步 · V2 增强（二）· 多版本创作 Agent 离线自检（不联网、不需要 API Key）")
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
        group2_clean(c, f)
        group3_validation(c, f)
        group4_angles_bounds(c, f)
        group5_reject_loop(c, f)
        group6_blocked(c, f)
        group7_all_failed(c, f)
        group8_batches_adopt(c, f)
        group9_interfaces_security(c, f)
        group10_migrations(c)
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

    group11_cleanup(c, f)

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
