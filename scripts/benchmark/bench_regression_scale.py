# -*- coding: utf-8 -*-
"""度量专项 · 阶段一.1：回归规模（枚举全部 selftest / smoke 脚本并逐个实跑）。

口径说明（写入指标 note 与 REPORT）：

- **用例数**（cases）= 脚本运行后自报的「通过 + 失败 + 跳过」之和，
  即运行时**真实执行**的检查数（含条件性跳过，跳过单独计数）；
- **静态检查点**（check_sites_static）= 源码中 ``c.check(`` 调用点计数，
  循环体只计 1 处，仅作结构参考，与运行时用例数口径不同；
- 脚本清单为**动态枚举**（glob），不硬编码；每个脚本的完整原始输出
  落 ``docs/benchmark/logs/reg_<脚本名>.txt``（UTF-8）。

用法（项目根目录）：

    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_regression_scale.py
        # 离线自检（16 个 selftest_*.py + verify_schema.py）

    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_regression_scale.py --smoke-only
        # 在线冒烟（smoke_*.py；需后端 127.0.0.1:8000 已启动）

退出码：0 = 全部通过；1 = 存在失败/解析失败；3 = 环境不满足（如服务未启动）。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import bench_common as bc

SUMMARY_RE = re.compile(
    r"通过\s*(\d+)\s*项[，,]\s*失败\s*(\d+)\s*项(?:[，,]\s*跳过\s*(\d+)\s*项)?"
)
CHECK_SITE_RE = re.compile(r"\.check\(")

METHOD_RUN = (
    "scripts/benchmark/bench_regression_scale.py 逐个实跑脚本（本机串行、"
    "清空代理环境）并解析汇总行「通过 X 项，失败 Y 项」"
)


def discover_offline() -> list[Path]:
    """离线回归集：全部 selftest_*.py + verify_schema.py（既有回归矩阵成员）。"""
    scripts = list(bc.SCRIPTS_DIR.glob("selftest_*.py"))
    verify = bc.SCRIPTS_DIR / "verify_schema.py"
    if verify.exists():
        scripts.append(verify)
    return sorted(scripts, key=lambda p: p.name)


def discover_smoke() -> list[Path]:
    """在线冒烟集：全部 smoke_*.py（需服务运行）。"""
    return sorted(bc.SCRIPTS_DIR.glob("smoke_*.py"), key=lambda p: p.name)


def static_sites(path: Path) -> int:
    text = path.read_text(encoding="utf-8", errors="replace")
    return len(CHECK_SITE_RE.findall(text))


def parse_summary(text: str) -> dict | None:
    """从输出尾部解析最后一个「通过 X 项，失败 Y 项[，跳过 Z 项]」。"""
    matches = SUMMARY_RE.findall(text)
    if not matches:
        return None
    passed, failed, skipped = matches[-1]
    return {"passed": int(passed), "failed": int(failed), "skipped": int(skipped or 0)}


def run_script(path: Path, *, timeout: int = 1800) -> dict:
    log_path = bc.LOGS_DIR / f"reg_{path.stem}.txt"
    started = time.perf_counter()
    rc, text = bc.run_capture(
        [str(bc.VENV_PY), str(path)],
        cwd=bc.ROOT,
        env=bc.clean_env(),
        log_path=log_path,
        timeout=timeout,
    )
    duration = time.perf_counter() - started
    parsed = parse_summary(text)

    record = {
        "script": path.name,
        "path": str(path.relative_to(bc.ROOT)),
        "exit_code": rc,
        "duration_s": round(duration, 2),
        "check_sites_static": static_sites(path),
        "log_path": str(log_path.relative_to(bc.ROOT)),
        "passed": parsed["passed"] if parsed else None,
        "failed": parsed["failed"] if parsed else None,
        "skipped": parsed["skipped"] if parsed else None,
    }
    if parsed is None:
        record["status"] = "parse_error"
    elif rc == 0 and parsed["failed"] == 0:
        record["status"] = "ok"
    else:
        record["status"] = "fail"
    return record


def build_metrics(detail: list[dict], label: str) -> tuple[list[dict], dict]:
    total_passed = sum(r["passed"] or 0 for r in detail)
    total_failed = sum(r["failed"] or 0 for r in detail)
    total_skipped = sum(r["skipped"] or 0 for r in detail)
    total_sites = sum(r["check_sites_static"] for r in detail)
    ok_count = sum(1 for r in detail if r["status"] == "ok")

    metrics: list[dict] = [
        bc.metric(f"{label}.scripts_total", len(detail), "个", n=len(detail), method=METHOD_RUN),
        bc.metric(f"{label}.cases_passed_total", total_passed, "项", n=len(detail), method=METHOD_RUN),
        bc.metric(f"{label}.cases_failed_total", total_failed, "项", n=len(detail), method=METHOD_RUN),
    ]
    if total_skipped:
        metrics.append(
            bc.metric(
                f"{label}.cases_skipped_total", total_skipped, "项", n=len(detail),
                method=METHOD_RUN,
                note="条件性跳过（如库内已有种子数据），不代表失败；逐脚本原因见各脚本日志",
            )
        )
    metrics.append(
        bc.metric(
            f"{label}.check_sites_static_total", total_sites, "处", n=len(detail),
            method="源码 ``c.check(`` 调用点正则计数（循环体计 1 处，结构参考口径）",
        )
    )
    denominator = max(1, total_passed + total_failed)
    metrics.append(
        bc.metric(
            f"{label}.pass_rate", round(total_passed / denominator, 6), "比率", n=len(detail),
            method=METHOD_RUN, note="分母 = 通过 + 失败（不含条件性跳过）",
        )
    )
    for record in detail:
        cases = (record["passed"] or 0) + (record["failed"] or 0) + (record["skipped"] or 0)
        metrics.append(
            bc.metric(
                f"{label}.{record['script']}.cases", cases, "项", n=1,
                method=METHOD_RUN, log_path=record["log_path"],
                note=(
                    f"通过 {record['passed']} / 失败 {record['failed']} / "
                    f"跳过 {record['skipped']}；退出码 {record['exit_code']}；"
                    f"墙钟 {record['duration_s']}s"
                ),
            )
        )

    summary = {
        "scripts": len(detail),
        "ok": ok_count,
        "passed": total_passed,
        "failed": total_failed,
        "skipped": total_skipped,
        "check_sites_static": total_sites,
    }
    return metrics, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="回归规模实跑（benchmark 套件）")
    parser.add_argument("--smoke-only", action="store_true", help="仅运行 smoke_*.py（需服务已启动）")
    parser.add_argument("--only", default="", help="逗号分隔的子串过滤（调试用，如 selftest_db,selftest_api）")
    args = parser.parse_args()

    bc.ensure_dirs()
    bc.print_banner("度量专项 · 回归规模（逐个实跑，动态枚举）")

    if args.smoke_only:
        scripts = discover_smoke()
        suite_file = "regression_smoke.json"
        label = "regression_smoke"
        status, _body, _ms = bc.http_get("http://127.0.0.1:8000/health")
        if status != 200:
            print(f"[环境] 后端 127.0.0.1:8000/health 未就绪（当前 {status}），smoke 无法执行。")
            print("       请先启动服务（scripts\\ops\\start.ps1），或由 run_all_offline.py 编排执行。")
            return bc.EXIT_ENV
        print(f"[环境] 后端已就绪；将运行 {len(scripts)} 个 smoke 脚本（真实网络 + 真实库）。")
    else:
        scripts = discover_offline()
        suite_file = "regression_scale.json"
        label = "regression"
        print(f"[枚举] 离线回归集：{len(scripts)} 个脚本（selftest_* + verify_schema）")

    if args.only:
        keys = [k.strip() for k in args.only.split(",") if k.strip()]
        scripts = [p for p in scripts if any(k in p.name for k in keys)]
        print(f"[过滤] --only 生效，剩余 {len(scripts)} 个脚本")

    detail: list[dict] = []
    for index, path in enumerate(scripts, start=1):
        print(f"[{index}/{len(scripts)}] {path.name} 运行中 …", flush=True)
        record = run_script(path)
        detail.append(record)
        flag = "OK " if record["status"] == "ok" else record["status"].upper()
        print(
            f"[{index}/{len(scripts)}] {path.name}  {flag}  "
            f"通过={record['passed']} 失败={record['failed']} 跳过={record['skipped']} "
            f"用时={record['duration_s']}s  -> {record['log_path']}",
            flush=True,
        )

    metrics, summary = build_metrics(detail, label)
    notes = [
        "用例数为运行时真实执行数（通过+失败+跳过）；静态检查点为源码调用点计数（口径不同）。",
        "跳过项为条件性跳过（详见各脚本日志），不代表失败；全部为 0 失败时 pass_rate=1.0。",
    ]
    result_path = bc.write_suite_result(
        suite_file, suite=label, metrics=metrics, detail=detail, summary=summary, notes=notes
    )

    print("-" * 66)
    print(
        f"合计：脚本 {summary['scripts']} 个（通过 {summary['ok']}）｜用例 {summary['passed']} 通过"
        f" / {summary['failed']} 失败 / {summary['skipped']} 跳过｜静态检查点 {summary['check_sites_static']} 处"
    )
    print(f"结果：{result_path.relative_to(bc.ROOT)}")
    all_ok = summary["failed"] == 0 and summary["ok"] == summary["scripts"]
    print("全部通过。" if all_ok else "存在失败或解析失败项，请查看上表与日志。")
    return bc.EXIT_OK if all_ok else bc.EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
