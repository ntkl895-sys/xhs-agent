"""第 10 步 · 选题 Agent【离线】自检（**不连外网、不需要真实 API Key**）。

用**脚本模型**（确定性返回 / 抛异常）经 service 的 ``model`` 注入口驱动整条链路，
断言 7 组行为（共 40+ 项）：

1. 图结构：含 load_context / generate_topics / validate_dedupe / persist 节点，
   以及 validate_dedupe 的**条件边**（回 generate_topics 的回路、去 persist、去 give_up）；
2. 合格响应：单热点落库 per_hotspot 条，字段映射正确（tags 剥 #、source_hotspot_ids、
   source_trace、score=confidence、difficulty、heat_score、meta），status=draft；
   对应 agent_runs 的 ``outputs`` 业务计数与 ``llm_raw``、token、json_parse_attempts 就位；
3. 脏 JSON（围栏 / 裸控制字符 / 噪声）可解析；「先噪后valid」时 parse_attempts 正确；
4. ScriptedChatModel 首轮不合格、次轮合格 → 条件边回环后落库；始终不合格 → 该热点 failed、无半成品；
5. force=False 重复运行 → skipped 不插不耗模型；归一化重复标题被过滤；库内重复被过滤；
6. persona 缺失 404；批量中某热点可重试耗尽异常 → 其余照常、整体 partial、失败热点 agent_runs=failed；
7. 安全：topics / agent_runs 均无 ``sk-``；本步新增代码不含小红书域名 / 登录态字段。

依赖本地 docker 的 PostgreSQL（与 ``selftest_agent_harness.py`` 同）；测试数据一律带 ``[SELFTEST]``
并在结束时**物理清理**，复查无残留。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\selftest_topic_agent.py

全部通过退出码 0；任一项 FAIL 退出码 1。
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402

from app.agent.checkpoint import close_checkpointer, init_checkpointer  # noqa: E402
from app.agent.errors import AgentRetryableError  # noqa: E402
from app.agent.topics.graph import (  # noqa: E402
    NODE_GENERATE,
    NODE_GIVE_UP,
    NODE_LOAD_CONTEXT,
    NODE_PERSIST,
    NODE_VALIDATE,
    build_topic_graph,
)
from app.agent.topics.service import generate_for_hotspots  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.core.timeutil import today_local  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.models.hotspot import Hotspot  # noqa: E402
from app.models.persona import Persona  # noqa: E402

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from pydantic import Field  # noqa: E402

SELFTEST_TAG = "[SELFTEST]"


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
# 脚本模型：按「提示词里出现的热点标题」分派各自的响应序列
# ---------------------------------------------------------------------------
class TopicScriptModel(BaseChatModel):
    """确定性假模型：按热点标题分派响应序列，可返回字符串 / 对象 / 异常。"""

    scripts: dict[str, list[Any]] = Field(default_factory=dict)
    fallback: list[Any] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    total_calls: int = 0
    usage_in: int = 11
    usage_out: int = 22

    @property
    def _llm_type(self) -> str:
        return "topic-script"

    def _text_of(self, messages: list[Any]) -> str:
        parts: list[str] = []
        for message in messages:
            content = getattr(message, "content", "")
            if isinstance(content, str):
                parts.append(content)
        return "\n".join(parts)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # noqa: ANN001
        text = self._text_of(messages)
        key: str | None = None
        for candidate in self.scripts:
            if candidate in text:
                key = candidate
                break
        sequence = list(self.scripts.get(key, self.fallback)) if key is not None else list(self.fallback)
        if not sequence:
            sequence = ['{"topics": []}']
        bucket = key or "__fallback__"
        index = self.counts.get(bucket, 0)
        item = sequence[min(index, len(sequence) - 1)]
        self.counts[bucket] = index + 1
        self.total_calls += 1

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


def candidate(tag: str, index: int, *, title: str | None = None, difficulty: str = "easy") -> dict[str, Any]:
    """构造一条合格候选（title 唯一）。"""
    return {
        "title": title if title is not None else f"{SELFTEST_TAG}{tag}{index}",
        "summary": "写给谁 / 为什么写",
        "angle": "从痛点切入",
        "pillar": "生活",
        "content_outline": ["大纲一", "大纲二", "大纲三"],
        "tags": [f"#标签{tag}{index}a", f"标签{tag}{index}b", f"标签{tag}{index}c"],
        "target_keywords": [f"关键词{tag}{index}"],
        "difficulty": difficulty,
        "confidence": 0.66,
    }


def topics_payload(*items: dict[str, Any]) -> dict[str, Any]:
    return {"topics": list(items)}


# 脏 JSON：代码围栏 + 字符串内裸换行/制表符 + 前后噪声
DIRTY_RESPONSE = (
    "好的，这是结果：\n```json\n"
    '{"topics": [{"title": "[SELFTEST]S3a1",\n\t"summary": "含裸控制字符",'
    ' "angle": "a", "pillar": "p", "content_outline": ["o1", "o2", "o3"],'
    ' "tags": ["#t1", "t2", "t3"], "target_keywords": ["k1"], "difficulty": "medium",'
    ' "confidence": 0.5}]}\n```\n以上。'
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------
class Fixture:
    def __init__(self) -> None:
        self.account_id: uuid.UUID | None = None
        self.persona_id: uuid.UUID | None = None
        self.hotspot_ids: list[uuid.UUID] = []
        self.before_threads: set[str] = set()

    def _current_threads(self) -> set[str]:
        with SessionLocal() as db:
            try:
                rows = db.execute(text("SELECT DISTINCT thread_id FROM langgraph.checkpoints")).all()
            except Exception:  # noqa: BLE001 —— 表可能尚未建
                return set()
        return {str(row[0]) for row in rows}

    def setup(self) -> None:
        self.before_threads = self._current_threads()
        today = today_local()
        with SessionLocal() as db:
            account = Account(name=f"{SELFTEST_TAG}选题账号")
            db.add(account)
            db.flush()
            persona = Persona(
                account_id=account.id,
                name=f"{SELFTEST_TAG}选题人设",
                positioning="面向租房青年的生活妙招号",
                target_audience="22-30 岁租房青年",
                tone="口语化、轻松",
                content_pillars=["租房改造", "省钱好物"],
            )
            db.add(persona)
            db.flush()
            self.account_id = account.id
            self.persona_id = persona.id

            for index in range(9):
                hotspot = Hotspot(
                    source="selftest",
                    source_item_id=f"selftest-{index}",
                    title=f"{SELFTEST_TAG}热点{index}",
                    url=f"https://example.com/hot/{index}",
                    mobile_url=f"https://example.com/m/{index}",
                    hot_score=100000 - index,
                    rank_position=index + 1,
                    hot_date=today,
                    content_hash=f"selftest-{uuid.uuid4().hex}",
                )
                db.add(hotspot)
                db.flush()
                self.hotspot_ids.append(hotspot.id)
            db.commit()

    def hotspot(self, index: int) -> uuid.UUID:
        return self.hotspot_ids[index]

    def cleanup(self) -> None:
        # 清理本次自检新建的检查点线程（按「运行前后差集」安全删除，不动历史线程）
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
        with SessionLocal() as db:
            if self.persona_id is not None:
                db.execute(
                    text("DELETE FROM agent_runs WHERE persona_id = :pid"),
                    {"pid": str(self.persona_id)},
                )
                db.execute(
                    text("DELETE FROM topics WHERE persona_id = :pid"),
                    {"pid": str(self.persona_id)},
                )
            if self.hotspot_ids:
                db.execute(
                    text("DELETE FROM hotspots WHERE source = 'selftest'"),
                )
            if self.persona_id is not None:
                db.execute(text("DELETE FROM personas WHERE id = :pid"), {"pid": str(self.persona_id)})
            if self.account_id is not None:
                db.execute(text("DELETE FROM accounts WHERE id = :aid"), {"aid": str(self.account_id)})
            db.commit()


def _fetch_topic_rows(persona_id: uuid.UUID, hotspot_id: uuid.UUID) -> list[dict[str, Any]]:
    # ⚠️ 必须 ORDER BY：本函数返回**同一热点的多条**候选（group2 会断言 `rows[0]` 的 tags /
    # target_keywords）。PostgreSQL 不保证无序 SELECT 的返回顺序，物理行序又会随插入 / 删除
    # （其他自检脚本在同一张表上增删）而改变 —— 第 12 步实测到此偶发失败（40/42 → 复跑 42/42）。
    # 这里按 title 排序（标题在本脚本内唯一且带序号，与断言中的 S2a1 / S2a2 / S2a3 顺序一致）。
    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT title, score, difficulty, heat_score, status, tags, content_outline, "
                "target_keywords, source_hotspot_ids, source_trace, meta, llm_model "
                "FROM topics WHERE persona_id = :pid AND source_hotspot_ids @> CAST(:hid AS jsonb) "
                "ORDER BY title"
            ),
            {"pid": str(persona_id), "hid": json.dumps([str(hotspot_id)])},
        ).all()
    keys = [
        "title", "score", "difficulty", "heat_score", "status", "tags", "content_outline",
        "target_keywords", "source_hotspot_ids", "source_trace", "meta", "llm_model",
    ]
    return [dict(zip(keys, row, strict=True)) for row in rows]


def _fetch_run(run_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        row = db.execute(
            text(
                "SELECT status, llm_model, llm_base_url, prompt_tokens, completion_tokens, "
                "total_tokens, json_parse_attempts, outputs, llm_raw, error_message "
                "FROM agent_runs WHERE id = :rid"
            ),
            {"rid": run_id},
        ).first()
    if row is None:
        return {}
    keys = [
        "status", "llm_model", "llm_base_url", "prompt_tokens", "completion_tokens",
        "total_tokens", "json_parse_attempts", "outputs", "llm_raw", "error_message",
    ]
    return dict(zip(keys, row, strict=True))


# ---------------------------------------------------------------------------
# 各组断言
# ---------------------------------------------------------------------------
def group1_graph(c: Checker) -> None:
    graph = build_topic_graph(model=TopicScriptModel(), use_checkpointer=False)
    view = graph.get_graph()
    nodes = set(view.nodes.keys())
    c.check(
        {NODE_LOAD_CONTEXT, NODE_GENERATE, NODE_VALIDATE, NODE_PERSIST}.issubset(nodes),
        "图含 load_context / generate_topics / validate_dedupe / persist 四个节点",
        f"实际={sorted(nodes)}",
    )
    edges = {(edge.source, edge.target, bool(edge.conditional)) for edge in view.edges}
    c.check(
        (NODE_VALIDATE, NODE_GENERATE, True) in edges,
        "validate_dedupe 存在「回退重生成」条件边",
        f"edges={sorted(edges)}",
    )
    c.check(
        (NODE_VALIDATE, NODE_PERSIST, True) in edges and (NODE_VALIDATE, NODE_GIVE_UP, True) in edges,
        "validate_dedupe 存在去 persist / give_up 的条件边",
    )
    c.check(
        (NODE_PERSIST, "__end__", False) in edges,
        "persist 连接 END",
    )


def group2_valid(c: Checker, f: Fixture) -> None:
    hotspot = f.hotspot(0)
    model = TopicScriptModel(
        scripts={
            f"{SELFTEST_TAG}热点0": [
                topics_payload(candidate("S2a", 1), candidate("S2a", 2), candidate("S2a", 3))
            ]
        }
    )
    summary = generate_for_hotspots(
        persona_id=f.persona_id, hotspot_ids=[hotspot], per_hotspot=3, force=True, model=model
    )
    c.check(summary["status"] == "success" and summary["persisted_count"] == 3,
            "合格响应：单热点落库 3 条（status=success）", f"{summary}")

    rows = _fetch_topic_rows(f.persona_id, hotspot)
    c.check(len(rows) == 3, "topics 实际写入 3 行")
    row = rows[0]
    c.check(row["status"] == "draft", "落库 status=draft", f"{row['status']}")
    c.check(row["tags"] == ["标签S2a1a", "标签S2a1b", "标签S2a1c"],
            "tags 已剥除误带的 # 且顺序保留", f"{row['tags']}")
    c.check(row["content_outline"] == ["大纲一", "大纲二", "大纲三"], "content_outline 正确")
    c.check(row["target_keywords"] == ["关键词S2a1"], "target_keywords 正确")
    c.check(row["source_hotspot_ids"] == [str(hotspot)], "source_hotspot_ids=[该热点 id]")
    trace = row["source_trace"]
    c.check(
        isinstance(trace, list) and trace and trace[0].get("source") == "selftest"
        and "title" in trace[0] and "hot_date" in trace[0] and "hot_score" in trace[0]
        and "rank_position" in trace[0] and "url" in trace[0] and "mobile_url" in trace[0],
        "source_trace 含 source/title/url/mobile_url/hot_date/hot_score/rank_position",
        f"{trace}",
    )
    c.check(abs(float(row["score"]) - 0.66) < 1e-6, "score == 模型 confidence", f"{row['score']}")
    c.check(row["difficulty"] == "easy" and abs(float(row["heat_score"]) - 100000.0) < 1e-6,
            "difficulty 与 heat_score（来自热点）正确", f"{row['difficulty']}/{row['heat_score']}")
    meta = row["meta"] or {}
    c.check(meta.get("prompt_version") == "v1" and meta.get("json_parse_attempts") == 1
            and abs(float(meta.get("confidence", -1)) - 0.66) < 1e-6,
            "meta 含 prompt_version / json_parse_attempts / confidence", f"{meta}")
    c.check(bool(row["llm_model"]), "llm_model 已写入", f"{row['llm_model']}")

    run = _fetch_run(summary["runs"][0])
    outputs = run.get("outputs") or {}
    c.check(run.get("status") == "success", "agent_runs 状态 success", f"{run.get('status')}")
    c.check(
        outputs.get("persisted") == 3 and outputs.get("generated") == 3
        and outputs.get("rejected") == 0 and outputs.get("skipped") == 0
        and len(outputs.get("topic_ids") or []) == 3,
        "agent_runs.outputs 已回填业务计数与 topic_ids", f"{outputs}",
    )
    c.check(run.get("prompt_tokens") == 11 and run.get("completion_tokens") == 22
            and run.get("total_tokens") == 33,
            "agent_runs token 已回填（11/22/33）",
            f"{run.get('prompt_tokens')}/{run.get('completion_tokens')}/{run.get('total_tokens')}")
    c.check(run.get("json_parse_attempts") == 1, "agent_runs.json_parse_attempts == 1")
    c.check(run.get("llm_raw") and SELFTEST_TAG in str(run["llm_raw"]),
            "agent_runs.llm_raw 已回填模型原始输出")
    c.check(run.get("llm_base_url") == "https://api.deepseek.com" and "sk-" not in str(run.get("llm_raw")),
            "llm_base_url 记录且 llm_raw 不含 sk-")


def group3_dirty_json(c: Checker, f: Fixture) -> None:
    # 3a：围栏 + 裸控制字符 + 噪声，一次命中
    dirty_hotspot = f.hotspot(1)
    model_a = TopicScriptModel(scripts={f"{SELFTEST_TAG}热点1": [DIRTY_RESPONSE]})
    summary_a = generate_for_hotspots(
        persona_id=f.persona_id, hotspot_ids=[dirty_hotspot], per_hotspot=1, force=True, model=model_a
    )
    c.check(summary_a["persisted_count"] == 1, "脏 JSON（围栏+裸控制字符+噪声）解析并落库 1 条",
            f"{summary_a}")
    rows_a = _fetch_topic_rows(f.persona_id, dirty_hotspot)
    c.check(
        rows_a and rows_a[0]["title"] == f"{SELFTEST_TAG}S3a1" and (rows_a[0]["meta"] or {}).get("json_parse_attempts") == 1,
        "脏 JSON 一次命中（json_parse_attempts=1）",
        f"{rows_a[:1]}",
    )

    # 3b：先噪后 valid → attempts == 2
    noise_hotspot = f.hotspot(2)
    model_b = TopicScriptModel(
        scripts={
            f"{SELFTEST_TAG}热点2": [
                "这段完全不是 JSON 的自由文本。",
                topics_payload(candidate("S3b", 1)),
            ]
        }
    )
    summary_b = generate_for_hotspots(
        persona_id=f.persona_id, hotspot_ids=[noise_hotspot], per_hotspot=1, force=True, model=model_b
    )
    rows_b = _fetch_topic_rows(f.persona_id, noise_hotspot)
    run_b = _fetch_run(summary_b["runs"][0])
    c.check(
        rows_b and (rows_b[0]["meta"] or {}).get("json_parse_attempts") == 2
        and run_b.get("json_parse_attempts") == 2,
        "「先噪后 valid」parse_attempts 正确记为 2",
        f"meta={(rows_b[0]['meta'] if rows_b else None)} run={run_b.get('json_parse_attempts')}",
    )


def group4_loop_and_fail(c: Checker, f: Fixture) -> None:
    # 4a：首轮不合格 → 次轮合格（验证回环 + 最终落库）
    loop_hotspot = f.hotspot(3)
    bad_round = topics_payload(
        {"title": "超" * 30, "content_outline": ["o1"], "tags": ["t1"], "target_keywords": ["k"],
         "difficulty": "easy", "confidence": 0.4}
    )
    good_round = topics_payload(candidate("S4a", 1), candidate("S4a", 2))
    model_a = TopicScriptModel(scripts={f"{SELFTEST_TAG}热点3": [bad_round, good_round]})
    summary_a = generate_for_hotspots(
        persona_id=f.persona_id, hotspot_ids=[loop_hotspot], per_hotspot=2, force=True, model=model_a
    )
    rows_a = _fetch_topic_rows(f.persona_id, loop_hotspot)
    c.check(
        summary_a["persisted_count"] == 2 and len(rows_a) == 2 and model_a.total_calls >= 2,
        "首轮不合格 → 条件边回环 → 次轮合格并落库 2 条",
        f"{summary_a} calls={model_a.total_calls}",
    )
    run_a = _fetch_run(summary_a["runs"][0])
    c.check((run_a.get("outputs") or {}).get("rejected", 0) >= 1,
            "回环轮次的不合格项记入 outputs.rejected",
            f"{(run_a.get('outputs') or {}).get('rejected')}")

    # 4b：始终不合格 → 该热点 failed、无半成品
    fail_hotspot = f.hotspot(4)
    always_bad = topics_payload(
        {"title": "超" * 40, "content_outline": ["o1"], "tags": ["t1"], "target_keywords": ["k"],
         "difficulty": "nope", "confidence": 5}
    )
    model_b = TopicScriptModel(scripts={f"{SELFTEST_TAG}热点4": [always_bad]})
    summary_b = generate_for_hotspots(
        persona_id=f.persona_id, hotspot_ids=[fail_hotspot], per_hotspot=3, force=True, model=model_b
    )
    rows_b = _fetch_topic_rows(f.persona_id, fail_hotspot)
    c.check(
        summary_b["failed"] == 1 and summary_b["persisted_count"] == 0 and not rows_b
        and summary_b["status"] == "failed",
        "始终不合格：该热点 failed 且无半成品 topics",
        f"{summary_b} rows={len(rows_b)}",
    )


def group5_skip_and_dedupe(c: Checker, f: Fixture) -> None:
    hotspot = f.hotspot(5)
    payload = topics_payload(candidate("S5a", 1), candidate("S5a", 2))
    model1 = TopicScriptModel(scripts={f"{SELFTEST_TAG}热点5": [payload]})
    generate_for_hotspots(
        persona_id=f.persona_id, hotspot_ids=[hotspot], per_hotspot=2, force=True, model=model1
    )

    # 5a：force=False 重复 → skipped、不插、不耗模型
    model2 = TopicScriptModel(scripts={f"{SELFTEST_TAG}热点5": [payload]})
    summary = generate_for_hotspots(
        persona_id=f.persona_id, hotspot_ids=[hotspot], per_hotspot=2, force=False, model=model2
    )
    c.check(
        summary["skipped"] == 1 and summary["persisted_count"] == 0 and model2.total_calls == 0,
        "force=False 重复热点 → skipped 且未调用模型",
        f"{summary} calls={model2.total_calls}",
    )

    # 5b：归一化重复标题（同一轮内）被过滤
    dup_hotspot = f.hotspot(6)
    dup_payload = topics_payload(
        candidate("S5b", 1, title=f"{SELFTEST_TAG}S5b1"),
        candidate("S5b", 1, title=f"{SELFTEST_TAG.lower()}s5b1 "),  # 归一化后与上一条重复
    )
    model3 = TopicScriptModel(scripts={f"{SELFTEST_TAG}热点6": [dup_payload]})
    summary3 = generate_for_hotspots(
        persona_id=f.persona_id, hotspot_ids=[dup_hotspot], per_hotspot=1, force=True, model=model3
    )
    rows3 = _fetch_topic_rows(f.persona_id, dup_hotspot)
    run3 = _fetch_run(summary3["runs"][0])
    c.check(
        summary3["persisted_count"] == 1 and len(rows3) == 1
        and (run3.get("outputs") or {}).get("skipped") == 1,
        "归一化重复标题被过滤（1 合格 + 1 重复跳过）",
        f"{summary3} skipped={(run3.get('outputs') or {}).get('skipped')}",
    )

    # 5c：库内已存在同标题 → 全部判重 → 该热点 failed、不新增
    model4 = TopicScriptModel(scripts={f"{SELFTEST_TAG}热点6": [dup_payload]})
    summary4 = generate_for_hotspots(
        persona_id=f.persona_id, hotspot_ids=[dup_hotspot], per_hotspot=1, force=True, model=model4
    )
    rows4 = _fetch_topic_rows(f.persona_id, dup_hotspot)
    c.check(
        summary4["failed"] == 1 and summary4["persisted_count"] == 0 and len(rows4) == 1,
        "force=True 但标题与库内重复 → 判重后 failed、不新增",
        f"{summary4} rows={len(rows4)}",
    )


def group6_missing_and_partial(c: Checker, f: Fixture) -> None:
    # 6a：persona 缺失 → 404 AppError
    missing_persona = uuid.uuid4()
    try:
        generate_for_hotspots(persona_id=missing_persona, hotspot_ids=[f.hotspot(7)], model=TopicScriptModel())
        c.check(False, "persona 缺失抛 404", "未抛异常")
    except AppError as exc:
        c.check(exc.status_code == 404 and exc.code == "not_found", "persona 缺失抛 404 not_found",
                f"{exc.status_code}/{exc.code}")
    except Exception as exc:  # noqa: BLE001
        c.check(False, "persona 缺失抛 404", f"异常类型={type(exc).__name__}: {exc}")

    # 6b：批量中一个热点可重试耗尽 → 其余照常、整体 partial、失败热点 agent_runs=failed
    ok_hotspot = f.hotspot(7)
    fail_hotspot = f.hotspot(8)
    model = TopicScriptModel(
        scripts={
            f"{SELFTEST_TAG}热点7": [topics_payload(candidate("S6a", 1))],
            f"{SELFTEST_TAG}热点8": [AgentRetryableError("模拟 429 持续失败（可重试耗尽）")],
        }
    )
    summary = generate_for_hotspots(
        persona_id=f.persona_id, hotspot_ids=[ok_hotspot, fail_hotspot],
        per_hotspot=1, force=True, model=model,
    )
    c.check(
        summary["succeeded"] == 1 and summary["failed"] == 1 and summary["status"] == "partial",
        "批量：一成功一失败 → 整体 partial",
        f"{summary}",
    )
    c.check(summary["persisted_count"] == 1, "成功热点照常落库 1 条", f"{summary['persisted_count']}")
    with SessionLocal() as db:
        failed_rows = db.execute(
            text(
                "SELECT count(*) FROM agent_runs WHERE persona_id = :pid "
                "AND run_type = 'topic_generation' AND status = 'failed'"
            ),
            {"pid": str(f.persona_id)},
        ).scalar_one()
    c.check(failed_rows >= 1, "失败热点在 agent_runs 落 failed", f"{failed_rows}")


def group7_security(c: Checker, f: Fixture) -> None:
    with SessionLocal() as db:
        topic_hits = db.execute(
            text(
                "SELECT count(*) FROM topics WHERE persona_id = :pid AND ("
                "title LIKE '%sk-%' OR COALESCE(meta::text,'') LIKE '%sk-%' "
                "OR COALESCE(source_trace::text,'') LIKE '%sk-%')"
            ),
            {"pid": str(f.persona_id)},
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
    c.check(topic_hits == 0, "topics 无 sk- 形态密钥", f"命中 {topic_hits}")
    c.check(run_hits == 0, "agent_runs 无 sk- 形态密钥", f"命中 {run_hits}")

    # 本步新增代码不出现小红书域名 / 登录态字段
    markers = ("xiaohongshu.com", "xhslink", "Cookie", "x-s-common", "x-t=", "web_session")
    scan_paths = [
        BACKEND_DIR / "app" / "agent" / "topics",
        BACKEND_DIR / "app" / "crud" / "topic.py",
        BACKEND_DIR / "app" / "schemas" / "topic.py",
        BACKEND_DIR / "app" / "api" / "routes" / "topics.py",
    ]
    hits: list[str] = []
    for path in scan_paths:
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for file in files:
            content = file.read_text(encoding="utf-8")
            for marker in markers:
                if marker in content:
                    hits.append(f"{file.name}:{marker}")
    c.check(not hits, "本步新增代码不含小红书域名 / 登录态字段", f"{hits}")


def group8_routes(c: Checker) -> None:
    paths = fastapi_app.openapi()["paths"]
    c.check("/api/v1/topics/generate" in paths and "post" in paths["/api/v1/topics/generate"],
            "POST /api/v1/topics/generate 已注册")
    c.check("/api/v1/topics/runs" in paths and "get" in paths["/api/v1/topics/runs"],
            "GET /api/v1/topics/runs 已注册（静态路径）")
    c.check("/api/v1/topics/{topic_id}" in paths
            and set(paths["/api/v1/topics/{topic_id}"]) == {"get", "patch"},
            "GET/PATCH /api/v1/topics/{topic_id} 已注册")
    c.check("get" in paths.get("/api/v1/topics", {}), "GET /api/v1/topics 列表已注册")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    c = Checker()
    print("=" * 78)
    print("第 10 步 · 选题 Agent 离线自检（不联网、不需要 API Key）")
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
        group3_dirty_json(c, f)
        group4_loop_and_fail(c, f)
        group5_skip_and_dedupe(c, f)
        group6_missing_and_partial(c, f)
        group7_security(c, f)
        group8_routes(c)
    finally:
        f.cleanup()
        close_checkpointer()

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
