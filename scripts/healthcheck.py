#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""xhs-agent · 一键连通自检（仅使用 Python 标准库）。

依次探测四个端点并打印结果：

    1. http://localhost:8000/health        后端存活
    2. http://localhost:8000/health/ready  后端就绪（含 PostgreSQL / pgvector / Redis）
    3. http://localhost:3000               前端首页可达
    4. http://localhost:3000/api/health    前端 Route Handler 透传

退出码：
    0 —— 前端可达 + 后端存活 + database / pgvector / redis 三项全为 true
    1 —— 任一条件不满足

仅发起本机 HTTP GET 请求，不访问任何公网地址。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# 控制台符号：若当前编码放不下 ✓/✗（如 Windows GBK 控制台），自动退化为 ASCII 标记
# ---------------------------------------------------------------------------
def _can_encode(text: str) -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return True
    except Exception:
        return False


OK_MARK = "\u2713" if _can_encode("\u2713") else "[OK]"
FAIL_MARK = "\u2717" if _can_encode("\u2717") else "[X]"
ARROW = "->"

# 本机健康检查必须【绕过系统代理】：
# 若环境里设置了 HTTP_PROXY / HTTPS_PROXY，urllib 默认会把本机请求也发给代理，
# 导致 localhost 探测出现 502 等假故障。这里显式使用不经过任何代理的 opener。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(url: str, timeout: float) -> tuple[bool, int | None, str]:
    """发起一次 GET 请求（不经过任何代理）。

    返回 (是否成功, HTTP 状态码, 响应体文本)。
    HTTP 4xx/5xx 仍视为「请求成功但状态非 2xx」，会返回其状态码与响应体；
    只有网络层错误（连接被拒、超时等）才返回 ok=False。
    """
    try:
        with _OPENER.open(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return True, resp.getcode(), body
    except urllib.error.HTTPError as exc:  # 服务端返回了非 2xx
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return True, exc.code, body
    except Exception as exc:  # 连接失败 / 超时 / DNS 等
        return False, None, f"{type(exc).__name__}: {exc}"


def parse_json(text: str) -> Any | None:
    try:
        return json.loads(text)
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="xhs-agent 一键连通自检（仅本机 HTTP GET）",
    )
    parser.add_argument("--backend-host", default="localhost", help="后端主机（默认 localhost）")
    parser.add_argument("--backend-port", type=int, default=8000, help="后端端口（默认 8000）")
    parser.add_argument("--frontend-host", default="localhost", help="前端主机（默认 localhost）")
    parser.add_argument("--frontend-port", type=int, default=3000, help="前端端口（默认 3000）")
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="单请求超时秒数（默认 15；中间件不可达时 /health/ready 最长约 10 秒才返回）",
    )
    args = parser.parse_args()

    backend = f"http://{args.backend_host}:{args.backend_port}"
    frontend = f"http://{args.frontend_host}:{args.frontend_port}"

    print("=" * 62)
    print(" xhs-agent · 一键连通自检")
    print("=" * 62)
    print(f" 后端：{backend}")
    print(f" 前端：{frontend}")
    print("-" * 62)

    checks: dict[str, bool] = {}

    # ---- 1. 后端存活探针 ----
    ok, code, body = fetch(f"{backend}/health", args.timeout)
    alive = ok and code == 200
    checks["backend_alive"] = alive
    print(f" [{OK_MARK if alive else FAIL_MARK}] 后端存活   {backend}/health  " f"{ARROW} {code if code is not None else 'unreachable'}")

    # ---- 2. 后端就绪探针 ----
    ok, code, body = fetch(f"{backend}/health/ready", args.timeout)
    ready_payload = parse_json(body) if ok else None
    db_ok = pgv_ok = redis_ok = False
    if isinstance(ready_payload, dict):
        c = ready_payload.get("checks") or {}
        if isinstance(c, dict):
            db_ok = bool(c.get("database"))
            pgv_ok = bool(c.get("pgvector"))
            redis_ok = bool(c.get("redis"))
    ready_all = db_ok and pgv_ok and redis_ok
    checks["database"] = db_ok
    checks["pgvector"] = pgv_ok
    checks["redis"] = redis_ok
    status_text = (ready_payload or {}).get("status") if isinstance(ready_payload, dict) else "unreachable"
    print(f" [{OK_MARK if ok else FAIL_MARK}] 后端就绪   {backend}/health/ready  " f"{ARROW} {code if code is not None else 'unreachable'}"
          + (f"（status={status_text}）" if ok else ""))
    print(f"         ├─ database  {OK_MARK if db_ok else FAIL_MARK}")
    print(f"         ├─ pgvector  {OK_MARK if pgv_ok else FAIL_MARK}")
    print(f"         └─ redis     {OK_MARK if redis_ok else FAIL_MARK}")

    # ---- 3. 前端首页可达 ----
    ok, code, _ = fetch(f"{frontend}/", args.timeout)
    fe_ok = ok and code is not None and 200 <= code < 400
    checks["frontend"] = fe_ok
    print(f" [{OK_MARK if fe_ok else FAIL_MARK}] 前端首页   {frontend}/  " f"{ARROW} {code if code is not None else 'unreachable'}")

    # ---- 4. 前端 API 透传 ----
    ok, code, body = fetch(f"{frontend}/api/health", args.timeout)
    api_payload = parse_json(body) if ok else None
    # 判定标准：请求能到达前端 Route Handler，并返回可解析的健康 JSON。
    # 注意：中间件异常时后端返回 503，前端会【原样透传】该状态码 —— 这正说明透传链路是通的，
    # 因此只要拿到了结构化的 {"status": ...} 即算通过，不把 503 误判为代理故障。
    api_ok = bool(
        ok
        and code is not None
        and isinstance(api_payload, dict)
        and "status" in api_payload
    )
    checks["frontend_api"] = api_ok
    print(f" [{OK_MARK if api_ok else FAIL_MARK}] 前端代理   {frontend}/api/health  " f"{ARROW} {code if code is not None else 'unreachable'}")
    if api_ok:
        brief = json.dumps(api_payload, ensure_ascii=False)
        if len(brief) > 160:
            brief = brief[:160] + "..."
        print(f"         原始返回：{brief}")

    print("-" * 62)

    # ---- 汇总 ----
    all_green = checks["frontend"] and checks["backend_alive"] and ready_all
    if all_green:
        print(f" {OK_MARK} 全部通过：前端可达 + 后端存活 + database/pgvector/redis 均为 true")
        print("=" * 62)
        return 0

    failed = [name for name, passed in checks.items() if not passed]
    print(f" {FAIL_MARK} 未通过，失败项：{', '.join(failed) if failed else '（无）'}")
    print("   排查提示：")
    print("     - 中间件未起：在 xhs-agent 目录执行 docker compose up -d，并用 docker compose ps 确认 healthy")
    print("     - 后端未起：cd backend && uvicorn app.main:app --reload --port 8000")
    print("     - 前端未起：cd frontend && npm run dev")
    print("=" * 62)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
