# -*- coding: utf-8 -*-
"""统一离线自检 runner（第 28 步）。

一条命令串行跑完全部 ``scripts/selftest_*.py`` + ``verify_schema.py``，
按输出中的 ``[PASS]`` / ``[FAIL]`` / ``[SKIP]`` 计数，机器聚合、机器判定退出码。
**只依赖标准库**，不引入 pytest / 任何第三方包。

用法（项目根目录）::

    backend\\.venv\\Scripts\\python.exe scripts\\run_selftests.py
    backend\\.venv\\Scripts\\python.exe scripts\\run_selftests.py --group knowledge
    backend\\.venv\\Scripts\\python.exe scripts\\run_selftests.py --only selftest_rag.py
    backend\\.venv\\Scripts\\python.exe scripts\\run_selftests.py --group ops --allow-destructive
    backend\\.venv\\Scripts\\python.exe scripts\\run_selftests.py --group all --allow-destructive

安全约定：

- 默认**排除** ``selftest_ops.py``（它会停监听服务 / taskkill / DROP DATABASE），
  只有 ``--group ops --allow-destructive`` 才运行；
- 每个脚本独立子进程，``cwd=仓库根``、``stdin=DEVNULL``、清除代理变量；
- 单个脚本超时（默认 600s，ops 组 1800s）不会中断其余脚本，最后统一聚合；
- 任一纳入脚本 FAIL>0 或 rc!=0 → 退出码 1；否则 0。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
DEFAULT_JSON = ROOT / "docs" / "benchmark" / "results" / "regression_run.json"
PYTHON = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
if not PYTHON.is_file():  # 兜底：极端情况下用当前解释器
    PYTHON = Path(sys.executable)

PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)

#: 分组只影响选择，不影响计数。新增脚本必须归组，否则 runner 启动即报错。
GROUPS: dict[str, tuple[str, ...]] = {
    "core": ("selftest_db.py", "selftest_api.py", "selftest_agent_harness.py", "verify_schema.py"),
    "agent": (
        "selftest_content_agent.py",
        "selftest_content_variants.py",
        "selftest_cover.py",
        "selftest_studio.py",
        "selftest_metrics.py",
        "selftest_diagnostics.py",
        "selftest_dashboard.py",
    ),
    "topics": ("selftest_topic_agent.py", "selftest_topic_tool_calling.py", "selftest_hotspot.py"),
    "knowledge": ("selftest_rag.py", "selftest_rag_fusion.py", "selftest_knowledge_admin.py"),
    "evolution": ("selftest_evolution.py", "selftest_evolution_v2.py"),
    "search": ("selftest_search.py",),
    "ops": ("selftest_ops.py",),
}

DESTRUCTIVE = {"selftest_ops.py"}


def discover() -> list[str]:
    names = sorted(path.name for path in SCRIPTS_DIR.glob("selftest_*.py"))
    if (SCRIPTS_DIR / "verify_schema.py").is_file():
        names.append("verify_schema.py")
    return names


def validate_groups(all_names: list[str]) -> None:
    assigned: list[str] = []
    for members in GROUPS.values():
        assigned.extend(members)
    duplicates = sorted({name for name in assigned if assigned.count(name) > 1})
    missing = sorted(set(all_names) - set(assigned))
    unknown = sorted(set(assigned) - set(all_names))
    if duplicates or missing or unknown:
        raise SystemExit(
            "脚本分组与磁盘不一致，请更新 GROUPS："
            f" duplicates={duplicates} missing={missing} unknown={unknown}"
        )


def clean_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in PROXY_KEYS}


def resolve_selection(args: argparse.Namespace) -> list[str]:
    all_names = discover()
    validate_groups(all_names)

    if args.only:
        wanted: list[str] = []
        for raw in args.only:
            for piece in raw.split(","):
                name = piece.strip()
                if not name:
                    continue
                if not name.endswith(".py"):
                    name += ".py"
                if name not in all_names:
                    raise SystemExit(f"未知脚本：{name}（可用 --list 查看）")
                if name not in wanted:
                    wanted.append(name)
        return wanted

    if args.group == "all":
        return list(all_names)
    if args.group:
        return list(GROUPS[args.group])

    return [name for name in all_names if name not in DESTRUCTIVE]


def run_one(name: str, timeout: int) -> dict[str, object]:
    path = SCRIPTS_DIR / name
    started = time.perf_counter()
    timed_out = False
    output = ""
    try:
        proc = subprocess.run(
            [str(PYTHON), str(path)],
            cwd=str(ROOT),
            env=clean_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        rc = proc.returncode
        output = proc.stdout or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        rc = 124
        raw = exc.stdout or ""
        output = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    seconds = round(time.perf_counter() - started, 1)
    passed = output.count("[PASS]")
    failed = output.count("[FAIL]")
    skipped = output.count("[SKIP]")
    runtime = passed + failed + skipped
    empty = passed == 0 and failed == 0 and rc == 0
    ok = rc == 0 and failed == 0 and not empty
    return {
        "name": name,
        "rc": rc,
        "pass": passed,
        "fail": failed,
        "skip": skipped,
        "runtime_cases": runtime,
        "seconds": seconds,
        "timed_out": timed_out,
        "empty": empty,
        "ok": ok,
        "output_tail": "" if ok else "\n".join(output.strip().splitlines()[-15:]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="统一离线自检 runner（标准库实现）")
    parser.add_argument("--group", metavar="名称",
                        help="只跑某一组：core/agent/topics/knowledge/evolution/search/ops/all")
    parser.add_argument("--only", action="append", metavar="脚本名", help="只跑指定脚本（可重复/逗号分隔）")
    parser.add_argument("--json", nargs="?", const=str(DEFAULT_JSON), metavar="路径",
                        help=f"输出机器可读结果（默认 {DEFAULT_JSON.relative_to(ROOT)}）")
    parser.add_argument("--allow-destructive", action="store_true", help="允许运行 selftest_ops.py")
    parser.add_argument("--timeout", type=int, default=600, help="单脚本超时秒数（默认 600）")
    parser.add_argument("--list", action="store_true", help="列出分组与脚本后退出")
    args = parser.parse_args(argv)

    if args.list:
        for group, members in GROUPS.items():
            print(f"{group:<10} {' '.join(members)}")
        print(f"{'all':<10} 全部 {len(discover())} 个（含 ops，需 --allow-destructive）")
        return 0

    if args.only and args.group:
        raise SystemExit("--only 与 --group 不要同时使用")

    if args.group and args.group not in (*GROUPS, "all"):
        raise SystemExit(f"未知分组：{args.group}（可用：{', '.join((*sorted(GROUPS), 'all'))}）")

    selection = resolve_selection(args)
    destructive = [name for name in selection if name in DESTRUCTIVE]
    if destructive and not (args.group in ("ops", "all") and args.allow_destructive):
        print("[GATED] 选中了破坏性脚本：%s" % ", ".join(destructive))
        print("        只有 `--group ops`/`all` 且显式带 `--allow-destructive` 才会运行；本次未执行任何脚本。")
        return 2

    if args.group != "ops" and not destructive:
        print("已排除破坏性脚本：selftest_ops（需要 --group ops --allow-destructive）")

    timeout = 1800 if any(name in DESTRUCTIVE for name in selection) else args.timeout
    print("=" * 96)
    print(f"统一自检 runner · 脚本 {len(selection)} 个 · 单脚本超时 {timeout}s · python={PYTHON}")
    print("=" * 96)

    rows: list[dict[str, object]] = []
    for name in selection:
        print(f"[RUN ] {name}", flush=True)
        row = run_one(name, timeout)
        rows.append(row)
        flag = "OK  " if row["ok"] else "BAD "
        extra = ""
        if row["timed_out"]:
            extra = " TIMEOUT"
        elif row["empty"]:
            extra = " EMPTY(无任何断言输出)"
        print(
            f"[{flag}] {name:<38} rc={row['rc']:<4} PASS={row['pass']:<4} "
            f"FAIL={row['fail']:<3} SKIP={row['skip']:<3} {row['seconds']}s{extra}",
            flush=True,
        )

    total_pass = sum(int(row["pass"]) for row in rows)
    total_fail = sum(int(row["fail"]) for row in rows)
    total_skip = sum(int(row["skip"]) for row in rows)
    total_runtime = sum(int(row["runtime_cases"]) for row in rows)
    total_seconds = round(sum(float(row["seconds"]) for row in rows), 1)
    ok = all(bool(row["ok"]) for row in rows)

    print("-" * 96)
    print(
        f"合计：脚本 {len(rows)} 个 ｜ 运行时用例 {total_runtime}（PASS {total_pass} / "
        f"FAIL {total_fail} / SKIP {total_skip}）｜ 用时 {total_seconds}s ｜ "
        f"{'全部通过' if ok else '存在失败'}"
    )
    if not ok:
        bad = [str(row["name"]) for row in rows if not row["ok"]]
        print(f"异常脚本：{bad}")

    if args.json:
        out_path = Path(args.json)
        if not out_path.is_absolute():
            out_path = ROOT / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "python": str(PYTHON),
            "selection": selection,
            "destructive_included": bool(destructive),
            "scripts": rows,
            "totals": {
                "scripts": len(rows),
                "pass": total_pass,
                "fail": total_fail,
                "skip": total_skip,
                "runtime_cases": total_runtime,
                "seconds": total_seconds,
            },
            "ok": ok,
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON：{out_path}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
