# -*- coding: utf-8 -*-
"""第 26 步 · 飞轮第 1 步：采集（harvest，**只读**）。

从 ``agent_runs`` / ``notes`` / ``topics`` 里捞出可作「进化信号」的样本，
按类型分类、标注目标测试集、写结构化清单。**本脚本只做 SELECT，
不写业务库、不改夹具、不发 LLM 请求。**

信号类型（9 类）：

| kind | 含义 | 目标 |
| --- | --- | --- |
| ``json_fallback`` | 原始输出需去围栏/控制字符/平衡扫描才解析成功 | json_robustness |
| ``json_unrecoverable`` | 有原始输出，但三层兜底全失败 | json_robustness |
| ``json_retry`` | ``json_parse_attempts > 1``（重问后才成功） | json_robustness |
| ``run_failed`` | run 状态 failed | json_robustness（有 raw 才产候选） |
| ``run_partial`` | run 状态 partial | json_robustness（同上） |
| ``compliance_hit`` | 笔记真实内容命中违禁词 | compliance |
| ``tool_error`` | 工具调用 ``ok=false`` | 暂不产候选（缺场景骨架） |
| ``tool_hallucination`` | 请求了白名单外工具 | 暂不产候选（同上） |
| ``topic_discarded`` | 人工废弃的选题 | 暂不产候选（无对应冻结集） |

用法（项目根目录）：

    :: 读真实库（只读，最近 30 天）
    backend\\.venv\\Scripts\\python.exe scripts/evolution/evolve_harvest.py --from-db

    :: 用合成事件（离线验证，绝不碰业务库）
    backend\\.venv\\Scripts\\python.exe scripts/evolution/evolve_harvest.py --from-json D:\\tmp\\events.json

退出码：0 完成；3 环境/参数问题。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evolve_common as ec  # noqa: E402

#: 单条 raw 的存储上限（超长的候选一律跳过，避免把半截文本当样本）
MAX_RAW_CHARS = 4000

#: 单条合规文本上限
MAX_TEXT_CHARS = 500

_DEFERRED_WHY = {
    "tool_error": "topic_tool 场景需要 persona/hotspot 骨架，v1 只登记不产候选",
    "tool_hallucination": "同上：白名单外工具需补完整场景才能成为可复现用例",
    "topic_discarded": "选题质量类信号，当前无对应冻结测试集（阶段 2 再定）",
}


# ---------------------------------------------------------------------------
# 采集源：真实库（只读） / 合成事件 JSON
# ---------------------------------------------------------------------------
def _read_db(days: int, limit: int) -> dict[str, list[dict]]:
    """只读三张表；任何写操作都没有。"""
    ec.add_backend_to_path()
    from sqlalchemy import text  # noqa: PLC0415

    from app.db.session import SessionLocal  # noqa: PLC0415

    days = max(1, int(days))
    limit = max(1, int(limit))
    with SessionLocal() as db:
        runs = [
            dict(row)
            for row in db.execute(
                text(
                    "SELECT id::text AS id, run_type, status, json_parse_attempts, total_tokens, "
                    "llm_raw, error_message, inputs, outputs, created_at "
                    "FROM agent_runs "
                    f"WHERE created_at >= now() - interval '{days} days' "
                    "ORDER BY created_at DESC, id DESC LIMIT :limit"
                ),
                {"limit": limit},
            ).mappings()
        ]
        notes = [
            dict(row)
            for row in db.execute(
                text(
                    "SELECT id::text AS id, title, body, compliance_status, content_check, status, "
                    "created_at FROM notes WHERE content_check IS NOT NULL "
                    "ORDER BY created_at DESC, id DESC LIMIT :limit"
                ),
                {"limit": limit},
            ).mappings()
        ]
        topics = [
            dict(row)
            for row in db.execute(
                text(
                    "SELECT id::text AS id, title, angle, summary, status, review_note, created_at "
                    "FROM topics WHERE status = 'discarded' "
                    "ORDER BY created_at DESC, id DESC LIMIT :limit"
                ),
                {"limit": limit},
            ).mappings()
        ]
    return {"runs": runs, "notes": notes, "topics": topics}


def _read_json_source(path: Path) -> dict[str, list[dict]]:
    payload = ec.read_json(path)
    if not isinstance(payload, dict):
        raise SystemExit(f"[参数错误] {path} 顶层应是对象（含 runs/notes/topics 三个可选键）")
    return {
        "runs": list(payload.get("runs") or []),
        "notes": list(payload.get("notes") or []),
        "topics": list(payload.get("topics") or []),
    }


# ---------------------------------------------------------------------------
# 信号构造
# ---------------------------------------------------------------------------
def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _make_signal(
    *,
    kind: str,
    source_table: str,
    source_id: str,
    run_type: str | None,
    created_at: Any,
    evidence: dict,
    raw: str = "",
    input_payload: dict | None = None,
) -> dict:
    target = ec.SIGNAL_TARGETS.get(kind)
    deferred = None
    if target is None:
        deferred = _DEFERRED_WHY.get(kind, "无对应冻结测试集")
    elif kind in ("run_failed", "run_partial") and not raw.strip():
        target = None
        deferred = "无 llm_raw（非 LLM 通道，或失败发生在拿到输出之前）"
    if raw and len(raw) > MAX_RAW_CHARS:
        target = None
        deferred = f"原始输出 {len(raw)} 字符超过 {MAX_RAW_CHARS} 上限，不做样本（避免半截文本进冻结集）"

    stored_raw, truncated = _truncate(raw, MAX_RAW_CHARS) if raw else ("", False)
    return {
        "signal_id": f"{kind}:{source_id}",
        "kind": kind,
        "source_table": source_table,
        "source_id": str(source_id),
        "run_type": run_type,
        "created_at": str(created_at) if created_at is not None else None,
        "target_testset": target,
        "deferred_reason": deferred,
        "raw": ec.redact(stored_raw) if stored_raw else "",
        "raw_chars": len(raw),
        "raw_truncated": truncated,
        "input_payload": ec.redact(input_payload or {}),
        "evidence": ec.redact(evidence),
    }


def _signals_from_run(run: dict) -> list[dict]:
    ec.add_backend_to_path()
    from app.agent.parsing.json_repair import extract_json_with_source  # noqa: PLC0415

    run_id = str(run.get("id"))
    run_type = run.get("run_type")
    created_at = run.get("created_at")
    raw = run.get("llm_raw") or ""
    status = str(run.get("status") or "").lower()
    attempts = int(run.get("json_parse_attempts") or 0)
    outputs = run.get("outputs") or {}
    tool_block = outputs.get("tool_calling") or {}

    payload = None
    source = ""
    if raw.strip():
        payload, source = extract_json_with_source(raw)

    common = {
        "source_table": "agent_runs",
        "source_id": run_id,
        "run_type": run_type,
        "created_at": created_at,
        "raw": raw,
        "input_payload": run.get("inputs") or {},
    }
    signals: list[dict] = []

    if raw.strip():
        if payload is None:
            signals.append(
                _make_signal(
                    kind="json_unrecoverable",
                    evidence={
                        "source": source,
                        "status": status,
                        "attempts": attempts,
                        "error": run.get("error_message"),
                        "rule": "三层兜底全失败 → 期望行为是「正确拒绝」，不得硬解析",
                    },
                    **common,
                )
            )
        elif source != "原文":
            signals.append(
                _make_signal(
                    kind="json_fallback",
                    evidence={
                        "source": source,
                        "attempts": attempts,
                        "rule": "该形状曾需兜底才解析成功 → 期望行为是「继续救回」",
                    },
                    **common,
                )
            )

    if attempts > 1:
        signals.append(
            _make_signal(
                kind="json_retry",
                evidence={
                    "attempts": attempts,
                    "source": source or None,
                    "status": status,
                    "rule": "重问后才拿到合法 JSON → 回归用例（避免再次退化到多次重问）",
                },
                **common,
            )
        )

    if status == "failed":
        signals.append(
            _make_signal(
                kind="run_failed",
                evidence={
                    "error": run.get("error_message"),
                    "has_raw": bool(raw.strip()),
                    "source": source or None,
                },
                **common,
            )
        )
    elif status == "partial":
        signals.append(
            _make_signal(
                kind="run_partial",
                evidence={"has_raw": bool(raw.strip()), "run_type": run_type},
                **common,
            )
        )

    for call in tool_block.get("calls") or []:
        if call.get("ok") is False:
            signals.append(
                _make_signal(
                    kind="tool_error",
                    evidence={
                        "tool": call.get("tool"),
                        "args": call.get("args"),
                        "error": call.get("error"),
                        "result_summary": call.get("result_summary"),
                        "rounds": tool_block.get("rounds"),
                    },
                    **common,
                )
            )

    hallucinated = tool_block.get("hallucinated") or []
    if hallucinated:
        signals.append(
            _make_signal(
                kind="tool_hallucination",
                evidence={"requested_tools": hallucinated, "rounds": tool_block.get("rounds")},
                **common,
            )
        )
    return signals


def _signals_from_note(note: dict) -> list[dict]:
    check = note.get("content_check") or {}
    hits = check.get("hits") or []
    if not hits:
        return []
    text = (note.get("body") or "").strip() or (note.get("title") or "").strip()
    if not text:
        return []
    text, truncated = _truncate(text, MAX_TEXT_CHARS)
    return [
        _make_signal(
            kind="compliance_hit",
            source_table="notes",
            source_id=str(note.get("id")),
            run_type=None,
            created_at=note.get("created_at"),
            evidence={
                "hits": [{"word": hit.get("word"), "category": hit.get("category")} for hit in hits],
                "compliance_status": note.get("compliance_status"),
                "check_status": check.get("status"),
                "text_truncated": truncated,
            },
            raw=text,
            input_payload={"note_title": note.get("title")},
        )
    ]


def _signals_from_topic(topic: dict) -> list[dict]:
    return [
        _make_signal(
            kind="topic_discarded",
            source_table="topics",
            source_id=str(topic.get("id")),
            run_type=None,
            created_at=topic.get("created_at"),
            evidence={
                "status": topic.get("status"),
                "review_note": topic.get("review_note"),
                "title": topic.get("title"),
                "angle": topic.get("angle"),
                "summary": topic.get("summary"),
            },
        )
    ]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def collect(source: dict[str, list[dict]]) -> dict:
    signals: list[dict] = []
    for run in source["runs"]:
        signals.extend(_signals_from_run(run))
    for note in source["notes"]:
        signals.extend(_signals_from_note(note))
    for topic in source["topics"]:
        signals.extend(_signals_from_topic(topic))

    # 去重（同一 kind + source_id 只留一条）
    seen: set[str] = set()
    unique: list[dict] = []
    for signal in signals:
        if signal["signal_id"] in seen:
            continue
        seen.add(signal["signal_id"])
        unique.append(signal)

    by_kind: dict[str, int] = {}
    by_target: dict[str, int] = {}
    kind_target: dict[str, str | None] = {}
    for signal in unique:
        by_kind[signal["kind"]] = by_kind.get(signal["kind"], 0) + 1
        key = signal["target_testset"] or "(deferred)"
        by_target[key] = by_target.get(key, 0) + 1
        # 同一 kind 可能因证据不足被降级为 deferred，展示时以实际归类为准
        previous = kind_target.get(signal["kind"], "unset")
        current = signal["target_testset"]
        if previous == "unset":
            kind_target[signal["kind"]] = current
        elif previous != current:
            kind_target[signal["kind"]] = current if current else previous

    return {
        "signals_total": len(unique),
        "by_kind": dict(sorted(by_kind.items())),
        "by_target": dict(sorted(by_target.items())),
        "kind_target": dict(sorted(kind_target.items())),
        "deferred_total": sum(1 for item in unique if item["target_testset"] is None),
        "signals": unique,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="飞轮第 1 步：只读采集失败/兜底/越权信号")
    parser.add_argument("--from-db", action="store_true", help="读真实库（只读，默认）")
    parser.add_argument("--from-json", type=Path, default=None, help="用合成事件 JSON 代替数据库")
    parser.add_argument("--days", type=int, default=30, help="读库窗口（天），默认 30")
    parser.add_argument("--limit", type=int, default=500, help="每表最多读多少行，默认 500")
    parser.add_argument("--workdir", type=Path, default=None, help="进化工作目录（默认仓库外 ../evolution）")
    parser.add_argument("--out", type=Path, default=None, help="显式指定输出文件")
    args = parser.parse_args(argv)

    workdir = args.workdir or ec.default_workdir()
    try:
        if args.from_json is not None:
            source = _read_json_source(args.from_json)
            source_tag = f"json:{args.from_json}"
        else:
            source = _read_db(args.days, args.limit)
            source_tag = f"db:last{args.days}d"
    except Exception as exc:  # noqa: BLE001 —— 采集失败必须显式报错，不静默产出空清单
        print(f"[环境错误] 采集失败：{type(exc).__name__}: {exc}")
        return ec.EXIT_ENV

    report = collect(source)
    payload = {
        "harvested_at": ec.now_iso(),
        "source": source_tag,
        "source_counts": {key: len(value) for key, value in source.items()},
        **report,
    }

    out_path = args.out or (ec.ensure_dir(workdir / "harvest") / f"harvest-{_stamp()}.json")
    ec.write_json(out_path, payload)

    print("=" * 78)
    print("  飞轮第 1 步 · 采集（只读）")
    print("=" * 78)
    print(f"来源：{source_tag} ｜ 行数：{payload['source_counts']}")
    print(f"信号总数：{payload['signals_total']}（其中 {payload['deferred_total']} 条无对应冻结集）")
    print("-" * 78)
    print(f"{'kind':<22}{'条数':>6}  目标")
    for kind, count in payload["by_kind"].items():
        print(f"{kind:<22}{count:>6}  {payload['kind_target'].get(kind) or '(deferred)'}")
    print("-" * 78)
    for signal in payload["signals"][:20]:
        mark = signal["target_testset"] or "deferred"
        extra = f" ← {signal['deferred_reason']}" if signal["deferred_reason"] else ""
        print(f"  {signal['signal_id'][:44]:<46} {mark}{extra}")
    if len(payload["signals"]) > 20:
        print(f"  …… 其余 {len(payload['signals']) - 20} 条见清单文件")
    print("-" * 78)
    print(f"清单：{out_path}")
    return ec.EXIT_OK


def _stamp() -> str:
    from datetime import datetime  # noqa: PLC0415

    return datetime.now().strftime("%Y%m%d-%H%M%S")


if __name__ == "__main__":
    sys.exit(main())
