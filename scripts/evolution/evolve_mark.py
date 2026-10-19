# -*- coding: utf-8 -*-
"""第 27 步 · known-failure 通道（stage 1）：把**已存在**的真实错例标成 xfail。

与 `evolve_merge.py` 的区别：

- merge 是「新增用例」，本脚本是「给已有用例加状态」；
- 标记前**先实测**：跑该测试集的评测脚本，确认这条用例当前确实不通过。
  当前就通过的用例不允许标 xfail（那等于把通过说成失败，是把 bug 藏起来的反面——
  把正常说成异常，同样会让报告失真）。

标记只改三处：`cases.json` 里的 `status / xfail`、`manifest.stats`、`manifest.sha256`，
并追加一条 `manifest.evolution.history`（含前后版本、备份目录、来源 run_id）。

用法（项目根目录）：

    :: 先看它是不是真错（只读，不写）
    backend\\.venv\\Scripts\\python.exe scripts/evolution/evolve_mark.py \\
        --testset normalize --case-id norm_107 --dry-run \\
        --root-cause "..." --source-run-id "..."

    :: 确认后标记（会升版本 + 备份 + 重算指纹）
    backend\\.venv\\Scripts\\python.exe scripts/evolution/evolve_mark.py \\
        --testset normalize --case-id norm_107 \\
        --root-cause "..." --source-run-id "..." --reason "..."

退出码：0 已标记（或已是 xfail）；1 拒绝（用例不存在 / 当前通过 / 校验失败）；3 参数或环境问题。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evolve_common as ec  # noqa: E402

MARKER = "scripts/evolution/evolve_mark.py"


def mark_case(
    *,
    testset: str,
    case_id: str,
    root_cause: str,
    source_run_id: str,
    reason: str | None,
    workdir: Path,
    dry_run: bool,
    allow_passing: bool,
) -> dict:
    manifest, cases = ec.load_cases(testset)
    statuses = {str(case.get("id")): ec.case_status(case) for case in cases}
    target = next((case for case in cases if str(case.get("id")) == case_id), None)
    if target is None:
        return {"ok": False, "error": f"用例不存在：{testset}/{case_id}"}
    if statuses.get(case_id) == "known_failure":
        return {"ok": True, "already": True, "case_id": case_id, "version": manifest.get("version")}

    # ---- 实测：当前到底通不通过（不改任何文件）----
    run = ec.run_bench(testset)
    result = run.get("result") or {}
    records = ((result.get("detail") or {}).get("records")) or []
    record = next((item for item in records if str(item.get("id")) == case_id), None)
    if record is None:
        return {"ok": False, "error": f"评测结果里找不到该用例：{case_id}（bench rc={run.get('rc')}）"}
    if record.get("correct") and not allow_passing:
        return {
            "ok": False,
            "error": (
                f"{case_id} 当前实测**通过**（actual={record.get('actual')!r}）——"
                "不能标 known_failure；如确属误判请加 --allow-passing 并说明"
            ),
        }

    xfail = {
        "expected": target.get("expected"),
        "actual": record.get("actual"),
        "root_cause": root_cause,
        "source_run_id": source_run_id,
        "opened_at": ec.now_iso(),
        "evidence": {
            "testset": testset,
            "case_id": case_id,
            "outcome": record.get("outcome"),
            "bench_rc": run.get("rc"),
            "note": reason,
        },
    }
    new_case = {**target, "status": "known_failure", "xfail": xfail}

    existing_other = {str(case.get("id")) for case in cases if str(case.get("id")) != case_id}
    problems = ec.validate_case(testset, new_case, existing_other)
    if problems:
        return {"ok": False, "error": "标记后用例不合法：" + "；".join(problems)}

    new_cases = [new_case if str(c.get("id")) == case_id else c for c in cases]
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
        "merged_by": MARKER,
        "action": "mark_xfail",
        "merged_at": ec.now_iso(),
        "parent_version": manifest.get("version"),
        "version": new_version,
        "case_status_changes": [{"id": case_id, "from": "passing", "to": "known_failure"}],
        "source_run_ids": [source_run_id],
        "reason": reason,
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
            "case_id": case_id,
            "before": {"version": manifest.get("version"), "n": len(cases)},
            "after": {"version": new_version, "n": len(new_cases)},
            "xfail": xfail,
        }

    backup_dir = ec.backup_testset(testset, workdir, tag="pre-xfail")
    entry["backup_dir"] = str(backup_dir)
    ec.write_json(ec.testset_dir(testset) / "cases.json", new_cases)
    ec.write_json(ec.testset_dir(testset) / "manifest.json", new_manifest)
    verified = ec.verify_frozen(testset)
    return {
        "ok": True,
        "case_id": case_id,
        "before": {"version": manifest.get("version"), "n": len(cases)},
        "after": {"version": verified["version"], "n": verified["n"], "sha256": verified["sha256"]},
        "backup_dir": str(backup_dir),
        "xfail": xfail,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="known-failure：把已存在真实错例标成 xfail")
    parser.add_argument("--testset", required=True, choices=sorted(ec.BENCH_SCRIPTS))
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--root-cause", required=True, help="根因假设（必填）")
    parser.add_argument("--source-run-id", required=True, help="来源 run id / 冻结集来源标识")
    parser.add_argument("--reason", default=None)
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-passing", action="store_true", help="允许把当前通过的用例标 xfail（不推荐）")
    args = parser.parse_args(argv)

    workdir = args.workdir or ec.default_workdir()
    print("=" * 78)
    print("  known-failure 通道 · 标记（xfail）" + ("（dry-run）" if args.dry_run else ""))
    print("=" * 78)

    report = mark_case(
        testset=args.testset,
        case_id=args.case_id,
        root_cause=args.root_cause,
        source_run_id=args.source_run_id,
        reason=args.reason,
        workdir=workdir,
        dry_run=args.dry_run,
        allow_passing=args.allow_passing,
    )
    if not report.get("ok"):
        print(f"[拒绝] {report.get('error')}")
        return ec.EXIT_FAIL
    if report.get("already"):
        print(f"[跳过] {args.case_id} 已是 known_failure（v{report.get('version')}），夹具未改动。")
        return ec.EXIT_OK

    before = report.get("before") or {}
    after = report.get("after") or {}
    print(f"实测：{args.case_id} 当前不通过 → expected={report['xfail']['expected']!r}，"
          f"actual={report['xfail']['actual']!r}")
    print(f"版本：v{before.get('version')}（{before.get('n')} 条）→ v{after.get('version')}（{after.get('n')} 条）")
    if report.get("dry_run"):
        print("dry-run：未写任何文件。")
        return ec.EXIT_OK
    print(f"备份：{report['backup_dir']}")
    print(f"新指纹：{after.get('sha256')}")
    print("下一步：evolve_gate.py --stage before（标记后重取基线），再跑 --stage after 验收。")
    return ec.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
