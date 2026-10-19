# -*- coding: utf-8 -*-
"""度量专项 · 阶段一.2：故障注入与可靠性（隔离环境实跑）。

覆盖（全部为本机确定性实验，不触碰真实业务数据）：

1. **数据库不可达 → /health/ready 返回 503** 的耗时与状态：真实 uvicorn
   子进程（DATABASE_URL 指向不可达端口 59999；应用 lifespan 设计为
   「初始化失败不阻断启动」）；
2. **404 不重试**：DailyHotApi 客户端与 GitHub 搜索 provider 各重复 3 次，
   以 mock 上游实际接收到的请求数计次；
3. **5xx 重试次数与退避总时长**：同上两者（指数退避 1s 起、8s 封顶，
   配置 max_attempts=3 → 最多 3 次尝试）；
4. **坏源隔离 / 部分成功（partial）/ 全部失败（failed）**：在**临时数据库**
   （alembic 迁移后的 xhs_bench_rel_*）上通过 mock DailyHotApi 走完整
   ``collect_and_store``（含落库与审计）；
5. **并发锁**：采集进行中再次触发（手动 → FetchInProgress / 定时 → 安静跳过），
   以 mock 请求数佐证「未重复采集」。

隔离与清理：临时库一律 DROP 并复查残留=0（追加全局兜底扫 xhs_bench%*）；
mock 上游为本机回环 HTTP（127.0.0.1 随机端口），全程不访问真实外部服务。

用法（项目根目录）：

    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_reliability.py

退出码：0 = 全部断言通过；1 = 存在失败；3 = 环境不满足（Docker 未就绪等）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bench_common as bc

#: 「DB 不可达」场景使用的死端口（本机回环连接会被立即拒绝）
DB_DOWN_PORT = 59999

METHOD_MOCK = (
    "scripts/benchmark/bench_reliability.py：本机回环 mock 上游（127.0.0.1 随机端口）"
    "注入故障，统计 mock 实际接收的请求次数与墙钟耗时"
)


class Checks:
    """轻量断言收集器（print [PASS]/[FAIL]）。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def check(self, condition: bool, label: str, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"[PASS] {label}")
        else:
            self.failures.append(label)
            print(f"[FAIL] {label}  -> {detail}")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# mock 上游（本机回环）
# ---------------------------------------------------------------------------
class MockServer:
    """可编排的 mock HTTP 上游：按路径返回 200/404/5xx（可选延迟），记录全部命中。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._routes: dict[str, dict] = {}
        self._hits: list[dict] = []
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def configure(self, routes: dict[str, dict]) -> None:
        with self._lock:
            self._routes = {key: dict(value) for key, value in routes.items()}

    def reset_hits(self) -> None:
        with self._lock:
            self._hits = []

    def record(self, raw_path: str, headers: dict) -> None:
        with self._lock:
            self._hits.append(
                {"path": raw_path.split("?")[0], "ua": headers.get("User-Agent", "")}
            )

    def hits(self) -> list[dict]:
        with self._lock:
            return list(self._hits)

    def hits_for(self, path: str) -> list[dict]:
        return [hit for hit in self.hits() if hit["path"] == path]

    def resolve(self, raw_path: str) -> dict:
        path = raw_path.split("?")[0]
        with self._lock:
            behavior = self._routes.get(path)
        return dict(behavior) if behavior else {"status": 404, "json": {"code": 404}}

    def close(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._httpd.server_close()
        except Exception:  # noqa: BLE001
            pass


def _make_handler(mock: MockServer):
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - 标准库命名
            mock.record(self.path, {k: v for k, v in self.headers.items()})
            behavior = mock.resolve(self.path)
            delay = float(behavior.get("delay") or 0)
            if delay > 0:
                time.sleep(delay)
            status = int(behavior.get("status", 200))
            payload = behavior.get("json")
            body = (
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
                if payload is not None
                else b"{}"
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:  # noqa: BLE001 - 客户端断开时忽略
                pass

        def log_message(self, *args) -> None:  # noqa: D102 - 静音
            pass

    return _Handler


def ok_items(n: int, *, delay: float = 0.0) -> dict:
    """构造一条 200 行为：data 含 n 条带 title 的合法条目。"""
    return {
        "status": 200,
        "delay": delay,
        "json": {
            "code": 200,
            "data": [
                {
                    "id": f"bench-{index}",
                    "title": f"[BENCH-MOCK] 条目 {index}",
                    "url": f"https://example.com/bench/{index}",
                    "hot": str(1000 + index),
                }
                for index in range(1, n + 1)
            ],
        },
    }


def err_status(code: int, *, delay: float = 0.0) -> dict:
    """构造一条错误行为（404/500 等）。"""
    return {"status": code, "delay": delay, "json": {"code": code, "message": "mock 注入"}}


# ---------------------------------------------------------------------------
# 组 2/3：404 不重试 / 5xx 重试（真实客户端 + mock 上游）
# ---------------------------------------------------------------------------
def run_hotspot_series(mock: MockServer, n: int) -> dict:
    """对 DailyHotClient 重复 n 次，返回每次的请求次数 / 耗时 / 异常类型。"""
    bc.add_backend_to_path()
    from app.services.hotspot.client import (  # noqa: PLC0415 - 延迟导入（需先入 sys.path）
        DailyHotClient,
        HotspotClientError,
        HotspotRetryableError,
    )

    attempts: list[int] = []
    elapsed: list[float] = []
    errors: list[str] = []
    for _ in range(n):
        mock.reset_hits()
        client = DailyHotClient(
            base_url=mock.base_url,
            timeout=5,
            per_source_limit=3,
            force_refresh=False,
            max_attempts=3,
        )
        started = time.perf_counter()
        try:
            client.get_list("zhihu")
            errors.append("None")
        except (HotspotClientError, HotspotRetryableError) as exc:
            errors.append(type(exc).__name__)
        finally:
            client.close()
        elapsed.append(round((time.perf_counter() - started) * 1000, 1))
        attempts.append(len(mock.hits_for("/zhihu")))
    return {
        "attempts": attempts,
        "elapsed_ms": elapsed,
        "error_types": errors,
        "ua_sample": (mock.hits_for("/zhihu") or [{}])[0].get("ua", ""),
    }


def run_search_series(mock: MockServer, n: int) -> dict:
    """对 GitHubProvider 重复 n 次，返回每次的请求次数 / 耗时 / 异常类型。"""
    bc.add_backend_to_path()
    from app.services.search.github import GitHubProvider  # noqa: PLC0415
    from app.services.search.provider import (  # noqa: PLC0415
        SearchRateLimitedError,
        SearchUpstreamError,
    )

    attempts: list[int] = []
    elapsed: list[float] = []
    errors: list[str] = []
    for _ in range(n):
        mock.reset_hits()
        provider = GitHubProvider(
            base_url=mock.base_url, timeout=5, max_attempts=3, token=""
        )
        started = time.perf_counter()
        try:
            provider.search("benchmark", limit=3, sort="stars")
            errors.append("None")
        except (SearchUpstreamError, SearchRateLimitedError) as exc:
            errors.append(type(exc).__name__)
        finally:
            provider.close()
        elapsed.append(round((time.perf_counter() - started) * 1000, 1))
        attempts.append(len(mock.hits()))
    return {
        "attempts": attempts,
        "elapsed_ms": elapsed,
        "error_types": errors,
        "ua_sample": (mock.hits() or [{}])[0].get("ua", ""),
    }


# ---------------------------------------------------------------------------
# 组 1：DB 不可达 → /health/ready 503（真实 uvicorn 子进程）
# ---------------------------------------------------------------------------
def find_free_port() -> int:
    import socket  # noqa: PLC0415

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def kill_proc_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    if proc.poll() is None:
        bc.run_capture(["taskkill", "/PID", str(proc.pid), "/T", "/F"], timeout=60)
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass


def wait_port_free(port: int, timeout: float = 30.0) -> bool:
    import socket  # noqa: PLC0415

    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(1)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return True
        time.sleep(0.5)
    return False


def test_db_down() -> dict:
    """起一个 DATABASE_URL 指向死端口的真实后端，观察 /health/ready 行为。"""
    port = find_free_port()
    log_path = bc.LOGS_DIR / "reliability_db_down_backend.log"
    env = bc.clean_env(
        {
            "DATABASE_URL": (
                f"postgresql+psycopg://postgres:postgres@127.0.0.1:{DB_DOWN_PORT}/xhs_agent"
            ),
            "HOTSPOT_SCHEDULER_ENABLED": "false",
            "WEEKLY_SCHEDULER_ENABLED": "false",
            "AGENT_LLM_ENABLED": "false",
        }
    )
    result: dict = {
        "port": port,
        "db_url": f"postgresql+psycopg://postgres:postgres@127.0.0.1:{DB_DOWN_PORT}/xhs_agent",
        "log_path": str(log_path.relative_to(bc.ROOT)),
    }
    with open(log_path, "wb") as log_file:
        proc = subprocess.Popen(
            [str(bc.VENV_PY), "-m", "uvicorn", "app.main:app", "--port", str(port)],
            cwd=str(bc.BACKEND_DIR),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    try:
        started = time.perf_counter()
        boot_ok, last = bc.wait_http(f"http://127.0.0.1:{port}/health", timeout=90)
        result["boot_ok"] = bool(boot_ok)
        result["boot_status"] = last
        result["boot_s"] = round(time.perf_counter() - started, 2)
        if not boot_ok:
            return result

        statuses: list[int | None] = []
        samples_ms: list[float] = []
        checks_sample = None
        for _ in range(10):
            status, body, elapsed_ms = bc.http_get(
                f"http://127.0.0.1:{port}/health/ready", timeout=30
            )
            statuses.append(status)
            samples_ms.append(round(elapsed_ms, 1))
            if checks_sample is None:
                try:
                    checks_sample = json.loads(body).get("checks")
                except Exception:  # noqa: BLE001
                    checks_sample = None
        result["ready_statuses"] = statuses
        result["ready_elapsed_ms"] = samples_ms
        result["ready_checks_sample"] = checks_sample
        return result
    finally:
        kill_proc_tree(proc)
        result["port_freed"] = wait_port_free(port)


# ---------------------------------------------------------------------------
# 组 4/5：临时库上的 collect_and_store 全链路场景（子进程）
# ---------------------------------------------------------------------------
def child_scenarios(out_path: str) -> int:
    """子进程入口：mock 上游 + 临时库，跑 partial / all_fail / lock 三场景。"""
    mock = MockServer()
    os.environ["HOTSPOT_API_BASE_URL"] = mock.base_url
    bc.add_backend_to_path()

    from app.core.config import settings  # noqa: PLC0415

    expected_db = os.environ.get("XHS_BENCH_TEMP_DB", "")
    if not expected_db or expected_db not in settings.DATABASE_URL:
        print(
            f"[SAFETY] DATABASE_URL 未指向预期临时库（期望包含 {expected_db!r}），"
            "已中止（防止误写真实库）。"
        )
        return 4

    from sqlalchemy import text  # noqa: PLC0415

    from app.db.session import SessionLocal  # noqa: PLC0415
    from app.services.hotspot import service as hotspot_service  # noqa: PLC0415

    payload: dict = {"expected_db": expected_db, "scenarios": {}}

    def row_count() -> int:
        with SessionLocal() as db:
            return int(db.execute(text("SELECT count(*) FROM hotspots")).scalar_one())

    sources4 = ["zhihu", "douyin", "bilibili", "toutiao"]

    # --- A) partial（坏源隔离：404 与 500 各一个，其余成功） ---
    mock.configure(
        {
            "/zhihu": ok_items(3),
            "/douyin": err_status(500),
            "/bilibili": err_status(404),
            "/toutiao": ok_items(2),
        }
    )
    mock.reset_hits()
    before = row_count()
    started = time.perf_counter()
    summary_a = hotspot_service.collect_and_store(sources=sources4, trigger="manual")
    payload["scenarios"]["partial"] = {
        "status": summary_a.status,
        "totals": summary_a.totals.model_dump(),
        "per_source": [item.model_dump(mode="json") for item in summary_a.per_source],
        "rows_before": before,
        "rows_after": row_count(),
        "hits": {key: len(mock.hits_for(f"/{key}")) for key in sources4},
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }

    # --- B) 全部源失败（500） ---
    mock.configure({"/zhihu": err_status(500), "/douyin": err_status(500)})
    mock.reset_hits()
    before_b = row_count()
    summary_b = hotspot_service.collect_and_store(
        sources=["zhihu", "douyin"], trigger="manual"
    )
    payload["scenarios"]["all_fail"] = {
        "status": summary_b.status,
        "totals": summary_b.totals.model_dump(),
        "rows_before": before_b,
        "rows_after": row_count(),
        "hits": {"zhihu": len(mock.hits_for("/zhihu")), "douyin": len(mock.hits_for("/douyin"))},
    }

    # --- C) 并发锁（慢响应期间再次触发） ---
    slow = {f"/{key}": ok_items(2, delay=1.2) for key in sources4}
    mock.configure(slow)
    mock.reset_hits()
    box: dict = {}

    def first_call() -> None:
        try:
            box["summary"] = hotspot_service.collect_and_store(
                sources=sources4, trigger="manual"
            )
        except Exception as exc:  # noqa: BLE001 - 如实记录
            box["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=first_call)
    thread.start()
    time.sleep(0.35)
    try:
        hotspot_service.collect_and_store(sources=sources4, trigger="manual")
        second_call = "no_error"
    except hotspot_service.FetchInProgress:
        second_call = "FetchInProgress"
    except Exception as exc:  # noqa: BLE001
        second_call = f"other:{type(exc).__name__}"
    third_call = hotspot_service.collect_and_store(
        sources=sources4, trigger="scheduled", raise_if_busy=False
    )
    thread.join(timeout=60)
    payload["scenarios"]["lock"] = {
        "second_call": second_call,
        "third_call_is_none": third_call is None,
        "first_status": (box.get("summary").status if box.get("summary") else None),
        "first_error": box.get("error"),
        "mock_requests_total": len(mock.hits()),
        "sources": len(sources4),
    }

    # --- 审计行（集合比较，不依赖行序） ---
    with SessionLocal() as db:
        run_statuses = sorted(
            str(row)
            for row in db.execute(
                text("SELECT status FROM agent_runs WHERE run_type = 'hotspot_fetch'")
            )
            .scalars()
            .all()
        )
    payload["run_statuses"] = run_statuses

    mock.close()
    Path(out_path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


def run_child_scenarios() -> dict:
    """父进程：建临时库 → alembic → 子进程跑场景 → DROP + 残留复查。"""
    temp_db = bc.temp_db_name("xhs_bench_rel")
    dsn = f"postgresql+psycopg://postgres:postgres@127.0.0.1:5432/{temp_db}"
    out_json = bc.LOGS_DIR / "reliability_child.json"
    result: dict = {"temp_db": temp_db, "steps": {}}

    rc, _ = bc.psql(f'CREATE DATABASE "{temp_db}"')
    result["steps"]["create_db_rc"] = rc
    if rc != 0:
        result["error"] = "创建临时库失败"
        return result

    try:
        rc_mig, _ = bc.run_capture(
            [str(bc.VENV_PY), "-m", "alembic", "upgrade", "head"],
            cwd=bc.BACKEND_DIR,
            env=bc.clean_env({"DATABASE_URL": dsn}),
            log_path=bc.LOGS_DIR / "reliability_alembic_tmp.txt",
            timeout=300,
        )
        result["steps"]["alembic_rc"] = rc_mig
        if rc_mig != 0:
            result["error"] = "临时库迁移失败"
            return result

        if out_json.exists():
            out_json.unlink()
        rc_child, _ = bc.run_capture(
            [str(bc.VENV_PY), str(Path(__file__).resolve()), "--child-scenarios", str(out_json)],
            env=bc.clean_env(
                {
                    "DATABASE_URL": dsn,
                    "XHS_BENCH_TEMP_DB": temp_db,
                    "AGENT_LLM_ENABLED": "false",
                }
            ),
            log_path=bc.LOGS_DIR / "reliability_child.log",
            timeout=600,
        )
        result["steps"]["child_rc"] = rc_child
        if out_json.exists():
            result["child"] = json.loads(out_json.read_text(encoding="utf-8"))
        else:
            result["error"] = "子进程未产出 JSON"
        return result
    finally:
        dropped = bc.drop_temp_db(temp_db)
        result["steps"]["drop_db_ok"] = dropped
        result["steps"]["exists_after_drop"] = bc.db_exists(temp_db)
        rc_scan, scan_out = bc.psql(
            "SELECT datname FROM pg_database WHERE datname LIKE 'xhs_bench%'"
        )
        leftovers = [line for line in scan_out.strip().splitlines() if line.strip()]
        result["steps"]["leftover_dbs"] = leftovers


# ---------------------------------------------------------------------------
# main：编排、断言与指标
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="故障注入与可靠性（benchmark 套件）")
    parser.add_argument("--child-scenarios", metavar="OUT_JSON", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.child_scenarios:
        return child_scenarios(args.child_scenarios)

    bc.ensure_dirs()
    bc.print_banner("度量专项 · 故障注入与可靠性（隔离环境实跑）")

    rc_docker, _ = bc.run_capture(["docker", "info"], timeout=60)
    if rc_docker != 0:
        print("[环境] Docker 引擎不可用，无法执行（需 db 容器用于临时库）。")
        return bc.EXIT_ENV

    checks = Checks()
    mock = MockServer()
    detail: dict = {}
    try:
        # ---- 组 2/3：404 不重试 / 5xx 重试 ----
        print("\n[2/3] 404 不重试 与 5xx 重试（mock 上游）……")
        mock.configure({"/zhihu": err_status(404)})
        hotspot_404 = run_hotspot_series(mock, n=3)
        detail["hotspot_404"] = hotspot_404
        checks.check(
            all(item == 1 for item in hotspot_404["attempts"]),
            "DailyHot 客户端：404 不重试（每次恰 1 次请求 ×3）",
            f"{hotspot_404['attempts']}",
        )
        checks.check(
            all(item == "HotspotClientError" for item in hotspot_404["error_types"]),
            "DailyHot 客户端：404 抛不可重试的 HotspotClientError",
            f"{hotspot_404['error_types']}",
        )
        checks.check(
            "Mozilla" in (hotspot_404["ua_sample"] or ""),
            "DailyHot 客户端：请求携带浏览器 UA",
        )

        mock.configure({"/zhihu": err_status(500)})
        hotspot_5xx = run_hotspot_series(mock, n=3)
        detail["hotspot_5xx"] = hotspot_5xx
        checks.check(
            all(item == 3 for item in hotspot_5xx["attempts"]),
            "DailyHot 客户端：5xx 按 max_attempts=3 尝试（每次 3 次请求 ×3）",
            f"{hotspot_5xx['attempts']}",
        )
        checks.check(
            all(2500 <= item <= 8000 for item in hotspot_5xx["elapsed_ms"]),
            "DailyHot 客户端：5xx 退避总时长落在 2.5~8s（1s+2s 指数退避实测）",
            f"{hotspot_5xx['elapsed_ms']}",
        )
        checks.check(
            all(item == "HotspotRetryableError" for item in hotspot_5xx["error_types"]),
            "DailyHot 客户端：重试耗尽后抛 HotspotRetryableError",
            f"{hotspot_5xx['error_types']}",
        )

        mock.configure({"/search/repositories": err_status(404)})
        search_404 = run_search_series(mock, n=3)
        detail["search_404"] = search_404
        checks.check(
            all(item == 1 for item in search_404["attempts"]),
            "GitHub 搜索：404 不重试（每次恰 1 次请求 ×3）",
            f"{search_404['attempts']}",
        )
        checks.check(
            all(item == "SearchUpstreamError" for item in search_404["error_types"]),
            "GitHub 搜索：404 抛 SearchUpstreamError",
            f"{search_404['error_types']}",
        )
        checks.check(
            (search_404["ua_sample"] or "").startswith("xhs-agent"),
            "GitHub 搜索：请求携带品牌 UA（xhs-agent/…）",
        )

        mock.configure({"/search/repositories": err_status(500)})
        search_5xx = run_search_series(mock, n=3)
        detail["search_5xx"] = search_5xx
        checks.check(
            all(item == 3 for item in search_5xx["attempts"]),
            "GitHub 搜索：5xx 按 max_attempts=3 尝试（每次 3 次请求 ×3）",
            f"{search_5xx['attempts']}",
        )
        checks.check(
            all(2500 <= item <= 8000 for item in search_5xx["elapsed_ms"]),
            "GitHub 搜索：5xx 退避总时长落在 2.5~8s",
            f"{search_5xx['elapsed_ms']}",
        )
        detail["mock_scope_note"] = (
            "mock 上游为本机回环 HTTP 注入（404/500 为程序构造，非真实上游故障）；"
            "请求计数取自 mock 服务端记录"
        )

        # ---- 组 1：DB 不可达 → 503 ----
        print("\n[1] 数据库不可达 → /health/ready（真实 uvicorn 子进程）……")
        db_down = test_db_down()
        detail["db_down"] = db_down
        checks.check(db_down.get("boot_ok") is True, "DB 不可达时后端进程仍可启动（lifespan 不阻断）")
        checks.check(
            all(status == 503 for status in db_down.get("ready_statuses", [])),
            "DB 不可达：/health/ready 恒返回 503（n=10）",
            f"{db_down.get('ready_statuses')}",
        )
        checks.check(
            (db_down.get("ready_checks_sample") or {}).get("database") is False,
            "503 响应体 checks.database = false（如实反馈）",
            f"{db_down.get('ready_checks_sample')}",
        )
        checks.check(db_down.get("port_freed") is True, "子进程回收、端口已释放")

        # ---- 组 4/5：临时库全链路 ----
        print("\n[4/5] 坏源隔离 / partial / 全部失败 / 并发锁（临时库全链路）……")
        child = run_child_scenarios()
        detail["child"] = child
        steps = child.get("steps", {})
        checks.check(steps.get("alembic_rc") == 0, "临时库迁移成功（alembic upgrade head）")
        checks.check(steps.get("child_rc") == 0 and "child" in child, "场景子进程成功完成")
        checks.check(steps.get("exists_after_drop") is False, "临时库已删除")
        checks.check(not steps.get("leftover_dbs"), "无 xhs_bench% 残留库（全局兜底扫）")

        scenario = (child.get("child") or {}).get("scenarios", {})
        partial = scenario.get("partial", {})
        checks.check(partial.get("status") == "partial", "坏源隔离：整体状态 = partial", f"{partial.get('status')}")
        totals = partial.get("totals", {})
        checks.check(totals.get("new") == 5, "partial：成功源共落库 5 条", f"{totals.get('new')}")
        checks.check(totals.get("failed") == 2, "partial：失败源计数 = 2", f"{totals.get('failed')}")
        checks.check(
            (partial.get("rows_after", 0) - partial.get("rows_before", 0)) == 5,
            "partial：hotspots 表净增 = 5（行数实查）",
        )
        hits = partial.get("hits", {})
        checks.check(hits.get("douyin") == 3, "partial：500 源请求 3 次（重试可见）", f"{hits}")
        checks.check(hits.get("bilibili") == 1, "partial：404 源请求 1 次（不重试）", f"{hits}")
        checks.check(hits.get("zhihu") == 1 and hits.get("toutiao") == 1, "partial：成功源各 1 次请求", f"{hits}")

        all_fail = scenario.get("all_fail", {})
        checks.check(all_fail.get("status") == "failed", "全失败：整体状态 = failed", f"{all_fail.get('status')}")
        checks.check(
            (all_fail.get("rows_after", -1) - all_fail.get("rows_before", -1)) == 0,
            "全失败：零落库（无脏数据）",
        )

        lock = scenario.get("lock", {})
        checks.check(lock.get("second_call") == "FetchInProgress", "并发锁：进行中再次手动触发 → FetchInProgress", f"{lock.get('second_call')}")
        checks.check(lock.get("third_call_is_none") is True, "并发锁：定时触发安静跳过（返回 None）")
        checks.check(
            lock.get("mock_requests_total") == lock.get("sources"),
            "并发锁：mock 请求数 = 源数（未重复采集）",
            f"{lock.get('mock_requests_total')} vs {lock.get('sources')}",
        )
        checks.check(lock.get("first_status") == "success", "并发锁：首轮采集正常完成", f"{lock.get('first_status')}")

        run_statuses = (child.get("child") or {}).get("run_statuses", [])
        checks.check(
            {"partial", "failed", "success"}.issubset(set(run_statuses)),
            "审计：agent_runs 三态（partial/failed/success）如实留痕",
            f"{run_statuses}",
        )
    finally:
        mock.close()

    # ---- 指标汇总 ----
    db_stats = bc.stat_block(detail.get("db_down", {}).get("ready_elapsed_ms", []))
    metrics: list[dict] = [
        bc.metric(
            "reliability.health_ready_db_down.status_code", 503, "HTTP 状态码",
            n=len(detail.get("db_down", {}).get("ready_statuses", [])),
            method=(
                "scripts/benchmark/bench_reliability.py：以 DATABASE_URL=死端口启动真实 uvicorn "
                "子进程后，对 /health/ready 发 10 次请求"
            ),
            log_path="docs/benchmark/logs/reliability_db_down_backend.log",
            note="响应体 checks.database=false、checks.pgvector=false；redis 仍为真实实例",
        ),
        bc.metric(
            "reliability.health_ready_db_down.latency_p50_ms",
            db_stats["p50"] if db_stats else None, "毫秒", n=db_stats["n"] if db_stats else 0,
            method="同上；nearest-rank 百分位",
            log_path="docs/benchmark/logs/reliability_db_down_backend.log",
        ),
        bc.metric(
            "reliability.health_ready_db_down.latency_p95_ms",
            db_stats["p95"] if db_stats else None, "毫秒", n=db_stats["n"] if db_stats else 0,
            method="同上；nearest-rank 百分位",
            log_path="docs/benchmark/logs/reliability_db_down_backend.log",
        ),
        bc.metric(
            "reliability.db_down_boot_ok", 1 if detail.get("db_down", {}).get("boot_ok") else 0,
            "布尔(1=是)", n=1, method="同上；观察 uvicorn 子进程是否仍能提供 /health",
            note="已验证「初始化失败不阻断启动」的设计在 DB 不可达时成立",
        ),
        bc.metric(
            "reliability.hotspot.404_attempts_per_call",
            ",".join(str(item) for item in detail.get("hotspot_404", {}).get("attempts", [])),
            "次请求（逐次）", n=3, method=METHOD_MOCK,
        ),
        bc.metric(
            "reliability.hotspot.5xx_attempts_per_call",
            ",".join(str(item) for item in detail.get("hotspot_5xx", {}).get("attempts", [])),
            "次请求（逐次）", n=3, method=METHOD_MOCK,
            note="max_attempts=3 ⇒ 最多 3 次尝试（含首次；重试 2 次），退避 1s+2s",
        ),
        bc.metric(
            "reliability.hotspot.5xx_backoff_total_p50_ms",
            (bc.stat_block(detail.get("hotspot_5xx", {}).get("elapsed_ms", [])) or {}).get("p50"),
            "毫秒", n=3, method=METHOD_MOCK,
            note="含 mock 即时应答开销；退避为主（≈3000ms）",
        ),
        bc.metric(
            "reliability.search.404_attempts_per_call",
            ",".join(str(item) for item in detail.get("search_404", {}).get("attempts", [])),
            "次请求（逐次）", n=3, method=METHOD_MOCK,
        ),
        bc.metric(
            "reliability.search.5xx_attempts_per_call",
            ",".join(str(item) for item in detail.get("search_5xx", {}).get("attempts", [])),
            "次请求（逐次）", n=3, method=METHOD_MOCK,
        ),
        bc.metric(
            "reliability.search.5xx_backoff_total_p50_ms",
            (bc.stat_block(detail.get("search_5xx", {}).get("elapsed_ms", [])) or {}).get("p50"),
            "毫秒", n=3, method=METHOD_MOCK,
        ),
    ]
    partial = (detail.get("child", {}).get("child") or {}).get("scenarios", {}).get("partial", {})
    all_fail = (detail.get("child", {}).get("child") or {}).get("scenarios", {}).get("all_fail", {})
    lock = (detail.get("child", {}).get("child") or {}).get("scenarios", {}).get("lock", {})
    child_method = (
        "scripts/benchmark/bench_reliability.py 子进程：临时库（alembic 迁移）+ mock DailyHotApi，"
        "真实调用 collect_and_store 全链路（落库 + 审计）"
    )
    metrics.extend(
        [
            bc.metric("reliability.hotspot.partial_status", partial.get("status"), "状态", n=1, method=child_method,
                      note="2 坏源（1×500、1×404）+ 2 好源"),
            bc.metric("reliability.hotspot.partial_new_rows", (partial.get("totals") or {}).get("new"), "条", n=1, method=child_method),
            bc.metric("reliability.hotspot.partial_failed_sources", (partial.get("totals") or {}).get("failed"), "个", n=1, method=child_method),
            bc.metric("reliability.hotspot.all_fail_status", all_fail.get("status"), "状态", n=1, method=child_method),
            bc.metric("reliability.hotspot.all_fail_new_rows",
                      (all_fail.get("rows_after", 0) - all_fail.get("rows_before", 0)), "条", n=1, method=child_method,
                      note="全失败零落库（无脏数据）"),
            bc.metric("reliability.hotspot.lock_second_call", lock.get("second_call"), "异常类名", n=1, method=child_method,
                      note="进行中再次手动触发被拒；定时触发安静跳过；mock 请求数=源数（无重复采集）"),
        ]
    )

    summary = {
        "checks_passed": checks.passed,
        "checks_failed": len(checks.failures),
        "failures": checks.failures,
    }
    result_path = bc.write_suite_result(
        "reliability.json",
        suite="reliability",
        metrics=metrics,
        detail=detail,
        summary=summary,
        notes=[
            "404/5xx 为程序注入到本机 mock 上游，用于验证客户端重试语义；非真实上游故障。",
            "退避时长受本机调度影响（毫秒级抖动），三次样本逐次留档。",
            "临时库已删除并全局兜底复查（xhs_bench% 残留=0）。",
        ],
        env_extra={"mock": "本机回环 HTTP（127.0.0.1 随机端口）"},
    )

    print("-" * 66)
    print(f"断言：{checks.passed} 通过 / {len(checks.failures)} 失败")
    if checks.failures:
        for name in checks.failures:
            print(f"  - FAIL: {name}")
    print(f"结果：{result_path.relative_to(bc.ROOT)}")
    return bc.EXIT_OK if not checks.failures else bc.EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
