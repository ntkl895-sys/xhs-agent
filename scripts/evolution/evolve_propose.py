# -*- coding: utf-8 -*-
"""第 26 步 · 飞轮第 2 步：候选生成（propose，**零 token**）。

把 harvest 清单里的信号整理成「候选评测 case」。本步是**纯规则**：

- 不调 LLM（阶段 2 才考虑用 LLM 归纳候选；届时须先给 dry-run 与 token 估算）；
- 不写夹具、不写业务库，只输出 JSON 到工作目录 ``review/``；
- 每条候选都带 ``expected_source`` 与 ``evidence``，说明「期望结果从哪来」，
  供人工审核时判断——**候选不等于真相**。

期望行为的来源（这是本步最容易被做假的地方，所以显式标注）：

| 信号 | label | expected 来源 |
| --- | --- | --- |
| ``json_fallback`` | repaired | 兜底救回后的解析结果（**需人工核对语义**） |
| ``json_retry`` | direct / repaired | 同上 |
| ``json_unrecoverable`` / ``run_failed`` / ``run_partial`` | failed | 无（期望行为 = 正确拒绝） |
| ``compliance_hit`` | —（expect_hit=true） | 真实命中记录（**需人工确认不是误伤**） |

用法（项目根目录）：

    backend\\.venv\\Scripts\\python.exe scripts/evolution/evolve_propose.py --harvest <harvest.json>

退出码：0 完成；3 参数/环境问题。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evolve_common as ec  # noqa: E402


# ---------------------------------------------------------------------------
# bucket 推断（只用于写进 case 的分类字段，不参与判定）
# ---------------------------------------------------------------------------
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_TRAILING_RE = re.compile(r",\s*[}\]]")
_FENCE_RE = re.compile(r"```")


def _balance(raw: str) -> tuple[int, int, int]:
    """返回 (对象深度, 数组深度, 未闭合引号数)——只做粗判，不追求完备。"""
    depth_obj = depth_arr = 0
    in_string = False
    escaped = False
    for ch in raw:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth_obj += 1
        elif ch == "}":
            depth_obj -= 1
        elif ch == "[":
            depth_arr += 1
        elif ch == "]":
            depth_arr -= 1
    return depth_obj, depth_arr, 1 if in_string else 0


def infer_bucket(raw: str, parsed: Any) -> str:
    """把原始输出归到 8 个既有分桶之一（新增桶会使分桶统计失真，故不新造）。"""
    if parsed is not None:
        try:
            if isinstance(json.loads(raw), (dict, list)):
                return "valid"
        except Exception:  # noqa: BLE001
            pass
    if _FENCE_RE.search(raw):
        return "fence"
    if _CTRL_RE.search(raw) or re.search(r'"[^"\n]*\n[^"]*"', raw):
        return "ctrl"
    if _TRAILING_RE.search(raw):
        return "trailing"
    depth_obj, depth_arr, open_quote = _balance(raw)
    # 截断优先于引号：截断样本几乎一定同时「有未闭合引号」，主因是截断
    if depth_obj > 0 or depth_arr > 0:
        return "truncated"
    if open_quote:
        return "quotes"
    if depth_obj < 0 or depth_arr < 0:
        return "bracket"
    if "'" in raw and '"' not in raw:
        return "quotes"
    return "broken"


# ---------------------------------------------------------------------------
# id 分配
# ---------------------------------------------------------------------------
def _next_id(cases: list[dict], prefix: str) -> str:
    pattern = re.compile(rf"^{prefix}(\d+)$")
    numbers = [int(m.group(1)) for c in cases if (m := pattern.match(str(c.get("id", ""))))]
    return f"{prefix}{(max(numbers) + 1) if numbers else 1:03d}"


# ---------------------------------------------------------------------------
# 单条信号 → 候选
# ---------------------------------------------------------------------------
def _propose_json_candidate(
    signal: dict, cases: list[dict], *, candidate_id: str
) -> tuple[dict | None, str | None]:
    raw = signal.get("raw") or ""
    if not raw.strip():
        return None, "信号没有原始输出"
    if signal.get("raw_truncated"):
        return None, "原始输出被截断，不能当完整样本"

    ec.add_backend_to_path()
    from app.agent.parsing.json_repair import extract_json_with_source  # noqa: PLC0415

    payload, source = extract_json_with_source(raw)
    kind = signal["kind"]
    evidence = signal.get("evidence") or {}

    if kind == "json_fallback":
        label = "repaired"
    elif kind == "json_retry":
        label = "direct" if (evidence.get("source") or "") == "原文" else "repaired"
    else:  # json_unrecoverable / run_failed / run_partial
        label = "failed"

    if label != "failed" and payload is None:
        return None, f"期望 {label}，但把原始输出重新解析后仍失败（证据与期望自相矛盾）"
    if label == "failed" and payload is not None:
        # 说明该信号其实已经能被救回，不是「不可救」，交给 fallback 信号处理
        return None, "期望 failed，但当前能解析成功（信号与实现不一致，需人工确认）"

    bucket = infer_bucket(raw, payload)
    if kind == "json_fallback":
        expected_source = f"兜底救回结果（命中方式：{source}）——需人工核对语义是否正确"
    elif label == "failed":
        expected_source = "无（期望行为=正确拒绝，不得硬解析）"
    else:
        expected_source = "原文直解结果——需人工核对语义是否正确"

    case = {
        "id": _next_id(cases, "json_"),
        "bucket": bucket,
        "raw": raw,
        "label": label,
        "expected": payload if label != "failed" else None,
        "note": (
            f"第 26 步采集：来自 run {signal['source_id'][:8]}（{signal.get('run_type')}），"
            + (
                f"原始输出经「{source}」才解析成功，期望继续救回"
                if label != "failed"
                else "原始输出三层兜底全失败，期望正确拒绝（不得硬解析）"
            )
        ),
    }
    return (
        {
            "candidate_id": candidate_id,
            "signal_id": signal["signal_id"],
            "signal_kind": kind,
            "target_testset": "json_robustness",
            "case": case,
            "expected_source": expected_source,
            "requires_expected": False,
            "source_table": signal.get("source_table"),
            "source_run_id": signal.get("source_id"),
            "evidence": evidence,
            "created_at": ec.now_iso(),
            "status": "pending",
        },
        None,
    )


def _propose_compliance_candidate(
    signal: dict, cases: list[dict], *, candidate_id: str
) -> tuple[dict | None, str | None]:
    text = (signal.get("raw") or "").strip()
    if not text:
        return None, "信号没有可扫描文本"
    evidence = signal.get("evidence") or {}
    words = [hit.get("word") for hit in (evidence.get("hits") or []) if hit.get("word")]
    if not words:
        return None, "命中记录缺少词条，无法生成可核对用例"
    case = {
        "id": _next_id(cases, "cmp_"),
        "group": "pos_harvest",
        "text": text,
        "expect_hit": True,
        "note": (
            f"第 26 步采集：来自 note {signal['source_id'][:8]} 的真实内容，"
            f"现命中 {words}，期望继续被扫描到"
        ),
    }
    return (
        {
            "candidate_id": candidate_id,
            "signal_id": signal["signal_id"],
            "signal_kind": signal["kind"],
            "target_testset": "compliance",
            "case": case,
            "expected_source": f"真实命中记录（词条 {words}）——需人工确认不是误伤",
            "requires_expected": False,
            "source_table": signal.get("source_table"),
            "source_run_id": signal.get("source_id"),
            "evidence": evidence,
            "created_at": ec.now_iso(),
            "status": "pending",
        },
        None,
    )


PROPOSERS = {
    "json_robustness": _propose_json_candidate,
    "compliance": _propose_compliance_candidate,
}


def build_candidates(harvest: dict) -> dict:
    """信号 → 候选；同一 run 命中多信号时按 ``SIGNAL_PRIORITY`` 只取一条。"""
    signals = list(harvest.get("signals") or [])
    priority = {kind: index for index, kind in enumerate(ec.SIGNAL_PRIORITY)}
    eligible = [s for s in signals if s.get("target_testset")]
    eligible.sort(
        key=lambda s: (
            s.get("source_id") or "",
            priority.get(s.get("kind"), 99),
            s.get("signal_id") or "",
        )
    )

    chosen: list[dict] = []
    used: set[tuple[str, str]] = set()
    for signal in eligible:
        key = (str(signal.get("source_id")), str(signal.get("target_testset")))
        if key in used:
            continue
        used.add(key)
        chosen.append(signal)

    cases_cache: dict[str, list[dict]] = {}
    candidates: list[dict] = []
    skipped: list[dict] = []
    for index, signal in enumerate(chosen, start=1):
        target = signal["target_testset"]
        if target not in cases_cache:
            _manifest, cases_cache[target] = ec.load_cases(target)
        candidate, reason = PROPOSERS[target](
            signal, cases_cache[target], candidate_id=f"cand_{index:04d}"
        )
        if candidate is None:
            skipped.append(
                {"signal_id": signal["signal_id"], "target_testset": target, "reason": reason}
            )
            continue
        cases_cache[target].append(candidate["case"])  # 让下一条候选拿到不冲突的 id
        candidates.append(candidate)

    by_target: dict[str, int] = {}
    for item in candidates:
        by_target[item["target_testset"]] = by_target.get(item["target_testset"], 0) + 1

    return {
        "proposed_at": ec.now_iso(),
        "harvest_source": harvest.get("source"),
        "signals_total": len(signals),
        "eligible_total": len(eligible),
        "candidates_total": len(candidates),
        "by_target": by_target,
        "skipped_total": len(skipped),
        "candidates": candidates,
        "skipped": skipped,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="飞轮第 2 步：信号 → 候选 case（零 token）")
    parser.add_argument("--harvest", type=Path, required=True, help="evolve_harvest.py 产出的清单")
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    workdir = args.workdir or ec.default_workdir()
    if not args.harvest.is_file():
        print(f"[参数错误] 找不到采集清单：{args.harvest}")
        return ec.EXIT_ENV

    harvest = ec.read_json(args.harvest)
    try:
        payload = build_candidates(harvest)
    except Exception as exc:  # noqa: BLE001
        print(f"[环境错误] 候选生成失败：{type(exc).__name__}: {exc}")
        return ec.EXIT_ENV

    payload["harvest_path"] = str(args.harvest)
    out_path = args.out or (
        ec.ensure_dir(workdir / "review") / f"candidates-{_stamp()}.json"
    )
    ec.write_json(out_path, payload)

    print("=" * 78)
    print("  飞轮第 2 步 · 候选生成（规则化，零 token）")
    print("=" * 78)
    print(f"信号 {payload['signals_total']} 条 → 有目标 {payload['eligible_total']} 条 "
          f"→ 候选 {payload['candidates_total']} 条（跳过 {payload['skipped_total']}）")
    if payload["by_target"]:
        print("目标分布：" + "，".join(f"{k}={v}" for k, v in payload["by_target"].items()))
    print("-" * 78)
    for item in payload["candidates"]:
        case = item["case"]
        preview = case.get("raw") or case.get("text") or ""
        print(f"  {item['candidate_id']}  {item['target_testset']:<16} "
              f"{case.get('label') or ('expect_hit=' + str(case.get('expect_hit'))):<9} "
              f"{preview[:44]!r}")
    for item in payload["skipped"]:
        print(f"  [跳过] {item['signal_id'][:40]:<42} {item['reason']}")
    print("-" * 78)
    print(f"候选清单：{out_path}")
    print("下一步：evolve_review.py（人工确认）——候选不会自动并入。")
    return ec.EXIT_OK


def _stamp() -> str:
    from datetime import datetime  # noqa: PLC0415

    return datetime.now().strftime("%Y%m%d-%H%M%S")


if __name__ == "__main__":
    sys.exit(main())
