# -*- coding: utf-8 -*-
"""第 25 步 · 选题图「LLM 自主选工具」评测（真实 LLM，小样本方向性）。

对照设计：

- **tool 模式**：``TOPIC_TOOLCALLING_ENABLED=true``，模型先自主决定调哪些只读工具，
  收敛后再走原生成节点；
- **baseline 模式**：开关关闭，与原实现逐字节一致（无工具、1 次生成调用）。

指标（逐场景留档）：

- 工具选择：precision / recall（微观=按调用计数；宏观=按场景平均）、
  白名单外幻觉工具率；
- 效率：平均工具轮数、``max_rounds`` 内收敛率；
- 质量：最终选题 JSON 的 ``json_parse_attempts == 1`` 且成功落库的比例；
- token / 延迟仅工程参考（不进简历）。

ground truth 来自冻结测试集 ``fixtures/testsets/topic_tool/scenarios.json``
（sha256 校验，评测只读不改）。

⚠️ 默认不发任何请求；``--yes`` 才真实调用。未配置 LLM → 退出码 2（零请求）。

用法（项目根目录）：

    :: 只看计划
    backend\\.venv\\Scripts\\python.exe scripts/benchmark/bench_topic_tool_calling.py --dry-run

    :: 真实对照
    backend\\.venv\\Scripts\\python.exe scripts/benchmark/bench_topic_tool_calling.py --yes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import bench_common as bc

bc.add_backend_to_path()

from sqlalchemy import text  # noqa: E402

from app.agent.checkpoint import close_checkpointer  # noqa: E402
from app.agent.llm.factory import describe_llm_readiness  # noqa: E402
from app.agent.topics.service import generate_for_hotspots  # noqa: E402
from app.agent.topics.tool_calling import TOOL_NAMES  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.models.enums import (  # noqa: E402
    AccountStatus,
    HotspotStatus,
    PersonaStatus,
    TopicStatus,
)
from app.models.hotspot import Hotspot  # noqa: E402
from app.models.persona import Persona  # noqa: E402
from app.models.topic import Topic  # noqa: E402

PREFIX = "[BENCH-TOOL]"
SET_NAME = "topic_tool"
RESULT_NAME = "topic_tool_calling.json"
METHOD = (
    "scripts/benchmark/bench_topic_tool_calling.py：冻结 12 场景 × 两种模式"
    "（tool / baseline），同一人设-热点夹具，逐场景读 agent_runs 与工具事件"
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _thread_ids() -> set[str]:
    with SessionLocal() as db:
        try:
            rows = db.execute(text("SELECT DISTINCT thread_id FROM langgraph.checkpoints")).all()
        except Exception:  # noqa: BLE001
            return set()
    return {str(row[0]) for row in rows}


def _run_ids() -> set[str]:
    with SessionLocal() as db:
        return {str(row[0]) for row in db.execute(text("SELECT id FROM agent_runs")).all()}


def _build_fixtures(scenario: dict) -> dict:
    """建账号 / 人设 / 热点 /（可选）历史选题，返回 id 集合。"""
    persona_cfg = scenario["persona"]
    hotspot_cfg = scenario["hotspot"]
    with SessionLocal() as db:
        account = Account(name=f"{PREFIX} {scenario['id']} 账号", status=AccountStatus.ACTIVE)
        db.add(account)
        db.flush()
        persona = Persona(
            name=f"{PREFIX} {scenario['id']} {persona_cfg['name']}",
            account_id=account.id,
            status=PersonaStatus.ACTIVE,
            positioning=persona_cfg["positioning"],
            target_audience=persona_cfg["target_audience"],
            tone=persona_cfg["tone"],
            content_pillars=list(persona_cfg["content_pillars"]),
            taboo=list(persona_cfg.get("taboo") or []),
            signature_style=persona_cfg.get("signature_style"),
        )
        db.add(persona)
        db.flush()
        hotspot = Hotspot(
            source=hotspot_cfg.get("source") or "zhihu",
            title=f"{PREFIX} {hotspot_cfg['title']}",
            hot_score=hotspot_cfg.get("hot_score") or 10000,
            rank_position=1,
            hot_date=datetime.now().date(),
            fetched_at=datetime.now(timezone.utc),
            content_hash=hashlib.sha256(f"tool-{scenario['id']}-{uuid.uuid4()}".encode()).hexdigest(),
            status=HotspotStatus.NEW,
        )
        db.add(hotspot)
        db.flush()
        for index, title in enumerate(scenario.get("existing_topics") or []):
            db.add(
                Topic(
                    account_id=account.id,
                    persona_id=persona.id,
                    title=title,
                    angle="历史选题（评测预置）",
                    summary="评测预置的历史选题，用于 list_topics 场景",
                    content_outline=["背景", "要点", "总结"],
                    target_keywords=["评测"],
                    status=TopicStatus.PUBLISHED if index == 0 else TopicStatus.DRAFT,
                    generated_at=datetime.now(timezone.utc),
                )
            )
        db.commit()
        return {
            "account_id": str(account.id),
            "persona_id": str(persona.id),
            "hotspot_id": str(hotspot.id),
        }


def _cleanup(account_id: str, before_runs: set[str], before_threads: set[str]) -> dict:
    """按 account_id 关联删除 + 双口径复查（与 bench_llm_stability 同范式）。"""
    result: dict = {"deleted": {}, "residue": {}, "residue_by_account": {}}
    new_threads = _thread_ids() - before_threads
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

    new_runs = _run_ids() - before_runs
    if new_runs:
        with SessionLocal() as db:
            db.execute(text("DELETE FROM agent_runs WHERE id = ANY(:ids)"), {"ids": list(new_runs)})
            db.commit()
    result["deleted"]["runs"] = len(new_runs)

    with SessionLocal() as db:
        note_ids = [
            str(row[0])
            for row in db.execute(
                text("SELECT id FROM notes WHERE account_id = :aid"), {"aid": account_id}
            ).all()
        ]
        if note_ids:
            db.execute(text("DELETE FROM diagnostics WHERE note_id = ANY(:ids)"), {"ids": note_ids})
            db.execute(text("DELETE FROM note_metrics WHERE note_id = ANY(:ids)"), {"ids": note_ids})
            db.execute(text("DELETE FROM notes WHERE id = ANY(:ids)"), {"ids": note_ids})
        result["deleted"]["topics"] = db.execute(
            text("DELETE FROM topics WHERE account_id = :aid"), {"aid": account_id}
        ).rowcount or 0
        result["deleted"]["personas"] = db.execute(
            text("DELETE FROM personas WHERE account_id = :aid"), {"aid": account_id}
        ).rowcount or 0
        db.execute(text("DELETE FROM hotspots WHERE title LIKE :p"), {"p": f"{PREFIX}%"})
        result["deleted"]["accounts"] = db.execute(
            text("DELETE FROM accounts WHERE id = :aid"), {"aid": account_id}
        ).rowcount or 0
        db.commit()

    with SessionLocal() as db:
        result["residue"] = {
            "accounts": int(db.execute(text("SELECT count(*) FROM accounts WHERE name LIKE :p"), {"p": f"{PREFIX}%"}).scalar_one()),
            "personas": int(db.execute(text("SELECT count(*) FROM personas WHERE name LIKE :p"), {"p": f"{PREFIX}%"}).scalar_one()),
            "topics": int(db.execute(text("SELECT count(*) FROM topics WHERE title LIKE :p"), {"p": f"{PREFIX}%"}).scalar_one()),
            "hotspots": int(db.execute(text("SELECT count(*) FROM hotspots WHERE title LIKE :p"), {"p": f"{PREFIX}%"}).scalar_one()),
        }
        result["residue_by_account"] = {
            "topics": int(db.execute(text("SELECT count(*) FROM topics WHERE account_id = :aid"), {"aid": account_id}).scalar_one()),
            "personas": int(db.execute(text("SELECT count(*) FROM personas WHERE account_id = :aid"), {"aid": account_id}).scalar_one()),
        }
    return result


# ---------------------------------------------------------------------------
# 单场景执行
# ---------------------------------------------------------------------------
def _read_run(run_id: str) -> dict:
    with SessionLocal() as db:
        row = db.execute(
            text(
                "SELECT status, json_parse_attempts, prompt_tokens, completion_tokens, "
                "total_tokens, outputs FROM agent_runs WHERE id = :rid"
            ),
            {"rid": run_id},
        ).mappings().first()
    return dict(row) if row else {}


def _run_scenario(scenario: dict, *, tool_mode: bool) -> dict:
    before_runs = _run_ids()
    before_threads = _thread_ids()
    fixtures = _build_fixtures(scenario)
    settings.TOPIC_TOOLCALLING_ENABLED = tool_mode
    started = time.perf_counter()
    error = None
    summary: dict = {}
    try:
        summary = generate_for_hotspots(
            persona_id=uuid.UUID(fixtures["persona_id"]),
            hotspot_ids=[uuid.UUID(fixtures["hotspot_id"])],
            per_hotspot=1,
            force=True,
            trigger="manual",
        )
    except Exception as exc:  # noqa: BLE001 —— 单场景失败如实记录，不中断整批
        error = f"{type(exc).__name__}: {exc}"
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    new_runs = sorted(_run_ids() - before_runs)
    run_rows = [_read_run(rid) for rid in new_runs]
    run_rows = [row for row in run_rows if row]
    output = (run_rows[0].get("outputs") if run_rows else None) or {}
    tool_info = output.get("tool_calling") or {}
    predicted = [item.get("tool") for item in (tool_info.get("calls") or [])]
    # 去重保序（同一工具调两次只算一个选择）
    deduped: list[str] = []
    for name in predicted:
        if name and name not in deduped:
            deduped.append(name)

    expected = list(scenario["expected_tools"])
    hit = [name for name in deduped if name in expected]
    precision = round(len(hit) / len(deduped), 4) if deduped else (1.0 if not expected else 0.0)
    recall = round(len(hit) / len(expected), 4) if expected else (1.0 if not deduped else 0.0)

    parse_attempts = run_rows[0].get("json_parse_attempts") if run_rows else None
    persisted = int(output.get("persisted") or 0)
    tokens = sum(int(row.get("total_tokens") or 0) for row in run_rows)

    record = {
        "id": scenario["id"],
        "mode": "tool" if tool_mode else "baseline",
        "expected": expected,
        "predicted": deduped,
        "precision": precision,
        "recall": recall,
        "hallucinated": list(tool_info.get("hallucinated") or []),
        "rounds": int(tool_info.get("rounds") or 0) if tool_mode else 0,
        "converged": persisted >= 1 and (not tool_mode or int(tool_info.get("rounds") or 0) <= settings.TOPIC_TOOLCALLING_MAX_ROUNDS),
        "parse_attempts": parse_attempts,
        "json_first_try": bool(parse_attempts == 1 and persisted >= 1),
        "persisted": persisted,
        "tokens": tokens,
        "latency_ms": elapsed_ms,
        "status": run_rows[0].get("status") if run_rows else None,
        "error": error,
        "tool_events": (tool_info.get("calls") or []) if tool_mode else [],
    }
    cleanup = _cleanup(fixtures["account_id"], before_runs, before_threads)
    record["cleanup_clean"] = all(v == 0 for v in cleanup["residue"].values()) and all(
        v == 0 for v in cleanup["residue_by_account"].values()
    )
    return record


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def _aggregate(records: list[dict], *, tool_mode: bool) -> dict:
    """汇总。``tool_mode=False`` 时**不计算**工具选择类指标（避免空预测得出误导性精确率）。"""
    total_expected = sum(len(item["expected"]) for item in records)
    total_predicted = sum(len(item["predicted"]) for item in records)
    total_hit = sum(len([n for n in item["predicted"] if n in item["expected"]]) for item in records)
    rounds = [item["rounds"] for item in records]
    return {
        "n": len(records),
        "precision_micro": (round(total_hit / total_predicted, 4) if total_predicted else None)
        if tool_mode
        else None,
        "recall_micro": (round(total_hit / total_expected, 4) if total_expected else None)
        if tool_mode
        else None,
        "precision_macro": (
            round(sum(item["precision"] for item in records) / len(records), 4) if records else None
        )
        if tool_mode
        else None,
        "recall_macro": (
            round(sum(item["recall"] for item in records) / len(records), 4) if records else None
        )
        if tool_mode
        else None,
        "hallucination_rate": (
            round(sum(1 for item in records if item["hallucinated"]) / len(records), 4)
            if records
            else None
        )
        if tool_mode
        else None,
        "avg_rounds": (round(sum(rounds) / len(rounds), 2) if rounds else None) if tool_mode else None,
        "convergence_rate": round(sum(1 for item in records if item["converged"]) / len(records), 4) if records else None,
        "json_first_try_rate": round(sum(1 for item in records if item["json_first_try"]) / len(records), 4) if records else None,
        "total_tokens": sum(item["tokens"] for item in records),
        "avg_latency_ms": round(sum(item["latency_ms"] for item in records) / len(records), 1) if records else None,
        "errors": sum(1 for item in records if item["error"]),
        "cleanup_ok": all(item["cleanup_clean"] for item in records),
    }


def _metrics(tool_agg: dict, base_agg: dict) -> list[dict]:
    cat = bc.CATEGORY_LLM_SAMPLE
    rows = [
        ("topic_tool.precision_micro", tool_agg["precision_micro"], "比率", tool_agg["n"], "工具选择：Σ命中/Σ预测"),
        ("topic_tool.recall_micro", tool_agg["recall_micro"], "比率", tool_agg["n"], "工具选择：Σ命中/Σ标注"),
        ("topic_tool.precision_macro", tool_agg["precision_macro"], "比率", tool_agg["n"], "按场景平均"),
        ("topic_tool.recall_macro", tool_agg["recall_macro"], "比率", tool_agg["n"], "按场景平均"),
        ("topic_tool.hallucination_rate", tool_agg["hallucination_rate"], "比率", tool_agg["n"], "白名单外工具请求率"),
        ("topic_tool.avg_rounds", tool_agg["avg_rounds"], "轮", tool_agg["n"], "平均工具执行轮数"),
        ("topic_tool.convergence_rate", tool_agg["convergence_rate"], "比率", tool_agg["n"], "max_rounds 内收敛且落库"),
        ("topic_tool.json_first_try_rate", tool_agg["json_first_try_rate"], "比率", tool_agg["n"], "最终 JSON 一次解析且落库"),
        ("topic_tool.baseline_json_first_try_rate", base_agg["json_first_try_rate"], "比率", base_agg["n"], "关闭开关的对照"),
        ("topic_tool.total_tokens_tool_mode", tool_agg["total_tokens"], "token", tool_agg["n"], "工程参考，不进简历"),
        ("topic_tool.total_tokens_baseline", base_agg["total_tokens"], "token", base_agg["n"], "工程参考，不进简历"),
        ("topic_tool.avg_latency_ms", tool_agg["avg_latency_ms"], "毫秒", tool_agg["n"], "工程参考，不进简历"),
    ]
    return [
        bc.metric(
            name, value, unit, n=n_value, category=cat, method=METHOD,
            confidence="medium", note=note,
        )
        for name, value, unit, n_value, note in rows
    ]


def _write(
    *,
    tool_records: list[dict],
    base_records: list[dict],
    testset: dict,
    max_rounds: int,
) -> Path:
    tool_agg = _aggregate(tool_records, tool_mode=True)
    base_agg = _aggregate(base_records, tool_mode=False)
    return bc.write_suite_result(
        RESULT_NAME,
        suite="topic_tool_calling",
        metrics=_metrics(tool_agg, base_agg),
        detail={
            "testset": testset,
            "tool": tool_records,
            "baseline": base_records,
            "aggregate": {"tool": tool_agg, "baseline": base_agg},
        },
        summary={
            "mode": "real",
            "n_scenarios": len(tool_records),
            "max_rounds": max_rounds,
            "tool": tool_agg,
            "baseline": base_agg,
            "run_finished_at": bc.now_iso(),
        },
        notes=[
            "n=12 小样本、方向性结论，禁止外推为通用工具选择能力。",
            "ground truth 为人工标注的场景应调工具集合（冻结 sha256），不是跑完抄结果。",
            "baseline = TOPIC_TOOLCALLING_ENABLED=false，与原实现逐字节一致。",
            "token / 延迟仅工程参考（本机 + 网络），不进简历。",
            "工具全部只读；白名单外调用被丢弃并计数，未执行任何写操作。",
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="选题工具调用评测（真实 LLM）")
    parser.add_argument("--yes", action="store_true", help="确认发起真实调用")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不发请求")
    parser.add_argument("--reaggregate", action="store_true", help="按现有 detail 重算汇总（零请求）")
    args = parser.parse_args()

    bc.ensure_dirs()
    bc.print_banner("第 25 步 · 选题工具调用评测（默认不发请求）")

    if args.reaggregate:
        path = bc.RESULTS_DIR / RESULT_NAME
        if not path.exists():
            print(f"[失败] 找不到 {path.relative_to(bc.ROOT)}")
            return bc.EXIT_ENV
        data = json.loads(path.read_text(encoding="utf-8"))
        out = _write(
            tool_records=data["detail"]["tool"],
            base_records=data["detail"]["baseline"],
            testset=(data.get("detail") or {}).get("testset") or {},
            max_rounds=int(
                (data.get("summary") or {}).get("max_rounds")
                or settings.TOPIC_TOOLCALLING_MAX_ROUNDS
            ),
        )
        print(f"[重算] 已按原 detail 重算汇总（零请求）：{out.relative_to(bc.ROOT)}")
        return bc.EXIT_OK

    readiness = describe_llm_readiness()
    print(f"[就绪] {json.dumps({k: readiness[k] for k in ('model', 'base_url', 'ready')}, ensure_ascii=False)}")

    manifest, files = bc.load_frozen_testsets(SET_NAME, ["scenarios.json"])
    scenarios = files["scenarios.json"]["scenarios"]
    print(f"[夹具] {SET_NAME} v{manifest['version']}：{len(scenarios)} 场景，sha256 校验通过")

    requests = len(scenarios) * 2
    print(
        f"计划：{len(scenarios)} 场景 × 2 模式（tool / baseline）；\n"
        f"      调用次数：baseline ≈ {len(scenarios)} 次，tool ≈ {len(scenarios)}×(1~4) 次 → "
        f"合计约 {len(scenarios) * 2}~{len(scenarios) * 5} 次；\n"
        "      token 粗估 60k~110k（每场景 tool 3~5k、baseline 1.5~2.5k）——粗估非承诺；\n"
        "      成本 = 输入 token × 输入单价 + 输出 token × 输出单价（本仓库不内置价格表）。"
    )
    if not readiness.get("ready"):
        print("[闸门] LLM 未配置，本脚本不发出任何请求（退出码 2）。")
        return bc.EXIT_GATED
    if args.dry_run:
        print("[模式] --dry-run：只打印计划，未做任何夹具变更、未发请求。")
        return bc.EXIT_OK
    if not args.yes:
        print("[确认] 真实调用需显式 --yes（零请求退出，退出码 3）。")
        return bc.EXIT_ENV

    original_switch = settings.TOPIC_TOOLCALLING_ENABLED
    detail: dict = {"tool": [], "baseline": []}
    try:
        for mode in ("tool", "baseline"):
            print(f"\n=== 模式：{mode} ===")
            for scenario in scenarios:
                record = _run_scenario(scenario, tool_mode=(mode == "tool"))
                detail[mode].append(record)
                print(
                    f"  {record['id']}  期望={record['expected']} 实际={record['predicted']} "
                    f"P={record['precision']} R={record['recall']} rounds={record['rounds']} "
                    f"parse={record['parse_attempts']} tokens={record['tokens']}"
                    + (f"  error={record['error']}" if record["error"] else "")
                )
    finally:
        settings.TOPIC_TOOLCALLING_ENABLED = original_switch
        try:
            close_checkpointer()
        except Exception:  # noqa: BLE001
            pass

    result = _write(
        tool_records=detail["tool"],
        base_records=detail["baseline"],
        testset={"name": SET_NAME, "version": manifest["version"], "sha256": manifest["sha256"]},
        max_rounds=settings.TOPIC_TOOLCALLING_MAX_ROUNDS,
    )
    tool_agg = _aggregate(detail["tool"], tool_mode=True)
    base_agg = _aggregate(detail["baseline"], tool_mode=False)

    print("\n" + "-" * 66)
    print(f"tool     ：P={tool_agg['precision_micro']} R={tool_agg['recall_micro']} "
          f"幻觉率={tool_agg['hallucination_rate']} 轮数={tool_agg['avg_rounds']} "
          f"收敛={tool_agg['convergence_rate']} JSON一次={tool_agg['json_first_try_rate']}")
    print(f"baseline ：JSON一次={base_agg['json_first_try_rate']} 错误={base_agg['errors']}")
    print(f"清理：tool={tool_agg['cleanup_ok']} baseline={base_agg['cleanup_ok']}")
    print(f"结果：{result.relative_to(bc.ROOT)}")
    return bc.EXIT_OK if (tool_agg["errors"] == 0 and base_agg["errors"] == 0) else bc.EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
