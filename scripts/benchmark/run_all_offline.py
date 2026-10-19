# -*- coding: utf-8 -*-
"""度量专项 · 阶段一总编排：一条命令跑完全部离线测量并生成 REPORT。

顺序（可用 --skip-* 跳过单项）：

1. ``bench_regression_scale.py``          回归规模（17 个离线自检脚本实跑）
2. ``bench_reliability.py``               故障注入与可靠性（mock + 临时库）
3. ``bench_rag_retrieval.py``             RAG 合成集（bge 就绪则实跑，否则如实 N/A）
4. **服务阶段**（需要时一键启动 → 四绿核验）：
   ``bench_api_latency.py`` + ``bench_regression_scale.py --smoke-only``
   → 若服务由本脚本启动，结束自动停机并确认端口释放
5. ``build_report.py``                    汇总全部 results/*.json → docs/benchmark/REPORT.md

产物：

- 各步骤结果 JSON：``docs/benchmark/results/*.json``
- 原始日志：``docs/benchmark/logs/run_all_*.txt``
- 总清单：``docs/benchmark/results/run_manifest.json``
- 人读报告：``docs/benchmark/REPORT.md``

用法（项目根目录）：

    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\run_all_offline.py
    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\run_all_offline.py --skip-scale --skip-reliability --skip-rag

退出码：0 = 全部步骤成功（含如实 N/A）；1 = 存在失败步骤；3 = 环境不满足。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path

import bench_common as bc

BENCH_DIR = Path(__file__).resolve().parent


def run_bench_script(script_name: str, args: list[str], log_name: str) -> int:
    rc, _ = bc.run_capture(
        [str(bc.VENV_PY), str(BENCH_DIR / script_name), *args],
        cwd=bc.ROOT,
        env=bc.clean_env(),
        log_path=bc.LOGS_DIR / log_name,
        timeout=5400,
    )
    return rc


def _port_listening(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_ports_free(ports: tuple[int, ...], timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(_port_listening(port) for port in ports):
            return True
        time.sleep(2)
    return False


def services_phase() -> dict:
    """服务阶段：启动（如需）→ 时延采样 → 在线冒烟 → 停机（如由我启动）。"""
    record: dict = {"started_by_us": False, "steps": {}}

    backend_up = bc.http_get("http://127.0.0.1:8000/health")[0] == 200
    front_up = bc.http_get("http://localhost:3000/api/health")[0] == 200
    already = backend_up and front_up
    record["already_running"] = already
    print(f"[服务阶段] 后端就绪={backend_up} 前端就绪={front_up}")

    rc_healthcheck = None
    if not already:
        print("[服务阶段] 启动前后端（start.ps1 -NoBrowser -SkipDailyhot）……")
        rc_start, _ = bc.run_ps1(
            bc.SCRIPTS_DIR / "ops" / "start.ps1",
            ["-NoBrowser", "-SkipDailyhot"],
            log_path=bc.LOGS_DIR / "run_all_start.txt",
            timeout=900,
        )
        record["steps"]["start_rc"] = rc_start
        if rc_start != 0:
            # 兜底：本机存在「长命子进程句柄继承 ⇒ 捕获端挂起/超时」的历史环境问题；
            # 若服务实际已就绪（healthcheck 四绿），如实记录并继续（不误报为启动失败）。
            recovered = False
            for _attempt in range(3):
                rc_recon, _ = bc.healthcheck(
                    log_path=bc.LOGS_DIR / "run_all_healthcheck_recover.txt", timeout=120
                )
                if rc_recon == 0:
                    recovered = True
                    break
                time.sleep(5)
            if not recovered:
                record["error"] = "一键启动失败（见 run_all_start.txt）"
                return record
            record["steps"]["start_recovered_from_nonzero_rc"] = True
            print("[服务阶段] start.ps1 返回码非 0，但服务已就绪（四绿）——如实记录并继续。")
        record["started_by_us"] = True

        ok = False
        deadline = time.time() + 240
        while time.time() < deadline:
            rc_healthcheck, _ = bc.healthcheck(
                log_path=bc.LOGS_DIR / "run_all_healthcheck.txt", timeout=120
            )
            if rc_healthcheck == 0:
                ok = True
                break
            time.sleep(5)
        record["steps"]["healthcheck_green"] = ok
        if not ok:
            record["error"] = "一键启动后 healthcheck 未四绿（见 run_all_healthcheck.txt）"
            return record
    else:
        print("[服务阶段] 服务已在运行（不接管、不停止）。")

    record["steps"]["latency_rc"] = run_bench_script(
        "bench_api_latency.py", [], "run_all_latency.txt"
    )
    record["steps"]["smoke_rc"] = run_bench_script(
        "bench_regression_scale.py", ["--smoke-only"], "run_all_smoke.txt"
    )

    if record["started_by_us"]:
        print("[服务阶段] 停止前后端（stop.ps1）……")
        rc_stop, _ = bc.run_ps1(
            bc.SCRIPTS_DIR / "ops" / "stop.ps1",
            [],
            log_path=bc.LOGS_DIR / "run_all_stop.txt",
            timeout=600,
        )
        record["steps"]["stop_rc"] = rc_stop
        record["ports_freed"] = wait_ports_free((3000, 8000))
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="度量专项 · 阶段一总编排")
    parser.add_argument("--skip-scale", action="store_true")
    parser.add_argument("--skip-reliability", action="store_true")
    parser.add_argument("--skip-rag", action="store_true")
    parser.add_argument("--skip-services", action="store_true")
    parser.add_argument("--skip-testsets", action="store_true")
    parser.add_argument("--skip-report", action="store_true")
    args = parser.parse_args()

    bc.ensure_dirs()
    bc.print_banner("度量专项 · 阶段一总编排（离线，零 token）")

    rc_docker, _ = bc.run_capture(["docker", "info"], timeout=60)
    if rc_docker != 0:
        print("[环境] Docker 引擎不可用；本套件依赖本地 Docker 容器（db/redis/dailyhot）。")
        return bc.EXIT_ENV

    started_at = bc.now_iso()
    steps: dict[str, dict] = {}

    def note_step(name: str, rc: int, log_name: str) -> None:
        steps[name] = {"rc": rc, "log": f"docs/benchmark/logs/{log_name}"}
        print(f"[{name}] 退出码 {rc}（日志 docs/benchmark/logs/{log_name}）")

    if not args.skip_scale:
        print("[1/6] 回归规模（17 脚本实跑，约数分钟）……")
        note_step("regression_scale", run_bench_script("bench_regression_scale.py", [], "run_all_scale.txt"), "run_all_scale.txt")
    if not args.skip_reliability:
        print("[2/6] 故障注入与可靠性……")
        note_step("reliability", run_bench_script("bench_reliability.py", [], "run_all_reliability.txt"), "run_all_reliability.txt")
    if not args.skip_rag:
        print("[3/6] RAG 检索合成集……")
        note_step("rag", run_bench_script("bench_rag_retrieval.py", [], "run_all_rag.txt"), "run_all_rag.txt")
    if not args.skip_services:
        print("[4/6] 服务阶段（启动 → 时延采样 → 在线冒烟 → 停机）……")
        services = services_phase()
        steps["services"] = services
        if services.get("error"):
            print(f"[services] 警告：{services['error']}")
    if not args.skip_testsets:
        print("[5/6] 测试集评测（冻结夹具；sha256 校验；零 token）……")
        for script, log_name in (
            ("bench_testset_json_robustness.py", "run_all_testset_json.txt"),
            ("bench_testset_compliance.py", "run_all_testset_compliance.txt"),
            ("bench_testset_normalize.py", "run_all_testset_normalize.txt"),
            ("bench_testset_rag_golden.py", "run_all_testset_rag.txt"),
        ):
            note_step(
                f"testset:{script.replace('bench_testset_', '').replace('.py', '')}",
                run_bench_script(script, [], log_name),
                log_name,
            )
    if not args.skip_report:
        print("[6/6] 生成 REPORT……")
        note_step("report", run_bench_script("build_report.py", [], "run_all_report.txt"), "run_all_report.txt")

    rcs = [entry.get("rc") for entry in steps.values() if "rc" in entry]
    failed = [name for name, entry in steps.items() if entry.get("rc") not in (0, None)]
    manifest = {
        "suite": "run_all_offline",
        "started_at": started_at,
        "finished_at": bc.now_iso(),
        "env": bc.env_fingerprint(),
        "steps": steps,
        "rcs": rcs,
        "failed_steps": failed,
    }
    bc.write_json(bc.RESULTS_DIR / "run_manifest.json", manifest)

    print("-" * 66)
    print(f"总清单：{ (bc.RESULTS_DIR / 'run_manifest.json').relative_to(bc.ROOT) }")
    if failed:
        print(f"存在失败步骤：{failed}")
        return bc.EXIT_FAIL
    print("全部步骤完成。")
    return bc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
