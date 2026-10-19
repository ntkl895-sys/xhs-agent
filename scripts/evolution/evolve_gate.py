# -*- coding: utf-8 -*-
"""第 26 步 · 飞轮第 5 步：评测门禁（regression gate，**不通过就自动回滚**）。

三个口径（v2 起扩展为「四行报告」，详见 docs/evolution_flywheel.md）：

| 口径 | 含义 | 要求 |
| --- | --- | --- |
| ``legacy_invariant`` | 并入**前已存在**的 passing case，逐条判定是否完全没变 | 必须 100% 相同（确定性评测，变了就是写坏了） |
| ``new_case_consistency`` | 新增 case 的「标注」与「实现实际行为」是否一致（回归锁定要对，known_failure 要红） | 必须 1.0（不一致 = 候选标错 = 坏候选） |
| ``passing / known_failure`` | 报告同时给通过数与 xfail 数；xfail 标红单列、**不阻断、绝不计通过** | 只报告，不折算成一个准确率 |
| ``score_all`` | 全集合准确率（分母变大了，含 xfail） | **只记录**，不作门禁：真实失败样本进集本就会拉低它 |

v2 额外规则：

- 把既有 passing 用例改成 known_failure 属于**降级**，默认拦截，须 ``--allow-xfail-downgrade``；
- 标成 known_failure 但实际通过的用例视为「标错了」，一律拦截；
- 旧 xfail 转绿会出现在 ``xfail_closed_candidates``，提示走 ``evolve_close.py`` 转正（门禁不自动转）。

为什么 ``score_all`` 不作门禁：失败样本按定义就是当前实现过不了的样本，
若拿它当门禁，任何真实失败样本都会被拒之门外，飞轮自我否定。
真正的「退化」只可能是前两条——**旧样本行为变了** 或 **新样本被标错了**。

用法（项目根目录）：

    :: 并入前先取基线（必须，否则 after 无从比对）
    ... evolve_gate.py --testset json_robustness --stage before

    :: 并入后判定；不通过会自动按备份还原并复跑确认
    ... evolve_gate.py --testset json_robustness --stage after

    :: 可选：门禁通过后再跑全量离线回归（19 脚本口径）
    ... evolve_gate.py --testset json_robustness --stage after --full-regression

退出码：0 通过；2 门禁拦截并已回滚；3 环境/参数问题。
"""

from __future__ import annotations

import argparse
import glob
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evolve_common as ec  # noqa: E402


def _gate_path(workdir: Path, testset: str) -> Path:
    return ec.ensure_dir(workdir / "gate") / f"latest-before-{testset}.json"


def _compare(before: dict, after: dict) -> dict:
    """v2 门禁：旧样本不变；新样本按 status 分成「回归锁定」与「known_failure」。"""
    legacy = before.get("by_id") or {}
    current = after.get("by_id") or {}
    status_before = before.get("status_by_id") or {}
    status_after = after.get("status_by_id") or {}

    def _sb(case_id: str) -> str:
        return str(status_before.get(case_id) or "passing")

    def _sa(case_id: str) -> str:
        return str(status_after.get(case_id) or "passing")

    missing = sorted(set(legacy) - set(current))
    common = set(legacy) & set(current)
    # 只有「前后都是 passing」的旧样本，判定变化才算回归；xfail 的转绿是修复信号，不是退化
    changed = sorted(
        case_id
        for case_id in common
        if _sb(case_id) == "passing" and _sa(case_id) == "passing" and legacy[case_id] != current[case_id]
    )
    downgraded = sorted(
        case_id for case_id in common if _sb(case_id) == "passing" and _sa(case_id) == "known_failure"
    )
    upgraded = sorted(
        case_id for case_id in common if _sb(case_id) == "known_failure" and _sa(case_id) == "passing"
    )

    new_ids = sorted(set(current) - set(legacy))
    lock_ids = [case_id for case_id in new_ids if _sa(case_id) == "passing"]
    lock_failed = [case_id for case_id in lock_ids if not current[case_id]]
    new_xfail_ids = [case_id for case_id in new_ids if _sa(case_id) == "known_failure"]
    new_xfail_red = [case_id for case_id in new_xfail_ids if not current[case_id]]
    new_xfail_green = [case_id for case_id in new_xfail_ids if current[case_id]]
    pending_ids = [case_id for case_id in new_ids if _sa(case_id) == "pending"]

    # 旧 xfail：仍在红 = 继续追踪；转绿 = 缺陷关闭候选（提示走 evolve_close.py，不在门禁里自动转正）
    legacy_xfail_ids = [
        case_id for case_id in common if _sb(case_id) == "known_failure" and _sa(case_id) == "known_failure"
    ]
    legacy_xfail_red = [case_id for case_id in legacy_xfail_ids if not current[case_id]]
    xfail_closed = sorted(case_id for case_id in legacy_xfail_ids if current[case_id])

    graded = lock_ids + new_xfail_ids
    consistent = [case_id for case_id in lock_ids if current[case_id]] + new_xfail_red
    all_ids = list(current)
    passing_ids = [case_id for case_id in all_ids if _sa(case_id) == "passing"]
    passing_ok = [case_id for case_id in passing_ids if current[case_id]]

    return {
        "legacy_total": len(legacy),
        "legacy_missing": missing,
        "legacy_changed": changed,
        "legacy_downgraded": downgraded,
        "legacy_upgraded": upgraded,
        "legacy_invariant": not missing and not changed,
        "new_case_total": len(new_ids),
        "regression_lock_total": len(lock_ids),
        "regression_lock_failed": lock_failed,
        # 兼容 v1 口径：新样本里「标注与实现不一致」的 id（含标错的 known_failure）
        "new_case_failed": lock_failed + new_xfail_green,
        "new_xfail_total": len(new_xfail_ids),
        "new_xfail_red": new_xfail_red,
        # 「现在处于 xfail」的口径：新增 xfail + 既有 xfail + 本次被显式降级的
        "xfail_total": len(new_xfail_ids) + len(legacy_xfail_ids) + len(downgraded),
        "xfail_red": (
            new_xfail_red
            + legacy_xfail_red
            + [case_id for case_id in downgraded if not current[case_id]]
        ),
        "xfail_green": new_xfail_green,
        "pending_total": len(pending_ids),
        "xfail_closed_candidates": xfail_closed,
        "new_case_consistency": round(len(consistent) / len(graded), 4) if graded else None,
        "passing_total": len(passing_ids),
        "passing_ok": len(passing_ok),
        "score_all_before": before.get("accuracy"),
        "score_all_after": after.get("accuracy"),
        "score_passing_only": round(len(passing_ok) / len(passing_ids), 4) if passing_ids else None,
        "n_before": before.get("n"),
        "n_after": after.get("n"),
    }


def _verdict_passed(verdict: dict, *, allow_downgrade: bool) -> bool:
    if not verdict["legacy_invariant"]:
        return False
    if verdict["legacy_downgraded"] and not allow_downgrade:
        return False
    if verdict["new_case_consistency"] is not None and verdict["new_case_consistency"] < 1.0:
        return False
    return True


def _latest_merge_backup(workdir: Path, testset: str) -> str | None:
    """从 recent merge 记录里找该测试集的备份目录；没有再退回 manifest.evolution。"""
    merges = sorted((workdir / "merges").glob("merge-*.json")) if (workdir / "merges").is_dir() else []
    for path in reversed(merges):
        try:
            payload = ec.read_json(path)
        except Exception:  # noqa: BLE001
            continue
        for item in payload.get("results") or []:
            if item.get("testset") == testset and item.get("merged") and item.get("backup_dir"):
                return item["backup_dir"]
    history = ((ec.load_manifest(testset).get("evolution") or {}).get("history")) or []
    for entry in reversed(history):
        if entry.get("backup_dir"):
            return entry["backup_dir"]
    return None


def _run_full_regression() -> dict:
    scripts = sorted(glob.glob(str(ec.ROOT / "scripts" / "selftest_*.py")))
    extra = ec.ROOT / "scripts" / "verify_schema.py"
    if extra.exists():
        scripts.append(str(extra))
    failures: list[dict] = []
    for script in scripts:
        proc = subprocess.run(
            [sys.executable, script],
            cwd=str(ec.ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-3:]
            failures.append({"script": Path(script).name, "rc": proc.returncode, "tail": tail})
    return {"scripts_total": len(scripts), "failures": failures, "passed": not failures}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="飞轮第 5 步：评测门禁（不过即回滚）")
    parser.add_argument("--testset", required=True, choices=sorted(ec.BENCH_SCRIPTS))
    parser.add_argument("--stage", required=True, choices=("before", "after"))
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("--before", type=Path, default=None, help="显式指定基线快照")
    parser.add_argument("--full-regression", action="store_true", help="门禁通过后再跑 19 脚本")
    parser.add_argument(
        "--allow-xfail-downgrade",
        action="store_true",
        help="允许把既有 passing 用例降级为 known_failure（默认拦截：降级必须显式声明）",
    )
    args = parser.parse_args(argv)

    workdir = args.workdir or ec.default_workdir()
    testset = args.testset
    print("=" * 78)
    print(f"  飞轮第 5 步 · 评测门禁 [{testset}] stage={args.stage}")
    print("=" * 78)

    run = ec.run_bench(testset)
    if run["result"] is None:
        print(f"[环境错误] 评测脚本未产出结果（rc={run['rc']}）：\n{run['stdout'][-800:]}")
        return ec.EXIT_ENV
    statuses = ec.case_statuses(testset)
    snapshot = ec.snapshot_records(run["result"], statuses)
    snapshot["ran_at"] = ec.now_iso()
    snapshot["bench_rc"] = run["rc"]
    status_line = "，".join(f"{key}={value}" for key, value in ec.status_counts(
        ec.load_cases(testset)[1]
    ).items())
    print(f"本次评测：n={snapshot['n']}，accuracy={snapshot['accuracy']}，"
          f"夹具版本 v{snapshot['fixture_version']}（{status_line}）")

    if args.stage == "before":
        target = args.before or _gate_path(workdir, testset)
        ec.write_json(target, snapshot)
        print(f"基线已存：{target}")
        print("现在可以执行 evolve_merge.py；并入后跑 --stage after。")
        return ec.EXIT_OK

    before_path = args.before or _gate_path(workdir, testset)
    if not before_path.is_file():
        print(f"[参数错误] 找不到基线快照 {before_path}——必须先跑 --stage before（并入之前）。")
        return ec.EXIT_ENV
    before = ec.read_json(before_path)

    verdict = _compare(before, snapshot)
    passed = _verdict_passed(verdict, allow_downgrade=args.allow_xfail_downgrade)

    print("-" * 78)
    print(f"① legacy_invariant：旧样本 {verdict['legacy_total']} 条，"
          f"缺失 {len(verdict['legacy_missing'])}，判定变化 {len(verdict['legacy_changed'])}，"
          f"降级 xfail {len(verdict['legacy_downgraded'])}"
          f" → {'PASS' if verdict['legacy_invariant'] and (args.allow_xfail_downgrade or not verdict['legacy_downgraded']) else 'FAIL'}")
    print(f"② new_case_consistency：新增 {verdict['new_case_total']} 条 = "
          f"回归锁定 {verdict['regression_lock_total']} + known_failure {verdict['new_xfail_total']}"
          f" + pending {verdict['pending_total']}；"
          f"不一致 {len(verdict['regression_lock_failed']) + len(verdict['xfail_green'])}"
          f" → {verdict['new_case_consistency'] if verdict['new_case_consistency'] is not None else 'N/A'}")
    print(f"③ 通过数：passing {verdict['passing_ok']}/{verdict['passing_total']}"
          f"（passing-only 准确率 {verdict['score_passing_only']}）"
          f"｜known_failure 仍红 {len(verdict['xfail_red'])}/{verdict['xfail_total']}（不计通过）")
    print(f"④ score_all（含 xfail，仅记录，不作门禁）："
          f"{verdict['score_all_before']} → {verdict['score_all_after']}")
    if verdict["legacy_changed"]:
        print("   判定的旧样本：" + ", ".join(verdict["legacy_changed"][:10]))
    if verdict["legacy_downgraded"]:
        print("   被降级为 xfail 的旧样本：" + ", ".join(verdict["legacy_downgraded"][:10])
              + ("（已显式允许）" if args.allow_xfail_downgrade else "（需 --allow-xfail-downgrade）"))
    if verdict["regression_lock_failed"]:
        print("   不一致的新增回归样本：" + ", ".join(verdict["regression_lock_failed"][:10]))
    if verdict["xfail_green"]:
        print("   标为 known_failure 但实际通过（标错了）：" + ", ".join(verdict["xfail_green"][:10]))
    if verdict["xfail_closed_candidates"]:
        print("   旧 known_failure 已转绿（走 evolve_close.py 转正）："
              + ", ".join(verdict["xfail_closed_candidates"][:10]))

    report = {
        "testset": testset,
        "stage": "after",
        "ran_at": ec.now_iso(),
        "verdict": verdict,
        "passed": passed,
        "allow_xfail_downgrade": args.allow_xfail_downgrade,
        "rolled_back": False,
        "restore_verified": None,
        "before_snapshot": str(before_path),
        "bench_rc": run["rc"],
    }

    if not passed:
        backup = _latest_merge_backup(workdir, testset)
        if not backup:
            print("[回滚失败] 找不到该测试集的备份目录——请人工按 git 记录恢复。")
            report["rollback_error"] = "backup_not_found"
            ec.write_json(ec.ensure_dir(workdir / "gate") / f"gate-{_stamp()}.json", report)
            return ec.EXIT_GATED

        backup_dir = Path(backup)
        backup_manifest = ec.read_json(backup_dir / "manifest.json")
        backup_sha = (backup_manifest.get("sha256") or {}).get("cases.json")
        backup_version = backup_manifest.get("version")
        if str(before.get("fixture_version")) != str(backup_version):
            print(
                f"[提示] 基线快照是 v{before.get('fixture_version')}，本次并入的父版本是 "
                f"v{backup_version}——两次并入之间没重取基线，"
                "「与基线逐条一致」不适用；以备份的字节级还原为准。"
            )

        restored = ec.restore_backup(testset, backup_dir)
        report["rolled_back"] = True
        report["backup_dir"] = backup
        report["restored"] = restored
        # ① 字节级：还原后的内容指纹必须等于备份里的指纹（与基线无关，恒可判）
        report["restore_verified"] = restored["verified"]["sha256"] == backup_sha
        # ② 口径级：只有当基线快照与备份同版本时，「复跑与基线逐条一致」才有意义
        recheck = ec.run_bench(testset)
        after_restore = ec.snapshot_records(recheck["result"] or {}, ec.case_statuses(testset))
        baseline_comparable = str(before.get("fixture_version")) == str(backup_version)
        same_as_baseline = (
            after_restore.get("by_id") == before.get("by_id")
            and after_restore.get("n") == before.get("n")
        )
        report["restore_matches_baseline"] = same_as_baseline if baseline_comparable else None
        report["restored_snapshot"] = {
            "n": after_restore.get("n"),
            "accuracy": after_restore.get("accuracy"),
            "fixture_version": after_restore.get("fixture_version"),
        }
        print("-" * 78)
        print(f"[门禁拦截] 已回滚到 v{restored['verified']['version']}"
              f"（{restored['verified']['n']} 条，指纹 {restored['verified']['sha256'][:12]}…）")
        print(f"① 字节级还原与备份一致：{'是' if report['restore_verified'] else '否'}")
        if baseline_comparable:
            print(f"② 回滚后复跑与基线逐条一致：{'是' if same_as_baseline else '否'}"
                  f"（n={after_restore.get('n')}，accuracy={after_restore.get('accuracy')}）")
        else:
            print(f"② 基线不同版本，改为记录复跑结果：n={after_restore.get('n')}，"
                  f"accuracy={after_restore.get('accuracy')}")
    else:
        print("-" * 78)
        print("[门禁通过] 旧样本行为未变、新增样本标注与实现一致"
              f"（passing {verdict['passing_ok']}/{verdict['passing_total']}，"
              f"known_failure 仍红 {len(verdict['xfail_red'])}/{verdict['xfail_total']} 已登记不计通过）。")

    if args.full_regression and passed:
        print("-" * 78)
        print("跑全量离线回归……")
        regression = _run_full_regression()
        report["full_regression"] = regression
        print(f"脚本 {regression['scripts_total']} 个，失败 {len(regression['failures'])} 个")
        for failure in regression["failures"]:
            print(f"  - {failure['script']} rc={failure['rc']}：{failure['tail']}")
        if not regression["passed"]:
            passed = False
            report["passed"] = False
            print("[门禁拦截] 全量回归出现失败项（不回滚夹具：属代码/环境问题，需人工判断）。")

    report_path = ec.ensure_dir(workdir / "gate") / f"gate-{_stamp()}.json"
    ec.write_json(report_path, report)
    print(f"门禁报告：{report_path}")
    return ec.EXIT_OK if passed else ec.EXIT_GATED


def _stamp() -> str:
    from datetime import datetime  # noqa: PLC0415

    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


if __name__ == "__main__":
    sys.exit(main())
