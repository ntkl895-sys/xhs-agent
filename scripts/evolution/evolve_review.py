# -*- coding: utf-8 -*-
"""第 26 步 · 飞轮第 3 步：人工确认门（review gate）。

逐条把候选摆到人面前：**采纳 / 拒绝 / 改写 / 跳过**。
这是整套飞轮里唯一允许「决定要不要进冻结集」的地方——脚本本身没有任何自动采纳路径。

两种用法：

1. 交互（人看）：
       backend\\.venv\\Scripts\\python.exe scripts/evolution/evolve_review.py --candidates <file>
2. 非交互（自检/批处理）：``--decisions`` 传入逐条决定：
       {"cand_0001": {"decision": "adopt"},
        "cand_0002": {"decision": "reject", "reason": "误伤"},
        "cand_0003": {"decision": "adopt", "patch": {"note": "改写后的说明"}}}

   没有出现在 decisions 里的候选一律视为 ``skip``（不并入）。

采纳前会跑一次静态校验（字段齐全 / id 不重复 / label 合法…），
**校验不过的候选不允许采纳**，直接标成 ``rejected_invalid``。

退出码：0 完成；3 参数问题。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evolve_common as ec  # noqa: E402

_MENU = "[a]采纳  [x]采纳为 known_failure  [r]拒绝  [e]改写  [s]跳过  [q]退出"


def _as_xfail(case: dict, *, root_cause: str | None, source_run_id: str | None) -> dict:
    """把候选改写成 known_failure：保留期望正确答案，登记根因与来源。"""
    block = {
        "expected": case.get("expected"),
        "actual": None,  # 由 evolve_mark.py 实测填充（人工不允许凭空写「实际行为」）
        "root_cause": root_cause or "（待补：候选未给出根因假设）",
        "source_run_id": source_run_id or "（待补）",
        "opened_at": ec.now_iso(),
    }
    return {**case, "status": "known_failure", "xfail": block}


def _print_candidate(index: int, total: int, candidate: dict) -> None:
    case = candidate["case"]
    print("-" * 78)
    print(f"[{index}/{total}] {candidate['candidate_id']} → {candidate['target_testset']}"
          f"（信号 {candidate['signal_kind']} / 来源 {str(candidate.get('source_run_id'))[:8]}）")
    print(f"期望来源：{candidate.get('expected_source')}")
    print("证据：" + json.dumps(candidate.get("evidence") or {}, ensure_ascii=False)[:240])
    print("用例：")
    print(json.dumps(case, ensure_ascii=False, indent=2))


def _ask(prompt: str) -> str:
    """input 包装：EOF 视为「跳过」（Git Bash 下 isatty 可能撒谎）。"""
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return ""


def _interactive_decide(candidate: dict) -> dict:
    case = candidate["case"]
    target = candidate["target_testset"]
    while True:
        answer = _ask(f"{_MENU} > ")
        if answer in ("q", "quit"):
            return {"decision": "quit"}
        if answer in ("", "s", "skip"):
            return {"decision": "skip"}
        if answer in ("r", "reject"):
            reason = _ask("拒绝理由（可空）> ")
            return {"decision": "reject", "reason": reason or None}
        if answer in ("a", "adopt"):
            return {"decision": "adopt", "case": case}
        if answer in ("x", "xfail", "known_failure"):
            root_cause = _ask("根因假设（必填，写不清就走拒绝）> ")
            if not root_cause:
                print("没有根因假设，不能标 known_failure（xfail 不是藏 bug 的地方）。")
                continue
            return {
                "decision": "adopt_xfail",
                "case": _as_xfail(
                    case,
                    root_cause=root_cause,
                    source_run_id=str(candidate.get("source_run_id") or ""),
                ),
            }
        if answer in ("e", "edit"):
            path = ec.write_json(
                ec.ensure_dir(ec.default_workdir() / "review") / f"edit-{candidate['candidate_id']}.json",
                case,
            )
            print(f"已导出到 {path}，改完保存后回车继续（内容非法会拒绝采纳）。")
            _ask("> ")
            try:
                edited = ec.read_json(path)
            except Exception as exc:  # noqa: BLE001
                print(f"读取失败：{exc}，请重新选择。")
                continue
            problems = ec.validate_case(target, edited, _existing_ids(target))
            if problems:
                print("改后仍不合法：" + "；".join(problems))
                continue
            return {"decision": "adopt", "case": edited, "edited": True}
        print("无效输入。")


def _existing_ids(target: str) -> set[str]:
    _manifest, cases = ec.load_cases(target)
    return {str(case.get("id")) for case in cases}


def _decisions_from_file(path: Path) -> dict:
    raw = ec.read_json(path)
    if not isinstance(raw, dict):
        raise SystemExit(f"[参数错误] {path} 顶层应是对象：{{candidate_id: {{decision: ...}}}}")
    return raw


def review(candidates_payload: dict, decisions: dict | None) -> dict:
    items: list[dict] = []
    candidates = candidates_payload.get("candidates") or []
    for index, candidate in enumerate(candidates, start=1):
        if decisions is not None:
            entry = decisions.get(candidate["candidate_id"]) or {"decision": "skip"}
            decision = str(entry.get("decision") or "skip").lower()
            if entry.get("case"):
                case = entry["case"]
            elif entry.get("patch"):
                case = {**candidate["case"], **(entry.get("patch") or {})}
            else:
                case = candidate["case"]
            reason = entry.get("reason")
            if decision == "adopt_xfail" and not case.get("xfail"):
                case = _as_xfail(
                    case,
                    root_cause=(entry.get("xfail") or {}).get("root_cause"),
                    source_run_id=str(candidate.get("source_run_id") or ""),
                )
            elif decision == "adopt_xfail":
                case = {**case, "status": "known_failure"}
        else:
            _print_candidate(index, len(candidates), candidate)
            result = _interactive_decide(candidate)
            if result["decision"] == "quit":
                items.append(
                    {"candidate_id": candidate["candidate_id"], "decision": "skipped_by_quit"}
                )
                break
            decision = result["decision"]
            case = result.get("case") or candidate["case"]
            reason = result.get("reason")

        record = {
            "candidate_id": candidate["candidate_id"],
            "signal_id": candidate.get("signal_id"),
            "signal_kind": candidate.get("signal_kind"),
            "target_testset": candidate["target_testset"],
            "decision": decision,
            "reason": reason,
            "case": case,
            "expected_source": candidate.get("expected_source"),
        }
        if decision in ("adopt", "adopt_xfail"):
            problems = ec.validate_case(candidate["target_testset"], case, _existing_ids(candidate["target_testset"]))
            if problems:
                record["decision"] = "rejected_invalid"
                record["problems"] = problems
                print(f"[拒绝] {candidate['candidate_id']} 静态校验未通过：" + "；".join(problems))
            else:
                tag = "（known_failure）" if decision == "adopt_xfail" else ""
                print(f"[采纳] {candidate['candidate_id']} → {candidate['target_testset']}"
                      f"（{case.get('id')}）{tag}")
        elif decision == "reject":
            print(f"[拒绝] {candidate['candidate_id']}" + (f"：{reason}" if reason else ""))
        else:
            print(f"[跳过] {candidate['candidate_id']}（{decision}）")
        items.append(record)

    adopted = [item for item in items if item["decision"] in ("adopt", "adopt_xfail")]
    by_target: dict[str, int] = {}
    by_decision: dict[str, int] = {}
    for item in adopted:
        by_target[item["target_testset"]] = by_target.get(item["target_testset"], 0) + 1
        by_decision[item["decision"]] = by_decision.get(item["decision"], 0) + 1
    return {
        "reviewed_at": ec.now_iso(),
        "candidates_total": len(candidates),
        "adopted_total": len(adopted),
        "adopted_by_target": by_target,
        "adopted_by_decision": by_decision,
        "items": items,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="飞轮第 3 步：人工确认门（采纳/拒绝/改写）")
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, default=None, help="非交互：逐条决定 JSON")
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if not args.candidates.is_file():
        print(f"[参数错误] 找不到候选文件：{args.candidates}")
        return ec.EXIT_ENV
    payload = ec.read_json(args.candidates)
    decisions = _decisions_from_file(args.decisions) if args.decisions else None

    print("=" * 78)
    print("  飞轮第 3 步 · 人工确认门（唯一决定权在人手上）")
    print("=" * 78)
    if decisions is not None:
        print(f"非交互模式：{args.decisions}（未列出的候选按 skip 处理）")

    result = review(payload, decisions)
    result["candidates_path"] = str(args.candidates)

    workdir = args.workdir or ec.default_workdir()
    out_path = args.out or (ec.ensure_dir(workdir / "review") / f"decisions-{_stamp()}.json")
    ec.write_json(out_path, result)

    print("-" * 78)
    print(f"采纳 {result['adopted_total']} / {result['candidates_total']}"
          + (f"（{'，'.join(f'{k}={v}' for k, v in result['adopted_by_target'].items())}）"
             if result["adopted_by_target"] else ""))
    print(f"决定清单：{out_path}")
    if result["adopted_total"]:
        print("下一步：evolve_merge.py（版本化并入 + 备份）——此刻夹具仍未被修改。")
    return ec.EXIT_OK


def _stamp() -> str:
    from datetime import datetime  # noqa: PLC0415

    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


if __name__ == "__main__":
    sys.exit(main())
