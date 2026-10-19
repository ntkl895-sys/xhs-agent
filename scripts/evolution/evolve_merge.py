# -*- coding: utf-8 -*-
"""第 26 步 · 飞轮第 4 步：版本化并入（merge gate，**唯一会写夹具的一步**）。

纪律（缺一不可）：

1. 只处理 review 里 ``decision == "adopt"`` 的条目；其余一律不碰；
2. 并入前**再跑一遍静态校验**（review 可被改写，这里不信任上游）；
3. 先整目录备份到 ``<workdir>/backups/<name>/v<旧版本>-<时间戳>``，再写文件；
4. 版本号 +1、``stats`` 重算、``sha256`` 重算、``evolution.history`` 追加一条可追溯记录；
5. 写完立刻按 manifest 复核指纹（写坏了立刻炸，不留半成品）。

用法（项目根目录）：

    backend\\.venv\\Scripts\\python.exe scripts/evolution/evolve_merge.py \\
        --decisions D:\\项目1\\evolution\\review\\decisions-xxxx.json

    # 只校验不落盘
    ... evolve_merge.py --decisions <file> --dry-run

退出码：0 已并入；1 校验失败（未写入）；2 没有可并入条目；3 参数/环境问题。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evolve_common as ec  # noqa: E402

MERGER = "scripts/evolution/evolve_merge.py"


def _merge_one(
    name: str,
    adopted: list[dict],
    *,
    workdir: Path,
    decisions_path: str,
    dry_run: bool,
) -> dict:
    manifest, cases = ec.load_cases(name)
    existing_ids = {str(case.get("id")) for case in cases}

    problems: list[str] = []
    for item in adopted:
        case = item["case"]
        found = ec.validate_case(name, case, existing_ids)
        problems.extend(f"{item['candidate_id']}/{case.get('id')}: {problem}" for problem in found)
        existing_ids.add(str(case.get("id")))
    if problems:
        return {"testset": name, "merged": False, "problems": problems}

    before = {
        "version": manifest.get("version"),
        "n": len(cases),
        "sha256": (manifest.get("sha256") or {}).get("cases.json"),
    }
    new_cases = list(cases) + [item["case"] for item in adopted]
    new_version = ec.bump_version(str(manifest.get("version") or "0.0"))
    new_manifest = dict(manifest)
    new_manifest["version"] = new_version
    new_manifest["generated_at"] = ec.now_iso()
    new_manifest["stats"] = ec.recompute_stats(name, new_cases)
    # 两套目标测试集都只有 cases.json 一个内容文件；其余文件沿用旧指纹
    new_manifest["sha256"] = {
        filename: (
            ec.sha256_of(new_cases)
            if filename == "cases.json"
            else (manifest.get("sha256") or {}).get(filename, "")
        )
        for filename in (manifest.get("sha256") or {"cases.json": ""})
    }

    entry = {
        "merged_by": MERGER,
        "merged_at": ec.now_iso(),
        "parent_version": before["version"],
        "version": new_version,
        "added_case_ids": [item["case"].get("id") for item in adopted],
        "candidate_ids": [item["candidate_id"] for item in adopted],
        "signal_kinds": sorted({str(item.get("signal_kind")) for item in adopted}),
        "source_signal_ids": [item.get("signal_id") for item in adopted],
        "source_run_ids": sorted({str(item.get("source_run_id")) for item in adopted}),
        "decision_file": decisions_path,
        "cases_before": before["n"],
        "cases_after": len(new_cases),
    }
    history = list(((manifest.get("evolution") or {}).get("history")) or [])
    history.append(entry)
    new_manifest["evolution"] = {"history": history}

    if dry_run:
        return {
            "testset": name,
            "merged": False,
            "dry_run": True,
            "before": before,
            "after": {"version": new_version, "n": len(new_cases)},
            "entry": entry,
        }

    backup_dir = ec.backup_testset(name, workdir, tag="pre-merge")
    entry["backup_dir"] = str(backup_dir)

    # 顺序：先 cases 后 manifest —— 中途崩会因指纹不符而显式报错，不会静默半成品
    ec.write_json(ec.testset_dir(name) / "cases.json", new_cases)
    ec.write_json(ec.testset_dir(name) / "manifest.json", new_manifest)
    verified = ec.verify_frozen(name)

    return {
        "testset": name,
        "merged": True,
        "before": before,
        "after": {"version": verified["version"], "n": verified["n"], "sha256": verified["sha256"]},
        "backup_dir": str(backup_dir),
        "entry": entry,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="飞轮第 4 步：版本化并入冻结测试集")
    parser.add_argument("--decisions", type=Path, required=True, help="evolve_review.py 产出的决定清单")
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", help="只校验并预览，不写任何文件")
    args = parser.parse_args(argv)

    if not args.decisions.is_file():
        print(f"[参数错误] 找不到决定清单：{args.decisions}")
        return ec.EXIT_ENV
    payload = ec.read_json(args.decisions)
    adopted = [
        item
        for item in payload.get("items") or []
        if item.get("decision") in ("adopt", "adopt_xfail")
    ]
    for item in adopted:
        if item.get("decision") == "adopt_xfail":
            item["case"] = {**item["case"], "status": "known_failure"}
    print("=" * 78)
    print("  飞轮第 4 步 · 版本化并入" + ("（dry-run，不落盘）" if args.dry_run else ""))
    print("=" * 78)
    if not adopted:
        print("没有 decision=adopt 的条目，夹具未改动。")
        return ec.EXIT_GATED

    by_target: dict[str, list[dict]] = {}
    for item in adopted:
        by_target.setdefault(item["target_testset"], []).append(item)

    workdir = args.workdir or ec.default_workdir()
    results: list[dict] = []
    failed = False
    for name, items in by_target.items():
        try:
            outcome = _merge_one(
                name, items, workdir=workdir, decisions_path=str(args.decisions), dry_run=args.dry_run
            )
        except Exception as exc:  # noqa: BLE001 —— 失败要如实抛给上层，不吞
            outcome = {"testset": name, "merged": False, "problems": [f"{type(exc).__name__}: {exc}"]}
        results.append(outcome)
        if outcome.get("problems"):
            failed = True
            print(f"[{name}] 校验失败，未写入：")
            for problem in outcome["problems"]:
                print(f"  - {problem}")
            continue
        before = outcome.get("before") or {}
        after = outcome.get("after") or {}
        verb = "预演" if outcome.get("dry_run") else "并入完成"
        print(f"[{name}] {verb}：v{before.get('version')} ({before.get('n')} 条) → "
              f"v{after.get('version')} ({after.get('n')} 条)")
        if outcome.get("merged"):
            print(f"  备份：{outcome['backup_dir']}")
            print(f"  新指纹：{after.get('sha256')}")

    record_path = None
    if not args.dry_run:
        record_path = ec.ensure_dir(workdir / "merges") / f"merge-{_stamp()}.json"
        ec.write_json(
            record_path,
            {
                "merged_at": ec.now_iso(),
                "decisions_path": str(args.decisions),
                "results": results,
            },
        )
        print("-" * 78)
        print(f"并入记录：{record_path}")

    print("-" * 78)
    if failed:
        print("结论：存在校验失败条目 → 对应测试集**未被修改**，请修候选后重跑。")
        return ec.EXIT_FAIL
    if not any(item.get("merged") for item in results):
        print("结论：dry-run 完成，夹具未改动。")
        return ec.EXIT_OK
    print("结论：已并入。下一步跑 evolve_gate.py --stage before（**并入前**应先跑过一次）"
          "，或直接 --stage after 做前后对照。")
    return ec.EXIT_OK


def _stamp() -> str:
    from datetime import datetime  # noqa: PLC0415

    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


if __name__ == "__main__":
    sys.exit(main())
