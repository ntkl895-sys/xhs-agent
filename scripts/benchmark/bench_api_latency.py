# -*- coding: utf-8 -*-
"""度量专项 · 阶段一.4：本地性能（核心只读接口 P50/P95）。

口径与边界（必须随数字一起引用）：

- **localhost 单机、串行单并发、非生产环境**——数字只反映本机回环 + 本地
  Docker PostgreSQL 的响应速度，不得表述为「线上性能 / 承载能力」；
- 样本 n=20/接口（warmup 3 次不计入）；原始样本逐条留档（不删离群值，
  离群值在 detail 中如实可见）；
- 只测**只读 GET**（零副作用）；含 HTTP 客户端（urllib）开销；
- 接口清单先按候选解析（对照运行中服务的 /openapi.json），试请求非 200 的
  候选会**如实剔除并说明原因**（不计入指标，避免把 422/404 混进性能数字）；
- 每次运行的进程状态、容器版本等环境信息写入结果 JSON。

用法（项目根目录；需后端已启动——可由 run_all_offline.py 编排）：

    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_api_latency.py --base http://127.0.0.1:8000

退出码：0 = 完成；3 = 服务未就绪。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import bench_common as bc

METHOD = (
    "scripts/benchmark/bench_api_latency.py：对运行中的本地后端串行发起 "
    "GET 请求（warmup 3 次不计入，采样 20 次取校准样本），nearest-rank 百分位"
)

#: 候选只读接口（对照 /openapi.json 解析；试请求 200 才纳入采样）
CANDIDATES: list[dict] = [
    {"path": "/health", "params": {}, "label": "后端存活探针（基线，不查库/缓存）"},
    {"path": "/api/v1/hotspots", "params": {"limit": "20"}, "label": "热点列表（当日）"},
    {"path": "/api/v1/hotspots/sources", "params": {}, "label": "热点来源快照"},
    {"path": "/api/v1/topics", "params": {"limit": "20"}, "label": "选题列表（分页）"},
    {"path": "/api/v1/notes", "params": {"limit": "20"}, "label": "笔记列表（分页）"},
    {"path": "/api/v1/accounts", "params": {"limit": "20"}, "label": "账号列表"},
    {"path": "/api/v1/personas", "params": {"limit": "20"}, "label": "人设列表"},
    {"path": "/api/v1/dashboard/summary", "params": {}, "label": "看板·汇总"},
    {"path": "/api/v1/dashboard/trend", "params": {"metric": "likes", "range_days": "30"}, "label": "看板·趋势（metric=likes，30 天）"},
    {"path": "/api/v1/dashboard/ranking", "params": {"metric": "likes", "limit": "20"}, "label": "看板·排行（metric=likes，前 20）"},
    {"path": "/api/v1/reports/weekly", "params": {}, "label": "周报列表"},
    {"path": "/api/v1/knowledge", "params": {"limit": "20"}, "label": "知识块列表（分页）"},
    {"path": "/api/v1/metrics", "params": {}, "label": "数据时间线（动态取 1 个 note_id）", "needs_note": True},
]

WARMUP = 3
SAMPLES = 20


def slug_of(path: str) -> str:
    text = path.strip("/").replace("/", "_").replace("-", "_")
    return text or "root"


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(opener, url: str, timeout: float = 30.0):
    request = urllib.request.Request(
        url, headers={"User-Agent": "xhs-agent-benchmark/1.0"}
    )
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}", (time.perf_counter() - started) * 1000
    return status, body, (time.perf_counter() - started) * 1000


def build_url(base: str, path: str, params: dict) -> str:
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description="本地只读接口时延（benchmark 套件）")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址（默认 127.0.0.1:8000）")
    args = parser.parse_args()

    bc.ensure_dirs()
    bc.print_banner("度量专项 · 本地性能（核心只读接口，localhost 单机）")

    opener = _opener()
    status, _body, _ms = _get(opener, args.base.rstrip("/") + "/health")
    if status != 200:
        print(f"[环境] 后端未就绪（{args.base}/health → {status}），请先启动服务。")
        return bc.EXIT_ENV

    # ---- 对照 openapi.json 解析候选 ----
    status, openapi_body, _ms = _get(opener, args.base.rstrip("/") + "/openapi.json")
    paths_in_spec: set[str] = set()
    if status == 200:
        try:
            paths_in_spec = set(json.loads(openapi_body).get("paths", {}).keys())
        except Exception:  # noqa: BLE001
            paths_in_spec = set()
    print(f"[解析] openapi.json 路径数：{len(paths_in_spec)}")

    # ---- 动态 note_id（/api/v1/metrics 必填）----
    note_id = None
    try:
        st, body, _ = _get(opener, args.base.rstrip("/") + "/api/v1/notes?limit=1")
        if st == 200:
            items = json.loads(body).get("items") or []
            if items:
                note_id = str(items[0].get("id") or "") or None
    except Exception:  # noqa: BLE001
        pass

    resolved: list[dict] = []
    dropped: list[dict] = []
    for candidate in CANDIDATES:
        path = candidate["path"]
        if path != "/health" and paths_in_spec and path not in paths_in_spec:
            dropped.append({**candidate, "reason": "不在 openapi.json 路径清单中"})
            continue
        if candidate.get("needs_note"):
            if not note_id:
                dropped.append({**candidate, "reason": "库内无笔记，note_id 无法动态解析"})
                continue
            candidate = {**candidate, "params": {"note_id": note_id}}
        url = build_url(args.base, path, candidate["params"])
        st, _body, _ms = _get(opener, url)
        if st == 200:
            resolved.append({**candidate, "url": url})
        else:
            dropped.append({**candidate, "reason": f"试请求状态 {st}（避免把非 200 混入性能样本）"})

    if not resolved:
        print("[环境] 无任何候选接口就绪，无法采样。")
        return bc.EXIT_ENV

    print(f"[采样] 纳入 {len(resolved)} 个接口；剔除 {len(dropped)} 个：")
    for item in dropped:
        print(f"        - 剔除 {item['path']}（{item['reason']}）")

    # ---- 采样 ----
    detail: list[dict] = []
    metrics: list[dict] = []
    for item in resolved:
        url = item["url"]
        for _ in range(WARMUP):
            _get(opener, url)
        samples: list[float] = []
        statuses: list[int | None] = []
        for _ in range(SAMPLES):
            st, _body, ms = _get(opener, url)
            statuses.append(st)
            samples.append(round(ms, 2))
        stats = bc.stat_block(samples)
        record = {
            "path": item["path"],
            "label": item["label"],
            "params": item["params"],
            "n": len(samples),
            "samples_ms": samples,
            "non_200_count": sum(1 for st in statuses if st != 200),
            "stats": stats,
        }
        detail.append(record)
        print(
            f"  {item['path']:<38} n={len(samples)}  p50={stats['p50']}ms  p95={stats['p95']}ms  "
            f"max={stats['max']}ms{'  [!] 非200=' + str(record['non_200_count']) if record['non_200_count'] else ''}"
        )
        slug = slug_of(item["path"])
        metrics.append(
            bc.metric(
                f"api_latency.{slug}.p50_ms", stats["p50"], "毫秒", n=stats["n"],
                method=METHOD + f"；接口：{item['path']}（{item['label']}）",
                note=f"localhost 单机、串行单并发、非生产；warmup={WARMUP}；"
                     f"min={stats['min']} max={stats['max']} mean={stats['mean']}（离群值保留在 detail）",
            )
        )
        metrics.append(
            bc.metric(
                f"api_latency.{slug}.p95_ms", stats["p95"], "毫秒", n=stats["n"],
                method=METHOD + f"；接口：{item['path']}（{item['label']}）",
                note="同上；样本 n=20，p95 为 nearest-rank",
            )
        )

    result_path = bc.write_suite_result(
        "api_latency.json",
        suite="api_latency",
        metrics=metrics,
        detail={"endpoints": detail, "dropped": dropped},
        summary={
            "endpoints_measured": len(resolved),
            "endpoints_dropped": len(dropped),
            "warmup": WARMUP,
            "samples_per_endpoint": SAMPLES,
        },
        notes=[
            "localhost 单机、串行单并发、非生产环境的时延；不得外推为线上性能。",
            "样本含 HTTP 客户端（urllib）开销；离群值如实保留（见 detail.samples_ms）。",
            "剔除项的原因逐条记录（避免非 200 响应污染性能样本）。",
        ],
    )
    print("-" * 66)
    print(f"结果：{result_path.relative_to(bc.ROOT)}")
    return bc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
