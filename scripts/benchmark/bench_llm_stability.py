# -*- coding: utf-8 -*-
"""度量专项 · 阶段二：真实 LLM 稳定性与成本（统一 n=5 抽样，7 张业务图）。

⚠️ 本脚本**默认不发任何请求**：

- 未配置 LLM（开关 / key / 模型名）→ 打印原因并**退出码 2（零请求）**；
- 就绪时先打印「将发出的请求次数与 token 估算」并**等待确认**
  （交互输入 yes；脚本化用 ``--yes``；非交互终端且未给 --yes → 拒绝并退出码 3）；
- ``--dry-run``：只做夹具准备 / 清理与估算打印，**绝不调用任何图**（可安全随时运行）。

覆盖图（可用 ``--graphs`` 选择子集；默认全部 7 个）：

- topics（选题）/ content（创作）/ content_variants（多版本）/ metric_vision（指标识别）
- diagnosis（单篇诊断）/ weekly（周报）/ insights（规律提炼）

每轮记录：``parse_attempts``、是否一次成功（parse_attempts==1 且 runs 全 success）、
runs 状态、prompt/completion/total tokens、墙钟时延；按图汇总：一次成功率、
token 与延迟的 P50/P95。**n=5 小样本，结论只能写「抽样」口径，不得外推为总体稳定性。**

另有两个**必须分开报**的口径（本次修复新增）：

- ``raw_direct_parse_rate``：把 ``agent_runs.llm_raw`` 用 ``extract_json_with_source``
  离线复算，命中方式为「原文」的占比——这才是「模型一次吐对」；
- ``contract_pass_rate``：原始 JSON 再按各图**自己的** schema 做字段级契约校验
  （复用业务模块的 Pydantic 模型 / ``normalize_*`` 纯函数，不另写一套规则）；
  若它与 ``success_rate`` 相等，结果里显式注明等价关系。

分母纪律：``raw_runs_total`` = **有真实调用证据**的 run 数（``json_parse_attempts>0`` 或
``total_tokens>0``；只建了 run 行、零 attempts 零 token 的按零调用处理，不计入）；
``raw_missing`` 单列，``*_rate`` 的分母是**可得 raw 样本数**，绝不静默缩小分母
（n=30 / missing=1 时 rate 的分母写 29，同时把 30 与 1 都记进指标）。

夹具（全部 ``[BENCH-LLM]`` 前缀；运行后必清理：按 ``account_id`` 关联 + 按前缀兜底双口径复查）：

- account + persona ×1；hotspot ×1（topics）；
- approved 选题 ×5/图（content、content_variants 各 5 条，逐一消耗）；
- 截图取自 ``docs/benchmark/fixtures/vision_synth/manifest.json`` 第一张（metric_vision）；
- 已发布 note + 2 条快照（diagnosis）；
- weekly / insights 不建业务夹具（只读既有数据；insights 另需 embedding 依赖就绪）。

线程与审计清理复用项目范式：``langgraph.{checkpoint_writes,checkpoint_blobs,checkpoints}``
按**运行前后差集**删除；``agent_runs`` 按差集删除。

**落盘先于清理**（本次修复）：结果 JSON 先完整写出并 fsync，之后才清库；
清理异常单独记进 ``detail.cleanup.cleanup_error``，不会让进程在写文件前中断
（历史事故：cleanup 外键冲突 → rc=1 → 结果文件根本没产出）。

用法（项目根目录）：

    :: 安全预检（不发请求）
    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_llm_stability.py --dry-run

    :: 真实运行（需在 backend\\.env 配置 key 与 LLM_MODEL；交互确认）
    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_llm_stability.py

    :: 只测单个图
    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_llm_stability.py --graphs topics --yes

退出码：0 完成；1 存在失败轮 / 清理后仍有残留；2 未配置（零请求）；3 环境或参数问题。
（清理**抛异常**但残留为 0 时不再置 1：异常已单独标记，结果文件已先落盘。）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bench_common as bc

bc.add_backend_to_path()

from sqlalchemy import text  # noqa: E402

from app.agent.checkpoint import close_checkpointer  # noqa: E402
from app.agent.errors import AgentConfigError  # noqa: E402
from app.agent.llm import factory as llm_factory  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.models.enums import (  # noqa: E402
    AccountStatus,
    ComplianceStatus,
    DataSource,
    HotspotStatus,
    NoteStatus,
    PersonaStatus,
    TopicStatus,
)
from app.models.hotspot import Hotspot  # noqa: E402
from app.models.metric import NoteMetric  # noqa: E402
from app.models.note import Note  # noqa: E402
from app.models.persona import Persona  # noqa: E402
from app.models.topic import Topic  # noqa: E402

PREFIX = "[BENCH-LLM]"
ROUNDS_DEFAULT = 5
GRAPH_ORDER = ["topics", "content", "content_variants", "metric_vision", "diagnosis", "weekly", "insights"]
GRAPH_LABELS = {
    "topics": "选题 Agent",
    "content": "创作 Agent",
    "content_variants": "多版本创作 Agent",
    "metric_vision": "指标识别（视觉）",
    "diagnosis": "单篇诊断",
    "weekly": "周报",
    "insights": "规律提炼",
}
METHOD = (
    "scripts/benchmark/bench_llm_stability.py：固定夹具 × n=5 轮真实调用，"
    "逐轮从 agent_runs 读取 json_parse_attempts / token，墙钟计时"
)


# ---------------------------------------------------------------------------
# 基础查询（与 check_* 脚本同源范式）
# ---------------------------------------------------------------------------
def _all_run_ids() -> set[str]:
    with SessionLocal() as db:
        return {str(row[0]) for row in db.execute(text("SELECT id FROM agent_runs")).all()}


def _all_thread_ids() -> set[str]:
    with SessionLocal() as db:
        try:
            rows = db.execute(text("SELECT DISTINCT thread_id FROM langgraph.checkpoints")).all()
        except Exception:  # noqa: BLE001 —— 框架表可能尚未建
            return set()
    return {str(row[0]) for row in rows}


def _all_snippet_ids() -> set[str]:
    with SessionLocal() as db:
        try:
            rows = db.execute(text("SELECT id FROM knowledge_snippets")).all()
        except Exception:  # noqa: BLE001
            return set()
    return {str(row[0]) for row in rows}


def _fetch_runs(run_ids: list[str]) -> list[dict]:
    if not run_ids:
        return []
    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT id, status, prompt_tokens, completion_tokens, total_tokens, "
                "json_parse_attempts, llm_raw FROM agent_runs WHERE id = ANY(:ids)"
            ),
            {"ids": run_ids},
        ).mappings().all()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# raw 复算 + schema 字段级契约校验（只读纯函数；不另写一套业务规则）
# ---------------------------------------------------------------------------
def _contract_check(graph: str, payload) -> tuple[bool, list[str]]:
    """用各图**自己的**契约对象/归一化函数做字段级校验。

    复用业务模块（只读导入）而不是在评测脚本里复制规则，避免「测的是一套、
    跑的是另一套」。返回 ``(是否通过, 失败原因列表)``。

    - topics：逐条 ``TopicCandidate.model_validate``，至少 1 条合格即通过；
    - content：``_normalize_draft(_unwrap_payload(...))`` 无 type_errors 且标题/正文非空；
    - content_variants：``normalize_variants`` 至少归一出 1 版；
    - metric_vision：``MetricRecognitionPayload.model_validate``；
    - diagnosis：``normalize_diagnosis_payload`` 无 errors；
    - weekly：``normalize_report_payload`` 无 errors；
    - insights：结构级（``patterns`` 非空数组且每条含 pattern 字符串；不做来源 id 白名单）。
    """
    try:
        if graph == "topics":
            from app.agent.topics.contracts import TopicCandidate  # noqa: PLC0415

            items = payload.get("topics") if isinstance(payload, dict) else payload
            if not isinstance(items, list) or not items:
                return False, ["缺少 topics 数组"]
            errors: list[str] = []
            for item in items:
                try:
                    TopicCandidate.model_validate(item)
                    return True, []
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{type(exc).__name__}")
            return False, errors[:3] or ["全部候选字段不合法"]

        if graph == "content":
            from app.agent.content.graph import _normalize_draft, _unwrap_payload  # noqa: PLC0415

            draft = _normalize_draft(_unwrap_payload(payload))
            if draft["type_errors"]:
                return False, list(draft["type_errors"])
            if not draft["title"] or not draft["body"]:
                return False, ["title/body 为空"]
            return True, []

        if graph == "content_variants":
            from app.agent.content_variants.graph import (  # noqa: PLC0415
                normalize_variants,
                unwrap_variants,
            )

            drafts, _extra = normalize_variants(unwrap_variants(payload), 3)
            return (True, []) if drafts else (False, ["未归一出任何版本"])

        if graph == "metric_vision":
            from app.agent.metrics.contracts import (  # noqa: PLC0415
                METRIC_FIELDS,
                MetricRecognitionPayload,
            )
            from app.agent.metrics.graph import _unwrap_payload  # noqa: PLC0415

            if not isinstance(payload, dict):
                return False, ["输出不是 JSON 对象"]
            unwrapped = _unwrap_payload(payload)
            obj = MetricRecognitionPayload.model_validate(unwrapped)
            # 契约本身对 metrics 极宽容（缺失即 None），因此这里再要求「六指标至少出现一个」，
            # 否则空对象也会被判通过，contract_pass_rate 会虚高。
            present = [key for key in METRIC_FIELDS if key in (obj.metrics or {})]
            if not present:
                return False, ["六指标一个都没出现"]
            return True, []

        if graph == "diagnosis":
            from app.agent.diagnosis.contracts import normalize_diagnosis_payload  # noqa: PLC0415
            from app.core.config import settings  # noqa: PLC0415

            _norm, errors, _warn = normalize_diagnosis_payload(
                payload,
                cold_start=False,
                max_items=settings.DIAG_MAX_LIST_ITEMS,
                text_max_len=settings.DIAG_TEXT_MAX_LEN,
                score_min=settings.DIAG_SCORE_MIN,
                score_max=settings.DIAG_SCORE_MAX,
            )
            return (not errors), errors[:3]

        if graph == "weekly":
            from app.agent.weekly.contracts import normalize_report_payload  # noqa: PLC0415

            _norm, errors, _warn = normalize_report_payload(
                payload, period_start="", period_end=""
            )
            return (not errors), errors[:3]

        if graph == "insights":
            items = payload.get("patterns") if isinstance(payload, dict) else payload
            if not isinstance(items, list) or not items:
                return False, ["缺少 patterns 数组"]
            bad = [
                idx
                for idx, item in enumerate(items)
                if not isinstance(item, dict) or not isinstance(item.get("pattern"), str)
            ]
            return (not bad), [f"第 {idx} 条缺少 pattern" for idx in bad[:3]]

        return False, [f"未定义契约：{graph}"]
    except Exception as exc:  # noqa: BLE001 —— 契约校验自身异常也算不通过，不炸评测
        return False, [f"{type(exc).__name__}: {exc}"][:3]


def _analyze_raw(graph: str, row: dict) -> dict:
    """单条 run 的 raw 复算记录（不保留原文，只留命中方式与契约结果）。

    ``llm_called`` 用**证据口径**判定：``json_parse_attempts > 0`` 或
    ``total_tokens > 0``。只建了 run 行、但既没解析也没 token 的（如 insights
    样本不足直接返回），属于「零调用轮次」，不得计入 raw 分母。
    """
    from app.agent.parsing.json_repair import extract_json_with_source  # noqa: PLC0415

    run_id = str(row["id"])
    called = bool(int(row.get("json_parse_attempts") or 0) > 0 or int(row.get("total_tokens") or 0) > 0)
    raw = row.get("llm_raw")
    if not called:
        return {
            "run_id": run_id,
            "llm_called": False,
            "raw_missing": True,
            "source": None,
            "parsed": False,
            "contract_ok": None,
            "contract_errors": ["零调用 run（无 attempts 也无 token），不计入 raw 分母"],
        }
    if not raw or not str(raw).strip():
        return {
            "run_id": run_id,
            "llm_called": True,
            "raw_missing": True,
            "source": None,
            "parsed": False,
            "contract_ok": None,
            "contract_errors": ["缺少 llm_raw（未落库）"],
        }
    payload, source = extract_json_with_source(str(raw))
    if payload is None:
        return {
            "run_id": run_id,
            "llm_called": True,
            "raw_missing": False,
            "source": source,
            "parsed": False,
            "contract_ok": False,
            "contract_errors": ["三层兜底均未命中"],
        }
    ok, errors = _contract_check(graph, payload)
    return {
        "run_id": run_id,
        "llm_called": True,
        "raw_missing": False,
        "source": source,
        "parsed": True,
        "contract_ok": ok,
        "contract_errors": errors,
    }


def _raw_summary(rounds: list[dict]) -> dict:
    """raw 口径汇总：总调用数 / 缺失数 / 可得样本数 / 三个率。

    分母只算**有真实调用证据**的 run（``llm_called=True``），零调用 run 不计入。
    """
    runs = [
        item
        for record in rounds
        for item in record.get("raw_runs", [])
        if item.get("llm_called")
    ]
    total = len(runs)
    missing = sum(1 for item in runs if item["raw_missing"])
    available = total - missing
    direct = sum(1 for item in runs if item.get("source") == "原文")
    parsed = sum(1 for item in runs if item.get("parsed"))
    contract = sum(1 for item in runs if item.get("contract_ok") is True)
    sources: dict[str, int] = {}
    for item in runs:
        key = item.get("source") or "（缺失）"
        sources[key] = sources.get(key, 0) + 1
    return {
        "raw_runs_total": total,
        "raw_missing": missing,
        "raw_sample_n": available,
        "raw_direct_parse": direct,
        "raw_direct_parse_rate": round(direct / available, 4) if available else None,
        "raw_parsed": parsed,
        "raw_parsed_rate": round(parsed / available, 4) if available else None,
        "contract_pass": contract,
        "contract_pass_rate": round(contract / available, 4) if available else None,
        "source_distribution": sources,
    }


# ---------------------------------------------------------------------------
# 调用适配器（服务层入口；全部为「先 diff 后取数」，不依赖返回字段名）
# ---------------------------------------------------------------------------
def call_topics(fx: dict) -> dict:
    from app.agent.topics.service import generate_for_hotspots  # noqa: PLC0415

    summary = generate_for_hotspots(
        persona_id=fx["persona_id"],
        hotspot_ids=[fx["hotspot_id"]],
        per_hotspot=1,
        force=True,
        trigger="manual",
    )
    return {"status": summary.get("status"), "persisted": summary.get("persisted_count")}


def call_content(fx: dict) -> dict:
    from app.agent.content.service import create_note_for_topic  # noqa: PLC0415

    topic_id = fx["content_topics"].pop(0)
    summary = create_note_for_topic(topic_id=topic_id, trigger="manual")
    return {"status": summary.get("status")}


def call_content_variants(fx: dict) -> dict:
    from app.agent.content_variants.service import generate_variants  # noqa: PLC0415

    topic_id = fx["variant_topics"].pop(0)
    summary = generate_variants(topic_id=topic_id, n=3, trigger="manual")
    return {"status": summary.get("status"), "persisted": summary.get("persisted")}


def call_metric_vision(fx: dict) -> dict:
    from app.agent.metrics.service import recognize_metric_screenshot  # noqa: PLC0415

    image_bytes, image_name = fx["vision_image"]
    summary = recognize_metric_screenshot(image_bytes, "image/png", image_name=image_name)
    return {"status": "success", "parse_attempts": summary.get("parse_attempts")}


def call_diagnosis(fx: dict) -> dict:
    from app.agent.diagnosis.service import diagnose_note  # noqa: PLC0415

    summary = diagnose_note(note_id=fx["note_id"], trigger="manual")
    return {"status": summary.get("status") or "success", "score": summary.get("score")}


def call_weekly(fx: dict) -> dict:  # noqa: ARG001 - 无专用夹具
    from app.agent.weekly.service import generate_weekly_report  # noqa: PLC0415

    summary = generate_weekly_report(trigger="manual", days=7)
    return {"status": "success", "parse_attempts": summary.get("parse_attempts")}


def call_insights(fx: dict) -> dict:  # noqa: ARG001 - 无专用夹具
    from app.agent.insights.service import generate_insights  # noqa: PLC0415

    summary = generate_insights(trigger="manual")
    return {
        "status": summary.get("status"),
        "insufficient_sample": summary.get("insufficient_sample"),
        "parse_attempts": summary.get("parse_attempts"),
    }


CALLERS = {
    "topics": call_topics,
    "content": call_content,
    "content_variants": call_content_variants,
    "metric_vision": call_metric_vision,
    "diagnosis": call_diagnosis,
    "weekly": call_weekly,
    "insights": call_insights,
}


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
class FixtureManager:
    """[BENCH-LLM] 夹具：建 → 用 → 双口径清理。"""

    def __init__(self) -> None:
        self.account_id: uuid.UUID | None = None
        self.persona_id: uuid.UUID | None = None
        self.hotspot_id: uuid.UUID | None = None
        self.topic_ids: list[uuid.UUID] = []
        self.note_id: uuid.UUID | None = None
        self.before_threads = _all_thread_ids()
        self.before_runs = _all_run_ids()
        self.before_snippets = _all_snippet_ids()

    # ---- 建 ----
    def build(self, graphs: list[str]) -> dict:
        fixtures: dict = {}
        with SessionLocal() as db:
            account = Account(name=f"{PREFIX} 账号", status=AccountStatus.ACTIVE)
            db.add(account)
            db.flush()
            persona = Persona(
                name=f"{PREFIX} 人设",
                account_id=account.id,
                is_default=False,
                status=PersonaStatus.ACTIVE,
                positioning="基准测试用（不入正式人设组）",
            )
            db.add(persona)
            db.flush()
            self.account_id = account.id
            self.persona_id = persona.id
            fixtures["persona_id"] = persona.id
            fixtures["account_id"] = account.id

            if "topics" in graphs:
                hotspot = Hotspot(
                    source="zhihu",
                    title=f"{PREFIX} 热点标题（基准测试）",
                    hot_score=12345,
                    rank_position=1,
                    hot_date=datetime.now().date(),
                    fetched_at=datetime.now(timezone.utc),
                    content_hash=hashlib.sha256(f"bench-{uuid.uuid4()}".encode()).hexdigest(),
                    status=HotspotStatus.NEW,
                )
                db.add(hotspot)
                db.flush()
                self.hotspot_id = hotspot.id
                fixtures["hotspot_id"] = hotspot.id

            for graph, key in (("content", "content_topics"), ("content_variants", "variant_topics")):
                if graph not in graphs:
                    continue
                ids: list[uuid.UUID] = []
                for index in range(ROUNDS_DEFAULT):
                    topic = Topic(
                        account_id=account.id,
                        persona_id=persona.id,
                        title=f"{PREFIX} {graph} 选题 {index + 1}",
                        angle="基准测试角度：面向新手的实用清单",
                        summary="基准测试用选题（由 benchmark 夹具生成）",
                        content_outline=["背景", "要点", "总结"],
                        target_keywords=["基准测试"],
                        difficulty=None,
                        status=TopicStatus.APPROVED,
                        generated_at=datetime.now(timezone.utc),
                    )
                    db.add(topic)
                    db.flush()
                    ids.append(topic.id)
                fixtures[key] = ids
                self.topic_ids.extend(ids)

            if "diagnosis" in graphs:
                note = Note(
                    account_id=account.id,
                    persona_id=persona.id,
                    title=f"{PREFIX} 诊断用笔记",
                    body="基准测试正文（diagnosis 图的输入夹具）。",
                    tags=["基准测试"],
                    status=NoteStatus.PUBLISHED,
                    compliance_status=ComplianceStatus.PASSED,
                    published_at=datetime.now(timezone.utc),
                )
                db.add(note)
                db.flush()
                base_time = datetime.now(timezone.utc)
                for offset, factor in ((3, 0.4), (0, 1.0)):
                    db.add(
                        NoteMetric(
                            note_id=note.id,
                            captured_at=base_time - timedelta(days=offset),
                            data_source=DataSource.MANUAL,
                            views=int(1200 * factor),
                            likes=int(180 * factor),
                            collects=int(60 * factor),
                            comments=int(24 * factor),
                            shares=int(8 * factor),
                            new_followers=int(15 * factor),
                            evidence={"benchmark": True},
                        )
                    )
                self.note_id = note.id
                fixtures["note_id"] = note.id
            db.commit()

        # 视觉夹具（合成截图；由 bench_vision_synth --generate-fixtures 产出）
        if "metric_vision" in graphs:
            manifest_path = bc.FIXTURES_DIR / "vision_synth" / "manifest.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                entry = (manifest.get("fixtures") or [None])[0]
                if entry:
                    image_path = bc.BENCH_ROOT / entry["path"]
                    fixtures["vision_image"] = (image_path.read_bytes(), image_path.name)
        return fixtures

    # ---- 清 ----
    def _prefix_sweep(self) -> dict:
        """按前缀兜底扫（防历史崩溃留下的孤儿），同样先子后父。"""
        deleted = {"notes": 0, "note_metrics": 0, "diagnostics": 0, "topics": 0, "hotspots": 0}
        with SessionLocal() as db:
            note_ids = [
                str(row[0])
                for row in db.execute(
                    text("SELECT id FROM notes WHERE title LIKE :p"), {"p": f"{PREFIX}%"}
                ).all()
            ]
            if note_ids:
                deleted["diagnostics"] = db.execute(
                    text("DELETE FROM diagnostics WHERE note_id = ANY(:ids)"), {"ids": note_ids}
                ).rowcount or 0
                deleted["note_metrics"] = db.execute(
                    text("DELETE FROM note_metrics WHERE note_id = ANY(:ids)"), {"ids": note_ids}
                ).rowcount or 0
                deleted["notes"] = db.execute(
                    text("DELETE FROM notes WHERE id = ANY(:ids)"), {"ids": note_ids}
                ).rowcount or 0
            deleted["topics"] = db.execute(
                text("DELETE FROM topics WHERE title LIKE :p"), {"p": f"{PREFIX}%"}
            ).rowcount or 0
            deleted["hotspots"] = db.execute(
                text("DELETE FROM hotspots WHERE title LIKE :p"), {"p": f"{PREFIX}%"}
            ).rowcount or 0
            db.commit()
        return deleted

    def cleanup(self) -> dict:
        """按 ``account_id`` 关联删除（先子后父），删前计数、删后双口径复查。

        历史缺陷：旧实现按 ``notes.title LIKE '[BENCH-LLM]%'`` 删笔记，而
        content / content_variants 的笔记标题由模型生成、不带前缀 → 删 ``accounts``
        时被 ``notes.account_id``（ON DELETE RESTRICT）挡住 → 异常冒泡、结果文件
        根本没写出。改为「账号即边界」后，标题长什么样都不影响清理。
        """
        result: dict = {
            "deleted": {},
            "pre_counts": {},
            "residue": {},
            "residue_by_account": {},
            "residue_clean": False,
        }
        aid = str(self.account_id) if self.account_id else None

        # 1) 线程（差集）
        new_threads = _all_thread_ids() - self.before_threads
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
        result["deleted"]["threads"] = len(new_threads)

        # 2) 审计（差集；llm_raw 已先落盘，此处删的是本次运行痕迹）
        new_runs = _all_run_ids() - self.before_runs
        if new_runs:
            with SessionLocal() as db:
                db.execute(text("DELETE FROM agent_runs WHERE id = ANY(:ids)"), {"ids": list(new_runs)})
                db.commit()
        result["deleted"]["runs"] = len(new_runs)

        # 3) 业务数据：account 即边界；diagnostics → note_metrics → notes → topics
        #    → personas → hotspots → accounts（与被引用顺序相反）
        if aid:
            with SessionLocal() as db:
                note_ids = [
                    str(row[0])
                    for row in db.execute(
                        text("SELECT id FROM notes WHERE account_id = :aid"), {"aid": aid}
                    ).all()
                ]
                topic_ids = [
                    str(row[0])
                    for row in db.execute(
                        text("SELECT id FROM topics WHERE account_id = :aid"), {"aid": aid}
                    ).all()
                ]
                pre = {
                    "notes": len(note_ids),
                    "topics": len(topic_ids),
                    "personas": int(
                        db.execute(
                            text("SELECT count(*) FROM personas WHERE account_id = :aid"), {"aid": aid}
                        ).scalar_one()
                    ),
                }
                pre["note_metrics"] = int(
                    db.execute(
                        text("SELECT count(*) FROM note_metrics WHERE note_id = ANY(:ids)"),
                        {"ids": note_ids},
                    ).scalar_one()
                ) if note_ids else 0
                pre["diagnostics"] = int(
                    db.execute(
                        text("SELECT count(*) FROM diagnostics WHERE note_id = ANY(:ids)"),
                        {"ids": note_ids},
                    ).scalar_one()
                ) if note_ids else 0
                result["pre_counts"] = pre

                if note_ids:
                    result["deleted"]["diagnostics"] = db.execute(
                        text("DELETE FROM diagnostics WHERE note_id = ANY(:ids)"), {"ids": note_ids}
                    ).rowcount or 0
                    result["deleted"]["note_metrics"] = db.execute(
                        text("DELETE FROM note_metrics WHERE note_id = ANY(:ids)"), {"ids": note_ids}
                    ).rowcount or 0
                result["deleted"]["notes"] = db.execute(
                    text("DELETE FROM notes WHERE account_id = :aid"), {"aid": aid}
                ).rowcount or 0
                result["deleted"]["topics"] = db.execute(
                    text("DELETE FROM topics WHERE account_id = :aid"), {"aid": aid}
                ).rowcount or 0
                result["deleted"]["personas"] = db.execute(
                    text("DELETE FROM personas WHERE account_id = :aid"), {"aid": aid}
                ).rowcount or 0
                if self.hotspot_id is not None:
                    result["deleted"]["hotspots"] = db.execute(
                        text("DELETE FROM hotspots WHERE id = :hid"), {"hid": str(self.hotspot_id)}
                    ).rowcount or 0
                result["deleted"]["accounts"] = db.execute(
                    text("DELETE FROM accounts WHERE id = :aid"), {"aid": aid}
                ).rowcount or 0
                db.commit()

        # 4) 前缀兜底扫（无账号归属的孤儿；正常为 0）
        result["deleted"]["prefix_sweep"] = self._prefix_sweep()

        # 5) insights 可能落库的知识块（差集）
        new_snippets = _all_snippet_ids() - self.before_snippets
        if new_snippets:
            with SessionLocal() as db:
                db.execute(text("DELETE FROM knowledge_snippets WHERE id = ANY(:ids)"), {"ids": list(new_snippets)})
                db.commit()
        result["deleted"]["knowledge_snippets"] = len(new_snippets)

        # 6) 双口径残留复查：① 按 account_id（含子表 join）② 按前缀
        by_account, by_prefix = self._residue_checks()
        result["residue"] = by_prefix
        result["residue_by_account"] = by_account
        result["residue_clean"] = all(
            value == 0
            for block in (by_prefix, by_account)
            for value in block.values()
        )
        return result

    def _residue_checks(self) -> tuple[dict, dict]:
        """只读复查（清理异常时也能用来判断真实残留）。"""
        aid = str(self.account_id) if self.account_id else None
        by_account: dict = {}
        by_prefix: dict = {}
        with SessionLocal() as db:
            if aid:
                checks_by_account = {
                    "notes": "SELECT count(*) FROM notes WHERE account_id = :aid",
                    "topics": "SELECT count(*) FROM topics WHERE account_id = :aid",
                    "personas": "SELECT count(*) FROM personas WHERE account_id = :aid",
                    "note_metrics": (
                        "SELECT count(*) FROM note_metrics WHERE note_id IN "
                        "(SELECT id FROM notes WHERE account_id = :aid)"
                    ),
                    "diagnostics": (
                        "SELECT count(*) FROM diagnostics WHERE note_id IN "
                        "(SELECT id FROM notes WHERE account_id = :aid)"
                    ),
                }
                for key, sql in checks_by_account.items():
                    by_account[key] = int(db.execute(text(sql), {"aid": aid}).scalar_one())
            checks_by_prefix = {
                "accounts": "SELECT count(*) FROM accounts WHERE name LIKE :p",
                "personas": "SELECT count(*) FROM personas WHERE name LIKE :p",
                "topics": "SELECT count(*) FROM topics WHERE title LIKE :p",
                "notes": "SELECT count(*) FROM notes WHERE title LIKE :p",
                "hotspots": "SELECT count(*) FROM hotspots WHERE title LIKE :p",
            }
            for key, sql in checks_by_prefix.items():
                by_prefix[key] = int(db.execute(text(sql), {"p": f"{PREFIX}%"}).scalar_one())
        return by_account, by_prefix


# ---------------------------------------------------------------------------
# 一轮执行
# ---------------------------------------------------------------------------
def run_round(graph: str, fixtures: dict) -> dict:
    before = _all_run_ids()
    started = time.perf_counter()
    error = None
    meta: dict = {}
    try:
        meta = CALLERS[graph](fixtures) or {}
    except (AppError, AgentConfigError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - 如实记录，不中断其余轮次
        error = f"{type(exc).__name__}: {exc}"
    wall_ms = round((time.perf_counter() - started) * 1000, 1)

    new_runs = _all_run_ids() - before
    rows = _fetch_runs(sorted(new_runs))
    parse_attempts = [row["json_parse_attempts"] for row in rows]
    statuses = [row["status"] for row in rows]
    tokens = {
        "prompt": sum(int(row["prompt_tokens"] or 0) for row in rows),
        "completion": sum(int(row["completion_tokens"] or 0) for row in rows),
        "total": sum(int(row["total_tokens"] or 0) for row in rows),
    }
    first_try = bool(rows) and all(item == 1 for item in parse_attempts) and all(
        item == "success" for item in statuses
    )
    raw_runs = [_analyze_raw(graph, row) for row in rows]
    llm_called = any(item["llm_called"] for item in raw_runs)
    return {
        "graph": graph,
        "ok": error is None,
        "error": error,
        "wall_ms": wall_ms,
        "new_runs": sorted(new_runs),
        "run_count": len(rows),
        "parse_attempts": parse_attempts,
        "statuses": statuses,
        "tokens": tokens,
        "first_try": first_try,
        "llm_called": llm_called,
        "zero_call": not llm_called,
        "meta": meta,
        # raw 复算（逐 run；不保留原文）
        "raw_runs": raw_runs,
    }


def summarize(rounds: list[dict]) -> dict:
    # 「有真实调用」用证据口径（attempts>0 或 tokens>0），不是「建了 run 行」
    called = [item for item in rounds if item.get("llm_called")]
    latencies = [item["wall_ms"] for item in called]
    totals = [item["tokens"]["total"] for item in called]
    latency_stats = bc.stat_block(latencies)
    token_stats = bc.stat_block(totals)
    repair_rounds = [
        item for item in called if any(attempt and attempt > 1 for attempt in item["parse_attempts"])
    ]
    failed_after_repair = [
        item
        for item in repair_rounds
        if not (item["statuses"] and all(status == "success" for status in item["statuses"]))
    ]
    return {
        "rounds": len(rounds),
        "rounds_with_calls": len(called),
        "first_try_rate": (
            round(sum(1 for item in called if item["first_try"]) / len(called), 4) if called else None
        ),
        "success_rate": (
            round(sum(1 for item in called if item["statuses"] and all(s == "success" for s in item["statuses"])) / len(called), 4)
            if called else None
        ),
        "repair_needed_rounds": len(repair_rounds),
        "failed_after_repair_rate": (
            round(len(failed_after_repair) / len(repair_rounds), 4) if repair_rounds else None
        ),
        "latency_ms": latency_stats,
        "tokens_per_round": token_stats,
        "tokens_grand_total": sum(totals),
        "zero_call_rounds": sum(1 for item in rounds if item.get("zero_call")),
        "error_rounds": sum(1 for item in rounds if item["error"]),
        "raw": _raw_summary(rounds),
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def estimate_block(graphs: list[str], rounds: int) -> str:
    requests = len(graphs) * rounds
    return (
        f"计划：{len(graphs)} 个图 × {rounds} 轮 = 约 {requests} 次模型调用"
        f"（若遇 JSON 回炉自动追加，单轮上限 3 次 → 理论上限 ~{requests * 3} 次）；\n"
        "token 粗估：按每轮约 1k~6k token 量级（因图与输出长度而异），"
        f"合计约 {requests} k ~ {requests * 6} k token 量级 —— **粗估、非承诺**；"
        "精确用量运行后从 agent_runs 统计（本仓库不内置价格表）。"
    )


def _write_and_sync(**kwargs) -> Path:
    """写结果文件并 fsync：**先落盘，后清理**。"""
    path = bc.write_suite_result(**kwargs)
    # Windows 下只读句柄 fsync 会报 EBADF，必须以可写方式打开（追加模式不改内容）
    with open(path, "ab") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _safe_cleanup(manager: FixtureManager) -> dict:
    """清理绝不抛：异常单独标记，并用只读复查判断真实残留。"""
    try:
        return manager.cleanup()
    except Exception as exc:  # noqa: BLE001 —— 清理失败不影响已落盘的结果文件
        error = f"{type(exc).__name__}: {exc}"
        print(f"[清理] ⚠️ 清理异常（结果文件已先落盘）：{error}")
        try:
            by_account, by_prefix = manager._residue_checks()
        except Exception:  # noqa: BLE001
            by_account, by_prefix = {}, {}
        return {
            "deleted": {},
            "pre_counts": {},
            "residue": by_prefix,
            "residue_by_account": by_account,
            "residue_clean": bool(by_prefix or by_account)
            and all(
                value == 0
                for block in (by_prefix, by_account)
                for value in block.values()
            ),
            "cleanup_error": error,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="真实 LLM 稳定性与成本（benchmark 套件）")
    parser.add_argument("--graphs", default=",".join(GRAPH_ORDER), help="逗号分隔的子集，默认全部 7 个")
    parser.add_argument("--n", type=int, default=ROUNDS_DEFAULT, help="每图轮数（默认 5）")
    parser.add_argument("--dry-run", action="store_true", help="只准备/清理夹具并打印估算，绝不调用模型")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认（脚本化）")
    args = parser.parse_args()

    bc.ensure_dirs()
    bc.print_banner("度量专项 · 阶段二：真实 LLM 稳定性（默认不发请求）")

    graphs = [item.strip() for item in args.graphs.split(",") if item.strip()]
    unknown = [item for item in graphs if item not in CALLERS]
    if unknown or not graphs:
        print(f"[参数] 未知图：{unknown}；可选：{', '.join(GRAPH_ORDER)}")
        return bc.EXIT_ENV
    rounds_n = max(1, int(args.n))

    readiness = llm_factory.describe_llm_readiness()
    print(f"[就绪] {json.dumps(readiness, ensure_ascii=False)}")

    # ---- 安全闸门：未配置 → 退出码 2（零请求）----
    if not readiness.get("ready"):
        print("[闸门] LLM 未配置（开关 / key / 模型名），本脚本不发出任何请求。")
        print("       请在 backend\\.env 配置 AGENT_LLM_ENABLED=true 与 LLM_API_KEY / LLM_MODEL 后重跑。")
        return bc.EXIT_GATED

    print(estimate_block(graphs, rounds_n))

    if args.dry_run:
        print("[模式] --dry-run：仅准备/清理夹具，验证接线；**不会调用任何模型**。")
    elif not args.yes:
        if not sys.stdin.isatty():
            print("[确认] 非交互终端：请加 --yes 明确授权真实调用，或改为 --dry-run。已停止（零请求）。")
            return bc.EXIT_ENV
        try:
            answer = input("确认向 DeepSeek 发起上述真实调用？输入 yes 继续：").strip().lower()
        except EOFError:
            print("[确认] 标准输入不可读（EOF）：视为未确认。已停止（零请求）。")
            return bc.EXIT_ENV
        if answer != "yes":
            print("[确认] 未确认，已停止（零请求）。")
            return bc.EXIT_GATED

    fixtures_mgr = FixtureManager()
    detail: dict = {"graphs": {}, "cleanup": None, "mode": "dry-run" if args.dry_run else "real"}
    metrics: list[dict] = []
    failed = False

    try:
        fixtures = fixtures_mgr.build(graphs)
        missing_vision = "metric_vision" in graphs and "vision_image" not in fixtures
        if missing_vision:
            print("[夹具] 缺少截图夹具（先运行 bench_vision_synth.py --generate-fixtures）；本图跳过。")
            graphs = [item for item in graphs if item != "metric_vision"]
        print(f"[夹具] 已就绪（账号/人设/热点/选题/笔记/截图按需创建；前缀 {PREFIX}）")

        if args.dry_run:
            for graph in graphs:
                detail["graphs"][graph] = {"dry_run": True, "fixtures_ok": True}
        else:
            for graph in graphs:
                rounds: list[dict] = []
                for index in range(1, rounds_n + 1):
                    record = run_round(graph, fixtures)
                    rounds.append(record)
                    flag = "OK" if record["ok"] and (record["new_runs"] or record["zero_call"]) else "FAIL"
                    print(
                        f"  [{GRAPH_LABELS[graph]}] 第 {index}/{rounds_n} 轮 {flag}  "
                        f"runs={len(record['new_runs'])} calls={'Y' if record['llm_called'] else 'N'} "
                        f"parse={record['parse_attempts']} "
                        f"tokens={record['tokens']['total']} 用时={record['wall_ms']}ms"
                        + (f"  error={record['error']}" if record["error"] else "")
                    )
                summary = summarize(rounds)
                detail["graphs"][graph] = {"rounds": rounds, "summary": summary}
                if summary["error_rounds"] and not summary["rounds_with_calls"]:
                    failed = True
    except Exception as exc:  # noqa: BLE001 —— 运行期异常也要保住结果文件与清理
        detail["run_error"] = f"{type(exc).__name__}: {exc}"
        failed = True
        print(f"[运行] ⚠️ 异常中断：{detail['run_error']}")
    finally:
        try:
            close_checkpointer()
        except Exception:  # noqa: BLE001
            pass

    # ---- 指标与落盘 ----
    if not args.dry_run:
        for graph, payload in detail["graphs"].items():
            summary = payload["summary"]
            called = summary["rounds_with_calls"]
            metrics.append(
                bc.metric(
                    f"llm_stability.{graph}.rounds_total", summary["rounds"], "轮", n=summary["rounds"],
                    category=bc.CATEGORY_LLM_SAMPLE, method=METHOD + f"；图={graph}",
                )
            )
            if called:
                metrics.extend(
                    [
                        bc.metric(
                            f"llm_stability.{graph}.first_try_rate", summary["first_try_rate"], "比率",
                            n=called, category=bc.CATEGORY_LLM_SAMPLE, method=METHOD + f"；图={graph}",
                            confidence="medium", note=f"n={called} 抽样（{rounds_n} 轮中 {called} 轮有真实调用），不得外推",
                        ),
                        bc.metric(
                            f"llm_stability.{graph}.success_rate", summary["success_rate"], "比率",
                            n=called, category=bc.CATEGORY_LLM_SAMPLE, method=METHOD + f"；图={graph}",
                            confidence="medium", note="以 agent_runs.status 计",
                        ),
                        bc.metric(
                            f"llm_stability.{graph}.latency_p50_ms",
                            (summary["latency_ms"] or {}).get("p50"), "毫秒", n=called,
                            category=bc.CATEGORY_LLM_SAMPLE, method=METHOD + f"；图={graph}",
                            confidence="medium", note="墙钟（含网络）；n=5 级小样本",
                        ),
                        bc.metric(
                            f"llm_stability.{graph}.latency_p95_ms",
                            (summary["latency_ms"] or {}).get("p95"), "毫秒", n=called,
                            category=bc.CATEGORY_LLM_SAMPLE, method=METHOD + f"；图={graph}",
                            confidence="medium", note="nearest-rank；n=5 级小样本",
                        ),
                        bc.metric(
                            f"llm_stability.{graph}.tokens_p50", (summary["tokens_per_round"] or {}).get("p50"),
                            "token/轮", n=called, category=bc.CATEGORY_LLM_SAMPLE,
                            method=METHOD + f"；图={graph}", confidence="medium", note="含 prompt+completion",
                        ),
                        bc.metric(
                            f"llm_stability.{graph}.tokens_p95", (summary["tokens_per_round"] or {}).get("p95"),
                            "token/轮", n=called, category=bc.CATEGORY_LLM_SAMPLE,
                            method=METHOD + f"；图={graph}", confidence="medium",
                        ),
                        bc.metric(
                            f"llm_stability.{graph}.tokens_grand_total", summary["tokens_grand_total"],
                            "token", n=called, category=bc.CATEGORY_LLM_SAMPLE,
                            method=METHOD + f"；图={graph}", note="本次运行该图合计消耗",
                        ),
                        bc.metric(
                            f"llm_stability.{graph}.repair_needed_rounds", summary["repair_needed_rounds"],
                            "轮", n=called, category=bc.CATEGORY_LLM_SAMPLE,
                            method=METHOD + f"；图={graph}",
                            note="发生了解析重问（parse_attempts>1）的轮次数",
                        ),
                        bc.metric(
                            f"llm_stability.{graph}.failed_after_repair_rate",
                            summary["failed_after_repair_rate"], "比率",
                            n=summary["repair_needed_rounds"] or None,
                            category=bc.CATEGORY_LLM_SAMPLE, method=METHOD + f"；图={graph}",
                            note="分母=发生解析重问的轮次数；无重问时 N/A（不虚报 0）",
                        ),
                    ]
                )
            # ---- raw 复算 / schema 契约（新增；分母口径显式） ----
            raw = summary["raw"]
            if raw["raw_runs_total"]:
                metrics.extend(
                    [
                        bc.metric(
                            f"llm_stability.{graph}.raw_runs_total", raw["raw_runs_total"], "run",
                            n=raw["raw_runs_total"], category=bc.CATEGORY_LLM_SAMPLE,
                            method=METHOD + f"；图={graph}",
                            note="有真实调用的 run 数（raw 复算的总分母）",
                        ),
                        bc.metric(
                            f"llm_stability.{graph}.raw_missing", raw["raw_missing"], "run",
                            n=raw["raw_runs_total"], category=bc.CATEGORY_LLM_SAMPLE,
                            method=METHOD + f"；图={graph}",
                            note="有真实调用但 llm_raw 未落库的 run 数（单列，不静默缩小分母）",
                        ),
                        bc.metric(
                            f"llm_stability.{graph}.raw_direct_parse_rate",
                            raw["raw_direct_parse_rate"], "比率", n=raw["raw_sample_n"],
                            category=bc.CATEGORY_LLM_SAMPLE, method=METHOD + f"；图={graph}",
                            confidence="medium",
                            note=(
                                f"分母=可得 raw 样本 {raw['raw_sample_n']}"
                                f"（总调用 {raw['raw_runs_total']}，缺失 {raw['raw_missing']}）；"
                                "原文=json.loads 直接成功（内嵌控制字符修复仍计原文）"
                            ),
                        ),
                        bc.metric(
                            f"llm_stability.{graph}.contract_pass_rate",
                            raw["contract_pass_rate"], "比率", n=raw["raw_sample_n"],
                            category=bc.CATEGORY_LLM_SAMPLE, method=METHOD + f"；图={graph}",
                            confidence="medium",
                            note=(
                                "schema 字段级契约（复用各图自己的 Pydantic 契约 / "
                                "normalize_* 纯函数，不另写一套规则）"
                            ),
                        ),
                    ]
                )
            if summary["zero_call_rounds"]:
                metrics.append(
                    bc.metric(
                        f"llm_stability.{graph}.zero_call_rounds", summary["zero_call_rounds"], "轮",
                        n=summary["rounds"], category=bc.CATEGORY_LLM_SAMPLE,
                        method=METHOD + f"；图={graph}",
                        note="样本不足 → 图内零调用零落库（如实记录，未消耗 token）",
                    )
                )

        # ---- 跨图合计（n = 全部有真实调用的轮次）----
        all_called = [
            record
            for payload in detail["graphs"].values()
            for record in payload["rounds"]
            if record.get("llm_called")
        ]
        if all_called:
            total_latency = bc.stat_block([record["wall_ms"] for record in all_called])
            total_repair = [
                record
                for record in all_called
                if any(attempt and attempt > 1 for attempt in record["parse_attempts"])
            ]
            total_failed_repair = [
                record
                for record in total_repair
                if not (record["statuses"] and all(s == "success" for s in record["statuses"]))
            ]
            total_first = round(
                sum(1 for record in all_called if record["first_try"]) / len(all_called), 4
            )
            total_success = round(
                sum(
                    1
                    for record in all_called
                    if record["statuses"] and all(s == "success" for s in record["statuses"])
                )
                / len(all_called),
                4,
            )
            for name, value, unit, n_value, note in [
                ("llm_stability.total.rounds_with_calls", len(all_called), "轮", len(all_called),
                 "全部图合计的有真实调用轮次（样本量）"),
                ("llm_stability.total.first_try_rate", total_first, "比率", len(all_called),
                 "全部轮次中「一次解析且契约通过」占比（n=全体抽样轮次）"),
                ("llm_stability.total.success_rate", total_success, "比率", len(all_called),
                 "最终契约通过率（含重问修复后成功）"),
                ("llm_stability.total.failed_after_repair_rate",
                 (round(len(total_failed_repair) / len(total_repair), 4) if total_repair else None),
                 "比率", len(total_repair) or None,
                 "分母=全部发生解析重问的轮次；无重问时 N/A"),
                ("llm_stability.total.latency_p50_ms", (total_latency or {}).get("p50"), "毫秒",
                 len(all_called), "墙钟含网络；n 级小样本"),
                ("llm_stability.total.latency_p95_ms", (total_latency or {}).get("p95"), "毫秒",
                 len(all_called), "nearest-rank"),
                ("llm_stability.total.tokens_grand_total",
                 sum(record["tokens"]["total"] for record in all_called), "token", len(all_called),
                 "本次运行全部图的 token 合计（按量记录，无价格表）"),
            ]:
                metrics.append(
                    bc.metric(
                        name, value, unit, n=n_value, category=bc.CATEGORY_LLM_SAMPLE,
                        method=METHOD + "；跨图合计", note=note, confidence="medium",
                    )
                )

        # ---- 跨图 raw / 契约合计（分母口径显式）----
        total_raw = _raw_summary(all_called)
        total_success_final = (
            round(
                sum(
                    1
                    for record in all_called
                    if record["statuses"] and all(s == "success" for s in record["statuses"])
                )
                / len(all_called),
                4,
            )
            if all_called
            else None
        )
        detail["raw_analysis"] = {
            **total_raw,
            "contract_equals_success": (
                total_raw["contract_pass_rate"] is not None
                and total_raw["contract_pass_rate"] == total_success_final
            ),
        }
        if total_raw["raw_runs_total"]:
            metrics.extend(
                [
                    bc.metric(
                        "llm_stability.total.raw_runs_total", total_raw["raw_runs_total"], "run",
                        n=total_raw["raw_runs_total"], category=bc.CATEGORY_LLM_SAMPLE,
                        method=METHOD + "；跨图合计", note="全部图有真实调用的 run 数",
                    ),
                    bc.metric(
                        "llm_stability.total.raw_missing", total_raw["raw_missing"], "run",
                        n=total_raw["raw_runs_total"], category=bc.CATEGORY_LLM_SAMPLE,
                        method=METHOD + "；跨图合计",
                        note="有真实调用但 llm_raw 未落库的 run 数（单列，不缩减分母）",
                    ),
                    bc.metric(
                        "llm_stability.total.raw_direct_parse_rate",
                        total_raw["raw_direct_parse_rate"], "比率", n=total_raw["raw_sample_n"],
                        category=bc.CATEGORY_LLM_SAMPLE, method=METHOD + "；跨图合计",
                        confidence="medium",
                        note=(
                            f"分母=可得 raw 样本 {total_raw['raw_sample_n']}"
                            f"（总调用 {total_raw['raw_runs_total']}，缺失 {total_raw['raw_missing']}）；"
                            "原文=json.loads 直接成功（内嵌控制字符修复仍计原文）"
                        ),
                    ),
                    bc.metric(
                        "llm_stability.total.contract_pass_rate",
                        total_raw["contract_pass_rate"], "比率", n=total_raw["raw_sample_n"],
                        category=bc.CATEGORY_LLM_SAMPLE, method=METHOD + "；跨图合计",
                        confidence="medium",
                        note=(
                            "schema 字段级契约通过率；"
                            + (
                                f"本样本下与 success_rate 数值等价（均 {total_success_final}）"
                                if detail["raw_analysis"]["contract_equals_success"]
                                else f"本样本下 success_rate={total_success_final}，两者不等价"
                            )
                        ),
                    ),
                ]
            )

        # ---- 模型与环境记录（不含任何密钥）----
        from app.core.config import settings as _settings  # noqa: PLC0415

        llm_env = {
            "llm_model": _settings.LLM_MODEL,
            "llm_base_url": _settings.LLM_BASE_URL,
            "llm_temperature": "factory 默认 0.2（结构化抽取建议低温度）",
            "run_finished_at": bc.now_iso(),
        }
        write_args = dict(
            filename="llm_stability.json",
            suite="llm_stability",
            metrics=metrics,
            detail=detail,
            summary={
                "mode": "real", "graphs": graphs, "rounds": rounds_n, **llm_env,
                "raw_runs_total": total_raw["raw_runs_total"],
                "raw_missing": total_raw["raw_missing"],
                "raw_sample_n": total_raw["raw_sample_n"],
                "raw_direct_parse_rate": total_raw["raw_direct_parse_rate"],
                "contract_pass_rate": total_raw["contract_pass_rate"],
            },
            env_extra=llm_env,
            notes=[
                "n=5 小样本抽样；结论只能写「抽样中」口径，禁止外推为总体稳定性 / 准确率。",
                "first_try_rate 是 attempts 口径（模型调用轮次），不等于 raw 直解率；"
                "raw_direct_parse_rate 由 llm_raw 离线复算单列。",
                "raw_missing 单列：raw 类指标的分母 = 可得样本数（总调用 - 缺失），"
                "并在指标 note 中同时给出总调用数与缺失数，不静默缩小分母。",
                "contract_pass_rate = 原始 JSON 按各图自身 Pydantic 契约 / normalize_* 校验；"
                "与 success_rate 是否等价见 detail.raw_analysis.contract_equals_success。",
                "token 三列来自 agent_runs（harness 记录），未内置价格。",
                "夹具与审计按 account_id 关联清理（先子后父）+ 前缀兜底；"
                "结果文件先落盘再清理，清理异常记 detail.cleanup.cleanup_error。",
                "「标准答案」= 客观契约（合法 JSON + 字段 schema + 约束），非主观好坏。",
            ],
        )
    else:
        write_args = dict(
            filename="llm_stability_dryrun.json",
            suite="llm_stability(dry-run)",
            metrics=[],
            detail=detail,
            summary={
                "mode": "dry-run", "graphs": graphs, "rounds": rounds_n,
                "requests_planned": len(graphs) * rounds_n,
            },
            notes=[
                "dry-run：仅验证夹具准备/清理与闸门逻辑；未调用任何模型（零请求零 token）。",
                "真实运行：去掉 --dry-run（就绪 + 交互确认 / --yes）。",
            ],
        )

    # ---- ① 先落盘（清理前；写失败也要继续清理）----
    result_path = None
    try:
        result_path = _write_and_sync(**write_args)
        print(f"[落盘] 结果已写出（清理前，已 fsync）：{result_path.relative_to(bc.ROOT)}")
    except Exception as exc:  # noqa: BLE001
        detail["write_error"] = f"{type(exc).__name__}: {exc}"
        failed = True
        print(f"[落盘] ⚠️ 结果写入失败：{detail['write_error']}（仍继续清理）")

    # ---- ② 再清理（异常单独标记，不影响上面的结果文件）----
    cleanup = _safe_cleanup(fixtures_mgr)
    detail["cleanup"] = cleanup
    if cleanup.get("cleanup_error"):
        print(f"[清理] 已标记 cleanup_error（残留复查：{cleanup['residue']} / {cleanup['residue_by_account']}）")
    if not cleanup["residue_clean"]:
        failed = True
        print(
            "[清理] ⚠️ 残留复查未归零："
            f"按前缀={cleanup['residue']} 按账号={cleanup['residue_by_account']}"
        )

    # ---- ③ 二次落盘：补 cleanup 明细（失败保留首版，绝不丢结果）----
    if result_path is not None:
        try:
            _write_and_sync(**write_args)
        except Exception as exc:  # noqa: BLE001
            print(f"[落盘] ⚠️ 补写 cleanup 明细失败（保留首版）：{type(exc).__name__}: {exc}")

    print("-" * 66)
    if result_path is not None:
        print(f"结果：{result_path.relative_to(bc.ROOT)}")
    print(
        f"清理：删除 runs={cleanup['deleted'].get('runs')} 线程={cleanup['deleted'].get('threads')} "
        f"残留复查={'0 ✔' if cleanup['residue_clean'] else cleanup['residue']}"
        + (f" cleanup_error={cleanup['cleanup_error']}" if cleanup.get("cleanup_error") else "")
    )
    return bc.EXIT_FAIL if failed else bc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
