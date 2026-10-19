# -*- coding: utf-8 -*-
"""第 27 步 · known-failure 通道（stage 1）：缺陷修复后把 xfail 转正。

转正条件（缺一不可，任一不满足即拒绝）：

1. 该用例当前**实测通过**（跑该测试集评测脚本，逐条核对）；
2. **全量离线回归绿**（默认跑 20 脚本；`--skip-regression` 仅用于离线自检，
   正式转正必须带回归结果）；
3. 转正后版本 +1、重算 stats / sha256、备份原目录，并在
   `manifest.evolution.history` 记一条 `close_xfail`（含关闭时间与证据）。

为什么要把「修复」和「转正」绑在一起：xfail 一旦转 passing，它就变成回归锁定用例，
从此任何退化都会被门禁拦下。如果代码没修就转正，等于把一条永不通过的用例塞进 passing 集合，
门禁会天天报假红灯。所以这里宁严不宽。

用法（项目根目录）：

    backend\\.venv\\Scripts\\python.exe scripts/evolution/evolve_close.py \\
        --testset normalize --case-id norm_107 --reason "修复多点小数解析"

退出码：0 已转正；1 拒绝；3 参数/环境问题。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evolve_common as ec  # noqa: E402
import evolve_gate as gate  # noqa: E402

CLOSER = "scripts/evolution/evolve_close.py"


def close_case(
    *,
    testset: str,
    case_id: str,
    reason: str,
    workdir: Path,
    dry_run: bool,
    skip_regression: bool,
) -> dict:
    manifest, cases = ec.load_cases(testset)
    target = next((case for case in cases if str(case.get("id")) == case_id), None)
    if target is None:
        return {"ok": False, "error": f"用例不存在：{testset}/{case_id}"}
    if ec.case_status(target) != "known_failure":
        return {
            "ok": False,
            "error": f"{case_id} 当前状态是 {ec.case_status(target)}，不是 known_failure——无需转正",
        }

    run = ec.run_bench(testset)
    records = ((run.get("result") or {}).get("detail") or {}).get("records") or []
    record = next((item for item in records if str(item.get("id")) == case_id), None)
    if record is None:
        return {"ok": False, "error": f"评测结果里找不到该用例：{case_id}"}
    if not record.get("correct"):
        return {
            "ok": False,
            "error": (
                f"{case_id} 仍然不通过（actual={record.get('actual')!r}，"
                f"expected={target.get('expected')!r}）——修复未完成，不能转正"
            ),
        }

    regression = None
    if not skip_regression:
        print("[回归] 跑全量离线脚本（20 个）……")
        regression = gate._run_full_regression()
        if not regression.get("passed"):
            return {
                "ok": False,
                "error": "全量回归存在失败项，不能转正",
                "regression": regression,
            }

    xfail = dict(ec.case_xfail(target))
    xfail.update(
        {
            "closed_at": ec.now_iso(),
            "closed_reason": reason,
            "closed_actual": record.get("actual"),
            "close_evidence": {"bench_rc": run.get("rc"), "outcome": record.get("outcome")},
        }
    )
    new_case = {**target, "status": "passing", "xfail": xfail}

    existing_other = {str(case.get("id")) for case in cases if str(case.get("id")) != case_id}
    problems = ec.validate_case(testset, new_case, existing_other)
    if problems:
        return {"ok": False, "error": "转正后用例不合法：" + "；".join(problems)}

    new_cases = [new_case if str(case.get("id")) == case_id else case for case in cases]
    new_version = ec.bump_version(str(manifest.get("version") or "0.0"))
    new_manifest = dict(manifest)
    new_manifest["version"] = new_version
    new_manifest["generated_at"] = ec.now_iso()
    new_manifest["stats"] = ec.recompute_stats(testset, new_cases)
    new_manifest["sha256"] = {
        filename: (
            ec.sha256_of(new_cases)
            if filename == "cases.json"
            else (manifest.get("sha256") or {}).get(filename, "")
        )
        for filename in (manifest.get("sha256") or {"cases.json": ""})
    }
    entry = {
        "merged_by": CLOSER,
        "action": "close_xfail",
        "merged_at": ec.now_iso(),
        "parent_version": manifest.get("version"),
        "version": new_version,
        "case_status_changes": [{"id": case_id, "from": "known_failure", "to": "passing"}],
        "reason": reason,
        "regression_passed": None if regression is None else regression.get("passed"),
        "regression_scripts": None if regression is None else regression.get("scripts_total"),
        "cases_before": len(cases),
        "cases_after": len(new_cases),
    }
    history = list(((manifest.get("evolution") or {}).get("history")) or [])
    history.append(entry)
    new_manifest["evolution"] = {"history": history}

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "before": {"version": manifest.get("version"), "n": len(cases)},
            "after": {"version": new_version, "n": len(new_cases)},
            "regression": regression,
        }

    backup_dir = ec.backup_testset(testset, workdir, tag="pre-close")
    entry["backup_dir"] = str(backup_dir)
    ec.write_json(ec.testset_dir(testset) / "cases.json", new_cases)
    ec.write_json(ec.testset_dir(testset) / "manifest.json", new_manifest)
    verified = ec.verify_frozen(testset)
    return {
        "ok": True,
        "before": {"version": manifest.get("version"), "n": len(cases)},
        "after": {"version": verified["version"], "n": verified["n"], "sha256": verified["sha256"]},
        "backup_dir": str(backup_dir),
        "regression": regression,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="known-failure 转正（xfail → passing）")
    parser.add_argument("--testset", required=True, choices=sorted(ec.BENCH_SCRIPTS))
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-regression", action="store_true", help="跳过全量回归（仅离线自检用）")
    args = parser.parse_args(argv)

    workdir = args.workdir or ec.default_workdir()
    print("=" * 78)
    print("  known-failure 通道 · 缺陷关闭（xfail → passing）"
          + ("（dry-run）" if args.dry_run else ""))
    print("=" * 78)

    report = close_case(
        testset=args.testset,
        case_id=args.case_id,
        reason=args.reason,
        workdir=workdir,
        dry_run=args.dry_run,
        skip_regression=args.skip_regression,
    )
    if not report.get("ok"):
        print(f"[拒绝] {report.get('error')}")
        for failure in (report.get("regression") or {}).get("failures") or []:
            print(f"  - {failure['script']} rc={failure['rc']}：{failure['tail']}")
        return ec.EXIT_FAIL

    before = report.get("before") or {}
    after = report.get("after") or {}
    print(f"实测：{args.case_id} 已通过；回归：{(report.get('regression') or {}).get('passed')}")
    print(f"版本：v{before.get('version')} → v{after.get('version')}（{after.get('n')} 条）")
    if report.get("dry_run"):
        print("dry-run：未写任何文件。")
        return ec.EXIT_OK
    print(f"备份：{report['backup_dir']}")
    print(f"新指纹：{after.get('sha256')}")
    print("已转为正式回归用例：此后任何退化都会被门禁拦下。")
    return ec.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
